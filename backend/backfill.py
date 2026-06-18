"""
One-time backfill script.

Discovers all English-language films released from November 2020 to today
that have at least 100 votes on TMDB, then scores them via OMDb.

This closes the gap between the Kaggle dataset (ends ~Oct 2020) and the present.

Two phases:
    Phase 1 - TMDB discovery
        Loops through monthly date chunks, paginates TMDB discover/movie,
        fetches full details (imdb_id, director, runtime) for each new film,
        and inserts them into the database. No scores yet at this point.

    Phase 2 - OMDb scoring
        Scores up to 950 films on this run using the OMDb free-tier quota.
        The daily updater takes over after that (~200/day) until the backlog clears.

About the audience score:
    RT Audience Score is Rotten Tomatoes proprietary data - no free API provides it.
    We use IMDB rating × 10 as a proxy (e.g. IMDB 7.5 → 75%). This is stored as
    audience_score_source='imdb' in the database and labelled clearly in the UI.
    It's a reasonable stand-in since IMDB and RT audience scores correlate strongly.

Usage:
    python backend/backfill.py

Safe to rerun - films already in the database are skipped automatically.

Required environment variables (same as the main app):
    TMDB_API_KEY, OMDB_API_KEY, DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import os
import sys
import time
import json
import logging
import calendar
from datetime import date, datetime

# make local imports work regardless of the working directory
sys.path.insert(0, os.path.dirname(__file__))

import db
from updater import TmdbClient, OmdbClient, _get, _classify_divergence, TMDB_API_KEY, OMDB_API_KEY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# the Kaggle dataset ends around here - start discovery from the day after
BACKFILL_FROM  = date(2020, 11, 1)
BACKFILL_TO    = date.today()

# only pull films that at least 100 people on TMDB have rated
# below this you get a lot of straight-to-VOD stuff with no critical coverage
TMDB_MIN_VOTES = 100

# OMDb free tier is 1,000 requests/day. We use up to this many in one run
# and leave a small buffer for the daily updater's live-film refreshes.
OMDB_RUN_LIMIT = 950

# TMDB doesn't publish a hard rate limit but 3 requests/second is safe
TMDB_SLEEP     = 0.35  # seconds between calls

# progress file - read by the /api/admin/backfill-progress endpoint
PROGRESS_FILE  = os.path.join(os.path.dirname(__file__), "backfill_progress.json")


# progress tracker
class _ProgressTracker:
    #Writes a JSON status file that the progress page polls every few seconds.
    #Failures to write are silently swallowed - a broken progress file is never
    #worth crashing the backfill over.
    def __init__(self, total_months: int):
        self._data = {
            "phase":   1,
            "phase_1": {
                "status":        "running",
                "months_done":   0,
                "months_total":  total_months,
                "current_month": "",
                "films_found":   0,
                "films_inserted": 0,
            },
            "phase_2": {
                "status":  "pending",
                "scored":  0,
                "total":   0,
                "skipped": 0,
            },
            "complete":   False,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": "",
        }
        self._flush()

    def update_phase1(self, current_month: str, months_done: int,
                      films_found: int, films_inserted: int):
        self._data["phase_1"].update({
            "current_month":  current_month,
            "months_done":    months_done,
            "films_found":    films_found,
            "films_inserted": films_inserted,
        })
        self._flush()

    def phase1_done(self):
        self._data["phase_1"]["status"] = "complete"
        self._data["phase"]             = 2
        self._data["phase_2"]["status"] = "running"
        self._flush()

    def update_phase2(self, scored: int, total: int, skipped: int):
        self._data["phase_2"].update({
            "scored":  scored,
            "total":   total,
            "skipped": skipped,
        })
        self._flush()

    def done(self):
        self._data["complete"]          = True
        self._data["phase_2"]["status"] = "complete"
        self._flush()

    def _flush(self):
        self._data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(PROGRESS_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
        except Exception:
            pass  # never crash the backfill over a logging detail


# helpers
def _iter_months(start: date, end: date):
    #Generates (year, month, month_start_str, month_end_str) for every calendar
    #month in the given range. The last chunk is capped at `end` if it falls
    #mid-month (which it usually does for the current month).
    cur = date(start.year, start.month, 1)
    while cur <= end:
        last_day  = calendar.monthrange(cur.year, cur.month)[1]
        month_end = min(date(cur.year, cur.month, last_day), end)
        yield cur.year, cur.month, str(cur), str(month_end)

        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)


def _load_genre_map() -> dict:
    #Fetches TMDB's complete genre-ID → name table in one API call.
    #We need this because discover/movie returns integer genre IDs, not names.
    data = _get(
        "https://api.themoviedb.org/3/genre/movie/list",
        {"api_key": TMDB_API_KEY, "language": "en-US"},
    )
    if not data:
        logger.warning("Could not load genre map - genres will be blank for backfill films")
        return {}

    genre_map = {g["id"]: g["name"] for g in data.get("genres", [])}
    logger.info(f"Loaded {len(genre_map)} genres from TMDB")
    return genre_map


def _discover_month(year: int, month: int, date_from: str, date_to: str) -> list:
    """
    Pulls all qualifying films for one calendar month by paginating through
    TMDB's discover/movie endpoint. Caps at 500 pages (TMDB's hard limit).
    Sorted by vote_count descending so the most well-known films come first
    - if we ever hit the 10k results cap, we'd rather have the popular films.
    """
    films = []
    page  = 1

    while True:
        data = _get("https://api.themoviedb.org/3/discover/movie", {
            "api_key":                  TMDB_API_KEY,
            "primary_release_date.gte": date_from,
            "primary_release_date.lte": date_to,
            "with_original_language":   "en",
            "vote_count.gte":           TMDB_MIN_VOTES,
            "sort_by":                  "vote_count.desc",
            "page":                     page,
        })
        time.sleep(TMDB_SLEEP)

        if not data or not data.get("results"):
            break

        films.extend(data["results"])
        total_pages = min(data.get("total_pages", 1), 500)

        if page >= total_pages:
            break
        page += 1

    return films


def _extract_imdb_rating(omdb_data: dict) -> float | None:
    #OMDb returns IMDB rating as a string like "7.5" or "N/A".
    #Returns a float or None.
    raw = omdb_data.get("imdbRating", "")
    if raw and raw != "N/A":
        try:
            return float(raw)
        except ValueError:
            pass
    return None


# phase 1: TMDB discovery
def _run_discovery(tmdb: TmdbClient, genre_map: dict, tracker: _ProgressTracker) -> int:
    #Loops through all months from BACKFILL_FROM to today, discovers qualifying
    #films, fetches full details, and inserts them into the database.
    #Returns the number of new films inserted.
    months       = list(_iter_months(BACKFILL_FROM, BACKFILL_TO))
    total_months = len(months)
    total_found  = 0
    total_new    = 0
    today        = date.today()

    for i, (year, month, date_from, date_to) in enumerate(months, 1):
        monthly = _discover_month(year, month, date_from, date_to)
        total_found += len(monthly)

        new_this_month = 0
        for f in monthly:
            tmdb_id = f["id"]
            rt_link = f"tmdb/{tmdb_id}"

            # skip films already in the DB - saves a TMDB detail API call
            if db.get_film(rt_link):
                continue

            details = tmdb.get_details(tmdb_id)
            time.sleep(TMDB_SLEEP)

            if not details:
                continue

            imdb_id      = details.get("imdb_id")
            release_str  = details.get("release_date", "")
            release_date = None
            release_year = None
            if release_str:
                try:
                    release_date = datetime.strptime(release_str[:10], "%Y-%m-%d").date()
                    release_year = release_date.year
                except ValueError:
                    pass

            # build genre string from names instead of IDs
            genre_names = ", ".join(
                genre_map.get(gid, str(gid))
                for gid in (details.get("genre_ids") or [g["id"] for g in details.get("genres", [])])
            )

            crew      = details.get("credits", {}).get("crew", [])
            directors = ", ".join(c["name"] for c in crew if c.get("job") == "Director")

            poster_path = details.get("poster_path") or ""
            poster_url  = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

            # films still within 90 days of release stay in the active review window
            is_active = 1 if release_date and (today - release_date).days < 90 else 0

            db.upsert_film({
                "rotten_tomatoes_link":  rt_link,
                "movie_title":           details.get("title", f.get("title", "")),
                "genres":                genre_names or None,
                "directors":             directors or None,
                "runtime":               details.get("runtime"),
                "original_release_date": release_str[:10] if release_str else None,
                "release_year":          release_year,
                "tomatometer_rating":    None,   # filled in Phase 2
                "audience_rating":       None,
                "divergence_score":      None,
                "divergence_label":      None,
                "critics_consensus":     (details.get("overview") or "")[:500] or None,
                "tmdb_id":               tmdb_id,
                "imdb_id":               imdb_id,
                "poster_url":            poster_url,
                "source":                "backfill",
                "review_fetch_active":   is_active,
                "omdb_last_fetched_at":  None,
                "audience_score_source": None,
            })
            new_this_month += 1

        total_new += new_this_month
        logger.info(
            f"[{i:>2}/{total_months}] {year}-{month:02d}  "
            f"found {len(monthly):>4}  inserted {new_this_month:>4}  "
            f"(total inserted so far: {total_new})"
        )
        tracker.update_phase1(
            current_month  = f"{year}-{month:02d}",
            months_done    = i,
            films_found    = total_found,
            films_inserted = total_new,
        )

    logger.info(f"\nPhase 1 done. Discovered {total_found} films, inserted {total_new} new entries.")
    return total_new


# phase 2: OMDb scoring
def _run_scoring(omdb: OmdbClient, tracker: _ProgressTracker) -> int:
    #Scores up to OMDB_RUN_LIMIT unscored backfill films using OMDb.
    #Uses IMDB rating × 10 as the audience score proxy.
    #Returns the number of films scored.
    unscored = db.get_unscored_tmdb_films(limit=OMDB_RUN_LIMIT)
    logger.info(f"Films awaiting scores: {len(unscored)} (capped at {OMDB_RUN_LIMIT} for this run)")

    tracker.update_phase2(scored=0, total=len(unscored), skipped=0)

    scored  = 0
    skipped = 0

    for film in unscored:
        imdb_id = film.get("imdb_id")
        if not imdb_id:
            skipped += 1
            continue

        omdb_data = omdb.get_by_imdb_id(imdb_id)
        if omdb.quota_exhausted:
            logger.warning(f"OMDb quota hit — scored {scored} before limit, stopping for today")
            break
        if not omdb_data or omdb_data.get("Response") != "True":
            skipped += 1
            continue

        tomatometer = omdb.extract_rt_score(omdb_data)
        imdb_raw    = _extract_imdb_rating(omdb_data)
        imdb_proxy  = round(imdb_raw * 10, 1) if imdb_raw is not None else None

        # need at least one score to be worth writing
        if tomatometer is None and imdb_proxy is None:
            skipped += 1
            continue

        div_score = (
            round(tomatometer - imdb_proxy, 2)
            if tomatometer is not None and imdb_proxy is not None
            else None
        )

        db.update_backfill_scores(
            rt_link               = film["rotten_tomatoes_link"],
            tomatometer_rating    = tomatometer,
            audience_rating       = imdb_proxy,
            audience_score_source = "imdb" if imdb_proxy is not None else None,
            divergence_score      = div_score,
            divergence_label      = _classify_divergence(div_score),
        )
        scored += 1

        if scored % 50 == 0:
            logger.info(f"  OMDb: {scored} scored, {skipped} skipped so far")
            tracker.update_phase2(scored=scored, total=len(unscored), skipped=skipped)

    tracker.update_phase2(scored=scored, total=len(unscored), skipped=skipped)
    return scored


# main
def run_backfill():
    logger.info("=" * 60)
    logger.info("CriticLens backfill")
    logger.info(f"  Date range : {BACKFILL_FROM} → {BACKFILL_TO}")
    logger.info(f"  Min votes  : {TMDB_MIN_VOTES}")
    logger.info(f"  OMDb quota : {OMDB_RUN_LIMIT} calls this run")
    logger.info("=" * 60)

    if not TMDB_API_KEY:
        logger.error("TMDB_API_KEY not set - cannot discover films. Exiting.")
        return

    tmdb = TmdbClient()
    omdb = OmdbClient()

    # make sure the DB schema has the audience_score_source column
    db.init_db()

    # count months up front so the progress tracker can show a denominator
    total_months = sum(1 for _ in _iter_months(BACKFILL_FROM, BACKFILL_TO))
    tracker = _ProgressTracker(total_months)

    # phase 1
    logger.info("\nPhase 1: TMDB film discovery\n")
    genre_map = _load_genre_map()
    _run_discovery(tmdb, genre_map, tracker)
    tracker.phase1_done()

    # phase 2
    if not OMDB_API_KEY:
        logger.info("\nPhase 2: OMDB_API_KEY not set - skipping scoring")
        logger.info("Set OMDB_API_KEY and rerun to score films, or wait for the daily updater.")
        tracker.done()
        return

    logger.info("\nPhase 2: OMDb scoring\n")
    scored = _run_scoring(omdb, tracker)
    logger.info(f"\nPhase 2 done. Scored {scored} films in this run.")

    remaining = db.count_unscored_tmdb_films()
    if remaining > 0:
        days_left = (remaining + 199) // 200  # daily updater does 200/day
        logger.info(
            f"{remaining} films still unscored. "
            f"The daily updater will handle them (~200/day, ~{days_left} more days)."
        )
    else:
        logger.info("All backfill films are scored!")

    tracker.done()
    logger.info("\n=== Backfill complete ===")


if __name__ == "__main__":
    run_backfill()
