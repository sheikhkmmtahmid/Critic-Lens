"""
One-time CSV-to-MySQL migration.

Reads the Kaggle RT CSVs and loads them into the films and critic_reviews tables.
Runs only when the films table is empty, so it is safe to call on every startup.

Why chunked inserts instead of pandas to_sql():
    to_sql() needs SQLAlchemy which adds another big dependency and does not
    give us INSERT IGNORE. Chunked executemany with our existing db helpers
    keeps things self-contained and re-run safe.
"""

import os
import hashlib
import logging
import pandas as pd
from datetime import date, timedelta

import db

logger = logging.getLogger(__name__)

DATA_DIR       = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
REVIEW_WINDOW  = 90   # films released within this many days get review_fetch_active=1
CHUNK_FILMS    = 500  # rows per INSERT batch for films
CHUNK_REVIEWS  = 2000 # rows per INSERT batch for reviews (bigger = faster migration)


# helpers
def _review_hash(rt_link: str, critic: str, review_date: str, content: str) -> str:
    #Stable 20-char ID for a Kaggle review, derived from its content.
    #Using a hash (not the row index) means the ID survives CSV re-downloads.
    raw = f"{rt_link}|{critic}|{review_date}|{(content or '')[:80]}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _safe_float(val) -> float | None:
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> int | None:
    try:
        f = float(val)
        return None if pd.isna(f) else int(f)
    except (TypeError, ValueError):
        return None


def _safe_str(val) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    return s if s else None


def _classify_divergence(score: float | None) -> str:
    if score is None:
        return "Aligned"
    if score > 20:
        return "Critics loved it, audiences didn't"
    if score < -20:
        return "Audiences loved it, critics didn't"
    return "Aligned"


# main entry point
def run_migration():
    #Migrate RT CSVs to MySQL.
    #Skips silently if the films table already has data.
    if not db.is_empty():
        logger.info("Database already populated - skipping CSV migration")
        return

    movies_path  = os.path.join(DATA_DIR, "rotten_tomatoes_movies.csv")
    reviews_path = os.path.join(DATA_DIR, "rotten_tomatoes_critic_reviews.csv")

    if not os.path.exists(movies_path):
        logger.error("rotten_tomatoes_movies.csv not found - cannot migrate")
        return

    logger.info("=== Starting one-time CSV migration ===")
    _migrate_films(movies_path)

    if os.path.exists(reviews_path):
        _migrate_reviews(reviews_path)
    else:
        logger.warning("rotten_tomatoes_critic_reviews.csv not found - skipping reviews migration")

    logger.info("=== CSV migration complete ===")


def _migrate_films(path: str):
    logger.info(f"Reading {path}...")
    df = pd.read_csv(path, low_memory=False)

    today  = date.today()
    cutoff = today - timedelta(days=REVIEW_WINDOW)

    # figure out which column holds runtime - the dataset uses two different names
    runtime_col = "runtime_in_minutes" if "runtime_in_minutes" in df.columns else "runtime"

    # parse dates so we can compute release_year and decide review_fetch_active
    df["_release_dt"] = pd.to_datetime(df["original_release_date"], errors="coerce")
    df["release_year"] = df["_release_dt"].dt.year

    df["tomatometer_rating"] = pd.to_numeric(df["tomatometer_rating"], errors="coerce")
    df["audience_rating"]    = pd.to_numeric(df["audience_rating"],    errors="coerce")
    df["divergence_score"]   = df["tomatometer_rating"] - df["audience_rating"]

    records = []
    skipped = 0

    for _, row in df.iterrows():
        rt_link = _safe_str(row.get("rotten_tomatoes_link"))
        if not rt_link:
            skipped += 1
            continue

        rel_date = row["_release_dt"]
        rel_str  = rel_date.strftime("%Y-%m-%d") if pd.notna(rel_date) else None
        # films released recently enough should get their review window opened
        active = 1 if (pd.notna(rel_date) and rel_date.date() >= cutoff) else 0

        div_score = _safe_float(row.get("divergence_score"))

        records.append({
            "rotten_tomatoes_link": rt_link,
            "movie_title":          _safe_str(row.get("movie_title")),
            "genres":               _safe_str(row.get("genres")),
            "directors":            _safe_str(row.get("directors")),
            "runtime":              _safe_float(row.get(runtime_col)),
            "original_release_date":rel_str,
            "release_year":         _safe_int(row.get("release_year")),
            "tomatometer_rating":   _safe_float(row.get("tomatometer_rating")),
            "audience_rating":      _safe_float(row.get("audience_rating")),
            "divergence_score":     div_score,
            "divergence_label":     _classify_divergence(div_score),
            "critics_consensus":    _safe_str(row.get("critics_consensus")),
            "source":               "kaggle",
            "review_fetch_active":  active,
        })

    logger.info(f"Inserting {len(records)} films ({skipped} rows skipped - missing rt_link)...")

    for i in range(0, len(records), CHUNK_FILMS):
        db.bulk_insert_films(records[i : i + CHUNK_FILMS])
        if i % 5000 == 0 and i > 0:
            logger.info(f"  films: {i}/{len(records)}")

    logger.info(f"Films done: {len(records)} inserted")


def _migrate_reviews(path: str):
    logger.info(f"Reading {path} (this takes a moment for ~1M rows)...")
    df = pd.read_csv(path, low_memory=False)
    df = df.fillna("")  # replace NaN with empty string so _safe_str works cleanly

    total   = len(df)
    records = []
    inserted_total = 0

    for idx, row in df.iterrows():
        rt_link = str(row.get("rotten_tomatoes_link", "")).strip()
        if not rt_link:
            continue

        content    = str(row.get("review_content", ""))
        critic     = str(row.get("critic_name",    ""))
        rev_date   = str(row.get("review_date",    ""))
        publisher  = str(row.get("publisher_name", ""))
        rev_type   = str(row.get("review_type",    ""))
        rev_score  = str(row.get("review_score",   ""))

        records.append({
            "rotten_tomatoes_link": rt_link,
            "critic_name":          critic    or None,
            "publisher_name":       publisher or None,
            "review_date":          rev_date  or None,
            "review_type":          rev_type  or None,
            "review_score":         rev_score or None,
            "review_content":       content   or None,
            "source":               "kaggle",
            "external_id":          _review_hash(rt_link, critic, rev_date, content),
        })

        if len(records) >= CHUNK_REVIEWS:
            db.bulk_insert_reviews(records)
            inserted_total += len(records)
            records = []
            if inserted_total % 50000 == 0:
                logger.info(f"  reviews: {inserted_total}/{total}")

    if records:
        db.bulk_insert_reviews(records)
        inserted_total += len(records)

    logger.info(f"Reviews done: {inserted_total} rows processed from {total} CSV rows")
