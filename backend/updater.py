"""
Daily update job - pulls new films and reviews from external APIs.

How it fits in:
    main.py schedules run_daily_update() at 2am every day via APScheduler.
    It can also be triggered manually via POST /api/admin/trigger-update.

APIs used:
    TMDB     - discovers films released in the last 24 hours
    OMDb     - refreshes Rotten Tomatoes % and IMDB scores for active films
    Guardian - pulls film review articles published yesterday
    NYT      - pulls film review articles published yesterday

All four API keys are optional - the job skips any step where the key is missing,
so the app starts fine even with zero keys configured.

Required environment variables (set whichever you have):
    TMDB_API_KEY      - themoviedb.org developer key (free)
    OMDB_API_KEY      - omdbapi.com key (free, 1000 req/day)
    GUARDIAN_API_KEY  - open-platform.theguardian.com key (free)
    NYT_API_KEY       - developer.nytimes.com key (free)
"""

import os
import re
import logging
import requests
from datetime import datetime, timedelta, date, timezone

import db

logger = logging.getLogger(__name__)

TMDB_API_KEY     = os.environ.get("TMDB_API_KEY",     "")
OMDB_API_KEY     = os.environ.get("OMDB_API_KEY",     "")

GUARDIAN_API_KEY = os.environ.get("GUARDIAN_API_KEY", "")
NYT_API_KEY      = os.environ.get("NYT_API_KEY",      "")

REVIEW_WINDOW_DAYS  = 90  # initial window from release date for new films
EARLY_STOP_DAYS     = 14  # deactivate early if no new reviews found for this many days
# skip very niche TMDB results so we don't burn OMDb's daily quota on films with 2 votes
TMDB_MIN_POPULARITY = 5.0


# shared HTTP helper
class _QuotaExhausted(Exception):
    pass

class _AuthError(Exception):
    pass


def _get(url: str, params: dict, timeout: int = 10) -> dict | None:
    """Simple GET with error handling. Returns parsed JSON or None on failure.
    Raises _QuotaExhausted on 429 (rate/quota limit).
    Raises _AuthError on 401 (bad or unconfigured API key)."""
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            raise _QuotaExhausted(f"Daily quota exhausted for {url}")
        if r.status_code == 401:
            raise _AuthError(f"API key rejected (401) for {url} — check key is valid and has access to this product")
        r.raise_for_status()
        return r.json()
    except (_QuotaExhausted, _AuthError):
        raise
    except requests.RequestException as e:
        logger.warning(f"API request failed ({url}): {e}")
        return None


# shared helpers
def _classify_divergence(score) -> str:
    if score is None:
        return "Aligned"
    if score > 20:
        return "Critics loved it, audiences didn't"
    if score < -20:
        return "Audiences loved it, critics didn't"
    return "Aligned"


def _extract_title_from_headline(headline: str) -> str:
    """
    Best-effort film title extraction from a review headline.

    Guardian format: "Dune: Part Two review – a visually stunning sequel"
    NYT format:      "Review: 'Oppenheimer' Is a Magnificent Achievement"

    Returns the raw headline (truncated) if no pattern matches,
    so the caller can still attempt a database lookup.
    """
    if not headline:
        return ""

    # NYT: "Review: 'Title' verb ..."  or  "Review: Title verb ..."
    nyt = re.match(
        r"[Rr]eview\s*[:–\-]\s*['‘’“”]?(.+?)['‘’“”]?\s+"
        r"(?:is|are|has|have|was|were|offers|takes|gives|brings)",
        headline,
    )
    if nyt:
        return nyt.group(1).strip()

    # Guardian / most other outlets: "Title review" or "Title – sub-headline"
    for sep in [" review", " – ", " - ", ": "]:
        idx = headline.lower().find(sep.lower())
        if idx > 2:
            return headline[:idx].strip().strip("'\"‘’“”")

    return headline[:80].strip()


def _match_review_to_film(film_title: str) -> dict | None:
    """
    Try to find this film in our database.
    1. Exact case-insensitive title match.
    2. If title has a colon (e.g. "Dune: Part Two"), try just the main title ("Dune").
    Returns None if no match found.
    """
    if not film_title:
        return None

    film = db.find_film_by_title(film_title)
    if film:
        return film

    if ":" in film_title:
        short = film_title.split(":")[0].strip()
        film = db.find_film_by_title(short)
        if film:
            return film

    return None


