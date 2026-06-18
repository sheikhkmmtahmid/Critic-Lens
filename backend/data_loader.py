"""
Data loading layer.

What changed from the original CSV-only version:
  - load_data() now initialises MySQL, runs the one-time CSV migration if needed,
    then loads the films DataFrame from MySQL (not directly from the CSV).
  - Reviews are no longer loaded into memory at startup.
    get_reviews_df() returns None (kept for compatibility).
    Use get_reviews_for_film(rt_link) to query per-film from MySQL instead.
  - reload_films() lets the daily updater refresh the in-memory DataFrame
    after inserting new TMDB films, without restarting the app.
"""

import pandas as pd
import os
import logging

import db
import migrate as migration

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

_RT_DATASET   = "stefanoleone992/rotten-tomatoes-movies-and-critic-reviews-dataset"
_IMDB_DATASET = "lakshmi25npathi/imdb-dataset-of-50k-movie-reviews"

_REQUIRED_FILES = {
    "rotten_tomatoes_movies.csv":         _RT_DATASET,
    "rotten_tomatoes_critic_reviews.csv": _RT_DATASET,
    "IMDB Dataset.csv":                   _IMDB_DATASET,
}

# in-memory films DataFrame - 17k rows, fast for search/leaderboard/overview
# reviews are NOT loaded here; they come from MySQL per-film on demand
_movies_df = None


# Kaggle download
def _kaggle_download(dataset_slug: str, dest_dir: str):
    try:
        import kaggle
    except ImportError:
        raise RuntimeError("kaggle package not installed. Run: pip install kaggle")

    logger.info(f"Downloading {dataset_slug} from Kaggle...")
    kaggle.api.authenticate()
    kaggle.api.dataset_download_files(dataset_slug, path=dest_dir, unzip=True, quiet=False)
    logger.info(f"Done: {dataset_slug}")


def ensure_data():
    os.makedirs(DATA_DIR, exist_ok=True)

    missing_by_dataset: dict[str, list[str]] = {}
    for fname, slug in _REQUIRED_FILES.items():
        if not os.path.exists(os.path.join(DATA_DIR, fname)):
            missing_by_dataset.setdefault(slug, []).append(fname)

    if not missing_by_dataset:
        logger.info("All data files present - skipping Kaggle download")
        return

    for slug, files in missing_by_dataset.items():
        logger.info(f"Missing {files} - pulling {slug} from Kaggle")
        _kaggle_download(slug, DATA_DIR)

    for fname in _REQUIRED_FILES:
        fpath = os.path.join(DATA_DIR, fname)
        if not os.path.exists(fpath):
            raise FileNotFoundError(
                f"{fname} still missing after Kaggle download. "
                f"Check your credentials and that the dataset name hasn't changed."
            )


# startup sequence
def load_data():
    db.init_db()

    if db.is_empty():
        ensure_data()
    else:
        logger.info("Database already populated - skipping CSV download")

    migration.run_migration()
    _reload_films_from_db()

    count = len(_movies_df) if _movies_df is not None else 0
    logger.info(f"Loaded {count} films into memory from MySQL")


def _reload_films_from_db():
    #Rebuild the in-memory DataFrame from the MySQL films table
    global _movies_df

    records = db.get_all_films_as_dicts()

    if not records:
        _movies_df = pd.DataFrame()
        return

    _movies_df = pd.DataFrame(records)

    # MySQL returns strings for some numeric columns depending on the driver version
    for col in ("tomatometer_rating", "audience_rating", "divergence_score"):
        if col in _movies_df.columns:
            _movies_df[col] = pd.to_numeric(_movies_df[col], errors="coerce")

    if "release_year" in _movies_df.columns:
        _movies_df["release_year"] = pd.to_numeric(_movies_df["release_year"], errors="coerce")

    if "review_fetch_active" in _movies_df.columns:
        _movies_df["review_fetch_active"] = pd.to_numeric(
            _movies_df["review_fetch_active"], errors="coerce"
        )


def reload_films():
    #Refresh the in-memory DataFrame after the daily updater adds new films.
    #Called by APScheduler after run_daily_update() completes.
    _reload_films_from_db()
    count = len(_movies_df) if _movies_df is not None else 0
    logger.info(f"Films reloaded - {count} total in memory")


# public accessors
def get_movies_df() -> pd.DataFrame | None:
    return _movies_df


def get_reviews_df():
    #Kept for backward compatibility - now always returns None.
    #All review queries go through get_reviews_for_film() instead.
    return None


def get_reviews_for_film(rt_link: str) -> list:
    #Returns all reviews for a film directly from MySQL.
    #Each dict includes pre-computed IMDB sentiment columns
    #(sentiment_fast_label etc.) which may be None if not yet cached.
    return db.get_reviews_for_film(rt_link)


def get_merged_df() -> pd.DataFrame | None:
    # kept for backward compatibility - same as get_movies_df()
    return _movies_df


def is_loaded() -> bool:
    return _movies_df is not None
