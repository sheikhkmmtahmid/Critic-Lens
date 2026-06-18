"""
Historical critic review backfill — Guardian + NYT.

Walks month-by-month from October 2020 (Kaggle dataset cutoff) to today,
pulling film reviews from The Guardian (full body text) and the NYT (abstract).

Each article is matched to a film already in the database by extracting the
film title from the headline.  Uses INSERT IGNORE so re-runs are safe.

Run:
    python -m backend.review_backfill
"""
import json
import logging
import os
import sys
import time
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import db
from updater import (
    _extract_title_from_headline,
    _match_review_to_film,
    _get,
    _QuotaExhausted,
    _AuthError,
    GUARDIAN_API_KEY,
    NYT_API_KEY,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

BACKFILL_START     = date(2020, 10, 1)   # Kaggle dataset ends here
GUARDIAN_DELAY     = 0.5                  # seconds between calls (Guardian free: 12/sec)
NYT_DELAY          = 6.5                  # seconds between calls (NYT free: 10/min)
GUARDIAN_PAGE_SIZE = 50
NYT_PAGE_SIZE      = 10
GUARDIAN_BASE      = "https://content.guardianapis.com/search"
NYT_BASE           = "https://api.nytimes.com/svc/search/v2/articlesearch.json"
PROGRESS_FILE      = Path(__file__).parent / "review_backfill_progress.json"


# Progress tracker
class _Tracker:
    def __init__(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        _default = {
            "started_at": now,
            "updated_at": now,
            "complete":   False,
            "guardian": {
                "status": "pending", "months_done": 0, "months_total": 0,
                "current_month": None, "articles_fetched": 0,
                "reviews_matched": 0, "reviews_inserted": 0,
            },
            "nyt": {
                "status": "pending", "months_done": 0, "months_total": 0,
                "current_month": None, "articles_fetched": 0,
                "reviews_matched": 0, "reviews_inserted": 0,
            },
        }
        # Resume from existing file if present
        if PROGRESS_FILE.exists():
            try:
                saved = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
                self._data = saved
                logger.info("Resuming from existing review_backfill_progress.json")
                return
            except Exception:
                pass
        self._data = _default
        self._save()

    def get(self, source: str, key: str, default=None):
        return self._data.get(source, {}).get(key, default)

    def update(self, source: str, **kwargs):
        self._data[source].update(kwargs)
        self._data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self._save()

    def mark_complete(self):
        self._data["complete"]   = True
        self._data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        self._save()

    def _save(self):
        try:
            PROGRESS_FILE.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Could not write review progress file: {exc}")


# Month list helper
def _months_from(start: date) -> list[tuple[int, int]]:
    today = date.today()
    months = []
    y, m = start.year, start.month
    while date(y, m, 1) <= today:
        months.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


# Guardian backfill
def _guardian_fetch_month(year: int, month: int) -> tuple[int, int, int]:
    #Fetch all Guardian film reviews for one calendar month.
    #Returns (articles_fetched, reviews_matched, reviews_inserted).
    last_day   = monthrange(year, month)[1]
    from_date  = f"{year}-{month:02d}-01"
    to_date    = f"{year}-{month:02d}-{last_day:02d}"

    articles_fetched = reviews_matched = reviews_inserted = 0
    page = 1
    total_pages = 1

    while page <= total_pages:
        data = _get(GUARDIAN_BASE, {
            "api-key":     GUARDIAN_API_KEY,
            "section":     "film",
            "tag":         "film/film",
            "from-date":   from_date,
            "to-date":     to_date,
            "show-fields": "headline,byline,bodyText,starRating",
            "page-size":   GUARDIAN_PAGE_SIZE,
            "page":        page,
            "order-by":    "oldest",
        })

        if not data or data.get("response", {}).get("status") != "ok":
            logger.warning(f"  Guardian: bad response for {from_date} page {page}")
            break

        resp        = data["response"]
        total_pages = resp.get("pages", 1)
        results     = resp.get("results", [])
        articles_fetched += len(results)

        batch = []
        for item in results:
            fields  = item.get("fields", {})
            content = (fields.get("bodyText") or "").strip()
            if not content:
                continue

            headline   = fields.get("headline", "")
            film_title = _extract_title_from_headline(headline)
            film       = _match_review_to_film(film_title)
            if not film:
                continue

            reviews_matched += 1
            stars       = fields.get("starRating")
            review_type = "Fresh" if (not stars or int(stars or 0) >= 3) else "Rotten"

            batch.append({
                "rotten_tomatoes_link": film["rotten_tomatoes_link"],
                "critic_name":          (fields.get("byline") or "The Guardian").strip(),
                "publisher_name":       "The Guardian",
                "review_date":          item.get("webPublicationDate", "")[:10],
                "review_content":       content[:6000],
                "review_type":          review_type,
                "review_score":         str(stars) if stars else None,
                "external_id":          item.get("id", "")[:64],
                "source":               "guardian",
            })

        if batch:
            db.bulk_insert_reviews(batch)
            reviews_inserted += len(batch)

        time.sleep(GUARDIAN_DELAY)
        page += 1

    return articles_fetched, reviews_matched, reviews_inserted


def run_guardian_backfill(tracker: _Tracker):
    if tracker.get("guardian", "status") == "complete":
        logger.info("Guardian backfill already complete — skipping")
        return

    if not GUARDIAN_API_KEY:
        logger.warning("GUARDIAN_API_KEY not set — skipping Guardian backfill")
        tracker.update("guardian", status="skipped")
        return

    months = _months_from(BACKFILL_START)
    already_done = tracker.get("guardian", "months_done", 0)
    tracker.update("guardian", status="running", months_total=len(months))

    totals = {
        "articles_fetched": tracker.get("guardian", "articles_fetched", 0),
        "reviews_matched":  tracker.get("guardian", "reviews_matched",  0),
        "reviews_inserted": tracker.get("guardian", "reviews_inserted", 0),
    }

    for i, (year, month) in enumerate(months, 1):
        if i <= already_done:
            continue
        month_str = f"{year}-{month:02d}"
        logger.info(f"[Guardian {i}/{len(months)}] {month_str}")

        try:
            fetched, matched, inserted = _guardian_fetch_month(year, month)
        except (_QuotaExhausted, _AuthError):
            # Guardian returns 401 for both quota and auth failures
            logger.warning(f"Guardian quota/auth error at {month_str} — progress saved, re-run tomorrow")
            tracker.update("guardian", status="quota_exhausted")
            return

        totals["articles_fetched"] += fetched
        totals["reviews_matched"]  += matched
        totals["reviews_inserted"] += inserted

        tracker.update("guardian", months_done=i, current_month=month_str, **totals)
        logger.info(f"  {month_str}: {fetched} articles, {matched} matched, {inserted} new reviews")

    tracker.update("guardian", status="complete")
    logger.info(f"Guardian done — {totals}")


# NYT backfill
def _nyt_fetch_month(year: int, month: int) -> tuple[int, int, int]:
    #Fetch NYT film reviews for one calendar month.
    #NYT abstract is short (~1-3 sentences) but still useful as a match signal.
    #Returns (articles_fetched, reviews_matched, reviews_inserted).
    last_day   = monthrange(year, month)[1]
    begin_date = f"{year}{month:02d}01"
    end_date   = f"{year}{month:02d}{last_day:02d}"

    articles_fetched = reviews_matched = reviews_inserted = 0
    page = 0

    while True:
        data = _get(NYT_BASE, {
            "api-key":    NYT_API_KEY,
            "fq":         'section_name:"Movies" AND type_of_material:"Review"',
            "begin_date": begin_date,
            "end_date":   end_date,
            "sort":       "oldest",
            "page":       page,
        })

        if not data or data.get("status") != "OK":
            logger.warning(f"  NYT: bad response for {begin_date} page {page}")
            break

        docs = data.get("response", {}).get("docs", [])
        if not docs:
            break

        meta   = data["response"].get("meta", {})
        total  = meta.get("hits", 0)
        articles_fetched += len(docs)

        batch = []
        for item in docs:
            content = (item.get("abstract") or item.get("lead_paragraph") or "").strip()
            if not content:
                continue

            headline  = item.get("headline", {})
            hl_text   = headline.get("main", "") if isinstance(headline, dict) else ""
            film_title = _extract_title_from_headline(hl_text)
            film = _match_review_to_film(film_title)
            if not film:
                continue

            reviews_matched += 1
            byline = item.get("byline") or {}
            import re
            critic = byline.get("original", "") if isinstance(byline, dict) else ""
            critic = re.sub(r"^By\s+", "", critic, flags=re.IGNORECASE).strip()

            batch.append({
                "rotten_tomatoes_link": film["rotten_tomatoes_link"],
                "critic_name":          critic or "NYT",
                "publisher_name":       "New York Times",
                "review_date":          item.get("pub_date", "")[:10],
                "review_content":       content[:6000],
                "review_type":          "Fresh",
                "review_score":         None,
                "external_id":          item.get("_id", "")[:64],
                "source":               "nyt",
            })

        if batch:
            db.bulk_insert_reviews(batch)
            reviews_inserted += len(batch)

        time.sleep(NYT_DELAY)

        # NYT returns 10 per page; stop if we've seen all
        if (page + 1) * NYT_PAGE_SIZE >= total:
            break
        page += 1

    return articles_fetched, reviews_matched, reviews_inserted


def run_nyt_backfill(tracker: _Tracker):
    if tracker.get("nyt", "status") == "complete":
        logger.info("NYT backfill already complete — skipping")
        return

    if not NYT_API_KEY:
        logger.warning("NYT_API_KEY not set — skipping NYT backfill")
        tracker.update("nyt", status="skipped")
        return

    months = _months_from(BACKFILL_START)
    already_done = tracker.get("nyt", "months_done", 0)
    tracker.update("nyt", status="running", months_total=len(months))

    totals = {
        "articles_fetched": tracker.get("nyt", "articles_fetched", 0),
        "reviews_matched":  tracker.get("nyt", "reviews_matched",  0),
        "reviews_inserted": tracker.get("nyt", "reviews_inserted", 0),
    }

    for i, (year, month) in enumerate(months, 1):
        if i <= already_done:
            continue
        month_str = f"{year}-{month:02d}"
        logger.info(f"[NYT {i}/{len(months)}] {month_str}")

        try:
            fetched, matched, inserted = _nyt_fetch_month(year, month)
        except _QuotaExhausted:
            logger.warning(f"NYT daily quota exhausted at {month_str} — progress saved, re-run tomorrow")
            tracker.update("nyt", status="quota_exhausted")
            return
        except _AuthError:
            logger.error("NYT API key rejected (401) — go to developer.nytimes.com, open your app, and enable 'Article Search API', then re-run")
            tracker.update("nyt", status="auth_error")
            return

        totals["articles_fetched"] += fetched
        totals["reviews_matched"]  += matched
        totals["reviews_inserted"] += inserted

        tracker.update("nyt", months_done=i, current_month=month_str, **totals)
        logger.info(f"  {month_str}: {fetched} articles, {matched} matched, {inserted} new reviews")

    tracker.update("nyt", status="complete")
    logger.info(f"NYT done — {totals}")


# Entry point
if __name__ == "__main__":
    db.init_db()
    tracker = _Tracker()

    logger.info("=== Review backfill started ===")
    run_guardian_backfill(tracker)
    run_nyt_backfill(tracker)

    g_done = tracker.get("guardian", "status") in ("complete", "skipped")
    n_done = tracker.get("nyt",      "status") in ("complete", "skipped")
    if g_done and n_done:
        tracker.mark_complete()
        logger.info("=== Review backfill complete ===")
    else:
        logger.info("=== Review backfill paused (quota) — re-run tomorrow to continue ===")