# TMDB client
class TmdbClient:
    BASE = "https://api.themoviedb.org/3"

    def get_recent_films(self, from_date: str, to_date: str) -> list:
        #Films with a primary theatrical release in the given date range.
        #Caps at 5 pages (~100 films) to avoid hammering the API.
        if not TMDB_API_KEY:
            return []

        results = []
        page = 1

        while True:
            data = _get(f"{self.BASE}/discover/movie", {
                "api_key":                     TMDB_API_KEY,
                "primary_release_date.gte":    from_date,
                "primary_release_date.lte":    to_date,
                "sort_by":                     "popularity.desc",
                "vote_count.gte":              3,
                "with_original_language":      "en",
                "page":                        page,
            })

            if not data or not data.get("results"):
                break

            for f in data["results"]:
                if f.get("popularity", 0) >= TMDB_MIN_POPULARITY:
                    results.append(f)

            total_pages = min(data.get("total_pages", 1), 5)
            if page >= total_pages:
                break
            page += 1

        return results

    def get_details(self, tmdb_id: int) -> dict | None:
        #Fetch full film details including cast/crew credits
        if not TMDB_API_KEY:
            return None
        return _get(f"{self.BASE}/movie/{tmdb_id}", {
            "api_key":             TMDB_API_KEY,
            "append_to_response":  "credits",
        })


# OMDb client
class OmdbClient:
    BASE = "http://www.omdbapi.com"

    def __init__(self):
        self.quota_exhausted = False

    def get_by_imdb_id(self, imdb_id: str) -> dict | None:
        if not OMDB_API_KEY or not imdb_id:
            return None
        try:
            return _get(self.BASE, {"apikey": OMDB_API_KEY, "i": imdb_id, "type": "movie"})
        except _QuotaExhausted:
            self.quota_exhausted = True
            logger.warning("OMDb daily quota exhausted, stopping OMDb calls for today")
            return None

    def extract_rt_score(self, omdb_data: dict) -> float | None:
        #Pull the Rotten Tomatoes % from the Ratings array OMDb returns
        for rating in omdb_data.get("Ratings", []):
            if rating.get("Source") == "Rotten Tomatoes":
                try:
                    return float(rating["Value"].replace("%", ""))
                except (ValueError, KeyError):
                    pass
        return None


# Guardian client
class GuardianClient:
    BASE = "https://content.guardianapis.com/search"

    def get_reviews_since(self, from_date: str) -> list:
        #Film review articles published on or after from_date (YYYY-MM-DD).
        #Returns up to 50 results (Guardian's max page size).
        if not GUARDIAN_API_KEY:
            return []

        data = _get(self.BASE, {
            "api-key":    GUARDIAN_API_KEY,
            "section":    "film",
            "tag":        "film/film",
            "from-date":  from_date,
            "show-fields":"headline,byline,bodyText,starRating",
            "page-size":  50,
            "order-by":   "newest",
        })

        if not data or data.get("response", {}).get("status") != "ok":
            return []

        return data["response"].get("results", [])

    def parse_review(self, item: dict) -> dict | None:
        #Convert a Guardian API result into the shape our bulk_insert_reviews expects,
        #plus a film_title key that the caller uses for database matching.
        #Returns None if the item doesn't look like a useful review.
        fields  = item.get("fields", {})
        content = (fields.get("bodyText") or "").strip()
        if not content:
            return None

        headline   = fields.get("headline", "")
        film_title = _extract_title_from_headline(headline)
        stars      = fields.get("starRating")

        # Guardian uses 1-5 stars; 3+ is roughly "Fresh" by RT standards
        review_type = "Fresh" if (not stars or int(stars or 0) >= 3) else "Rotten"

        return {
            "film_title":      film_title,
            "critic_name":     (fields.get("byline") or "The Guardian").strip(),
            "publisher_name":  "The Guardian",
            "review_date":     item.get("webPublicationDate", "")[:10],
            "review_content":  content[:6000],
            "review_type":     review_type,
            "review_score":    str(stars) if stars else None,
            "external_id":     item.get("id", ""),
            "source":          "guardian",
        }


# NYT client
class NytClient:
    BASE = "https://api.nytimes.com/svc/search/v2/articlesearch.json"

    def get_reviews_since(self, from_date: str) -> list:
        #NYT film reviews published on or after from_date (YYYY-MM-DD).
        #NYT's date format for begin_date is YYYYMMDD.
        if not NYT_API_KEY:
            return []

        data = _get(self.BASE, {
            "api-key":    NYT_API_KEY,
            "fq":         'section_name:"Movies" AND type_of_material:"Review"',
            "begin_date": from_date.replace("-", ""),
            "sort":       "newest",
            "page":       0,
        })

        if not data or data.get("status") != "OK":
            return []

        return data.get("response", {}).get("docs", [])

    def parse_review(self, item: dict) -> dict | None:
        content = (item.get("abstract") or item.get("lead_paragraph") or "").strip()
        if not content:
            return None

        headline   = item.get("headline", {})
        hl_text    = headline.get("main", "") if isinstance(headline, dict) else ""
        film_title = _extract_title_from_headline(hl_text)

        byline = item.get("byline") or {}
        critic = byline.get("original", "") if isinstance(byline, dict) else ""
        critic = re.sub(r"^By\s+", "", critic, flags=re.IGNORECASE).strip()

        return {
            "film_title":      film_title,
            "critic_name":     critic or "NYT",
            "publisher_name":  "New York Times",
            "review_date":     item.get("pub_date", "")[:10],
            "review_content":  content[:6000],
            "review_type":     "Fresh",  # NYT does not use Fresh/Rotten labels
            "review_score":    None,
            "external_id":     item.get("_id", ""),
            "source":          "nyt",
        }


# main update entry point
def run_daily_update():
    """
    Full daily update cycle. Called by APScheduler at 2am and by the admin endpoint.

    Steps:
        1. Discover films released yesterday → add new ones (TMDB)
        2. Refresh RT/IMDB scores for active films (OMDb)
        3. Pull yesterday's film reviews and link them to known films (Guardian)
        4. Same for NYT
        5. Deactivate films past the 90-day review window
    """
    logger.info("=== Daily update started ===")

    today     = date.today()
    yesterday = today - timedelta(days=1)

    tmdb     = TmdbClient()
    omdb     = OmdbClient()
    guardian = GuardianClient()
    nyt      = NytClient()

    # 1. New films from TMDB
    if TMDB_API_KEY:
        logger.info("[1/4] Discovering new films via TMDB...")
        new_films = tmdb.get_recent_films(str(yesterday), str(today))
        added = 0

        for f in new_films:
            tmdb_id = f["id"]
            rt_link = f"tmdb/{tmdb_id}"

            if db.get_film(rt_link):
                continue  # already in the database

            details = tmdb.get_details(tmdb_id)
            if not details:
                continue

            imdb_id      = details.get("imdb_id")
            release_str  = details.get("release_date", "")
            release_year = int(release_str[:4]) if len(release_str) >= 4 else None

            # grab RT score from OMDb while we have the imdb_id handy
            tomatometer = None
            if OMDB_API_KEY and imdb_id:
                omdb_data = omdb.get_by_imdb_id(imdb_id)
                if omdb_data and omdb_data.get("Response") == "True":
                    tomatometer = omdb.extract_rt_score(omdb_data)

            genres = ", ".join(g["name"] for g in details.get("genres", []))

            # pull director from credits
            crew      = details.get("credits", {}).get("crew", [])
            directors = ", ".join(c["name"] for c in crew if c.get("job") == "Director")[:2]

            poster_path = details.get("poster_path") or ""
            poster_url  = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

            db.upsert_film({
                "rotten_tomatoes_link": rt_link,
                "movie_title":          details.get("title", f.get("title", "")),
                "genres":               genres or None,
                "directors":            directors or None,
                "runtime":              details.get("runtime"),
                "original_release_date":release_str or None,
                "release_year":         release_year,
                "tomatometer_rating":   tomatometer,
                "audience_rating":      None,
                "divergence_score":     None,
                "divergence_label":     "Aligned",
                "critics_consensus":    (details.get("overview") or "")[:500] or None,
                "tmdb_id":              tmdb_id,
                "imdb_id":              imdb_id,
                "poster_url":           poster_url,
                "source":               "tmdb",
                "review_fetch_active":  1,
                "omdb_last_fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })
            added += 1

        logger.info(f"[1/4] Added {added} new films")
    else:
        logger.info("[1/4] TMDB_API_KEY not set - skipping film discovery")

    # 2. Score unscored backfill films first — gets first access to daily quota
    #    so the ~436-film backlog clears before live-film refreshes consume calls.
    if OMDB_API_KEY:
        _score_backfill_batch(omdb, batch_size=800)

    # 3. Refresh scores for active films via OMDb (runs after backfill so the
    #    backlog has budget priority; quota check stops the loop on exhaustion)
    if OMDB_API_KEY and not omdb.quota_exhausted:
        logger.info("[2/4] Refreshing scores via OMDb...")
        active    = db.get_active_films()
        refreshed = 0

        for film in active:
            if omdb.quota_exhausted:
                logger.warning("[2/4] OMDb quota exhausted mid-refresh — stopping")
                break

            imdb_id = film.get("imdb_id")
            if not imdb_id:
                continue

            omdb_data = omdb.get_by_imdb_id(imdb_id)
            if not omdb_data or omdb_data.get("Response") != "True":
                continue

            tomatometer = omdb.extract_rt_score(omdb_data)
            if tomatometer is None:
                continue

            aud = film.get("audience_rating")
            div = round(tomatometer - aud, 2) if aud is not None else None

            db.upsert_film({
                **film,
                "tomatometer_rating":   tomatometer,
                "divergence_score":     div,
                "divergence_label":     _classify_divergence(div),
                "omdb_last_fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            })
            refreshed += 1

        logger.info(f"[2/4] Refreshed scores for {refreshed} films")
    else:
        logger.info("[2/4] OMDB_API_KEY not set - skipping score refresh")

    # 3. Guardian reviews
    if GUARDIAN_API_KEY:
        logger.info("[3/4] Fetching Guardian reviews...")
        items    = guardian.get_reviews_since(str(yesterday))
        inserted = 0

        for item in items:
            parsed = guardian.parse_review(item)
            if not parsed:
                continue

            film_title = parsed.pop("film_title", "")
            film = _match_review_to_film(film_title)
            if not film:
                continue  # can't link this review to a film we know

            rt_link = film["rotten_tomatoes_link"]

            # if the film was already deactivated, a new review means it's
            # getting attention again (re-release, awards, streaming debut)
            if not film.get("review_fetch_active"):
                db.reactivate_film(rt_link)
                logger.info(
                    f"Auto-reactivated '{film.get('movie_title', rt_link)}' "
                    f"- new Guardian review found"
                )

            db.bulk_insert_reviews([{**parsed, "rotten_tomatoes_link": rt_link}])
            db.mark_reviews_updated(rt_link)
            db.update_last_review_fetch(rt_link)
            inserted += 1

        logger.info(f"[3/4] Inserted {inserted} Guardian reviews")
    else:
        logger.info("[3/4] GUARDIAN_API_KEY not set - skipping Guardian reviews")

    # 4. NYT reviews
    if NYT_API_KEY:
        logger.info("[4/4] Fetching NYT reviews...")
        items    = nyt.get_reviews_since(str(yesterday))
        inserted = 0

        for item in items:
            parsed = nyt.parse_review(item)
            if not parsed:
                continue

            film_title = parsed.pop("film_title", "")
            film = _match_review_to_film(film_title)
            if not film:
                continue

            rt_link = film["rotten_tomatoes_link"]

            if not film.get("review_fetch_active"):
                db.reactivate_film(rt_link)
                logger.info(
                    f"Auto-reactivated '{film.get('movie_title', rt_link)}' "
                    f"- new NYT review found"
                )

            db.bulk_insert_reviews([{**parsed, "rotten_tomatoes_link": rt_link}])
            db.mark_reviews_updated(rt_link)
            db.update_last_review_fetch(rt_link)
            inserted += 1

        logger.info(f"[4/4] Inserted {inserted} NYT reviews")
    else:
        logger.info("[4/4] NYT_API_KEY not set - skipping NYT reviews")

    
    # 5. Deactivate films that have gone quiet
    # Two stopping conditions (whichever comes first):
    #   A) 90-day hard cap from release date AND no API reviews found yet
    #   B) 14 consecutive days with no new reviews (early stopping)        
    # Films reactivated for awards/re-release skip rule A and rely only
    # on rule B, so they stay alive as long as reviews keep arriving.
    
    active = db.get_active_films()
    closed = 0

    for film in active:
        rt_link = film["rotten_tomatoes_link"]

        # parse original release date (may be None for some TMDB entries)
        rel_date = None
        rel = film.get("original_release_date") or ""
        if rel:
            try:
                rel_date = datetime.strptime(str(rel)[:10], "%Y-%m-%d").date()
            except ValueError:
                pass

        # PyMySQL returns DATETIME columns as Python datetime objects
        reviews_updated = film.get("reviews_updated_at")
        if isinstance(reviews_updated, datetime):
            reviews_updated = reviews_updated.date()

        last_fetched = film.get("last_review_fetched_at")
        if isinstance(last_fetched, datetime):
            last_fetched = last_fetched.date()

        should_deactivate = False
        reason = ""

        # rule A: past the 90-day initial window and never received an API review
        # (we skip this for films that were reactivated, since their original
        # release was long ago - rule B handles them instead)
        if rel_date and not reviews_updated:
            if (today - rel_date).days > REVIEW_WINDOW_DAYS:
                should_deactivate = True
                reason = f"past {REVIEW_WINDOW_DAYS}-day window with no API reviews"

        # rule B-1: had API reviews at some point but none in the last 14 days
        elif reviews_updated and (today - reviews_updated).days > EARLY_STOP_DAYS:
            should_deactivate = True
            reason = f"no new reviews for {EARLY_STOP_DAYS}+ consecutive days"

        # rule B-2: been checking daily for 14+ days but never found any API reviews
        elif last_fetched and not reviews_updated and (today - last_fetched).days > EARLY_STOP_DAYS:
            should_deactivate = True
            reason = f"checked daily for {EARLY_STOP_DAYS}+ days without finding reviews"

        if should_deactivate:
            db.deactivate_film(rt_link)
            closed += 1
            logger.debug(
                f"Deactivated '{film.get('movie_title', rt_link)}': {reason}"
            )

    if closed:
        logger.info(f"Deactivated {closed} films (90-day cap or {EARLY_STOP_DAYS}-day early stop)")

    logger.info("=== Daily update complete ===")


def _score_backfill_batch(omdb: OmdbClient, batch_size: int = 200):
    #Scores a small batch of backfill films that don't have OMDb data yet.
    #Called at the end of each daily update so the backlog shrinks gradually
    #without eating into the OMDb quota needed for live film refreshes.
    films = db.get_unscored_tmdb_films(limit=batch_size)
    if not films:
        return

    scored  = 0
    skipped = 0

    for film in films:
        imdb_id = film.get("imdb_id")
        if not imdb_id:
            skipped += 1
            continue

        omdb_data = omdb.get_by_imdb_id(imdb_id)
        if omdb.quota_exhausted:
            logger.warning(f"[backfill] OMDb quota hit — scored {scored} before limit, stopping")
            break
        if not omdb_data or omdb_data.get("Response") != "True":
            # OMDb doesn't know this film — stamp it so it's never retried
            db.stamp_omdb_attempted(film["rotten_tomatoes_link"])
            skipped += 1
            continue

        tomatometer = omdb.extract_rt_score(omdb_data)
        imdb_raw    = omdb_data.get("imdbRating", "")
        imdb_proxy  = round(float(imdb_raw) * 10, 1) if imdb_raw and imdb_raw != "N/A" else None

        if tomatometer is None and imdb_proxy is None:
            # OMDb has the film but no usable scores — stamp and move on
            db.stamp_omdb_attempted(film["rotten_tomatoes_link"])
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

    if scored or skipped:
        logger.info(f"[backfill] Daily scoring batch: {scored} scored, {skipped} skipped")

    remaining = db.count_unscored_tmdb_films()
    if remaining > 0:
        logger.info(f"[backfill] {remaining} films still awaiting OMDb scores")
