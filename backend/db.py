"""
MySQL persistence layer for CriticLens.

All raw SQL lives here so the rest of the codebase stays clean.
Designed to work with local MySQL in dev and TiDB Cloud in production
(TiDB is fully MySQL-compatible, just needs SSL for remote connections).

Required environment variables:
    DB_HOST       - hostname (default: localhost)
    DB_PORT       - port     (default: 3306, TiDB Cloud uses 4000)
    DB_NAME       - database/schema name (default: criticlens)
    DB_USER       - username  (default: root)
    DB_PASSWORD   - password  (default: empty)
    DB_SSL_CA     - path to CA cert PEM file (only needed for TiDB Cloud / remote SSL)

TiDB Cloud tip: download the CA cert from your cluster's connection settings,
save it as ca.pem, then set DB_SSL_CA=/path/to/ca.pem.
"""

import os
import json
import logging
from contextlib import contextmanager

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

# read connection settings from environment so nothing is hard-coded
DB_HOST     = os.environ.get("DB_HOST",     "localhost")
DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
DB_NAME     = os.environ.get("DB_NAME",     "criticlens")
DB_USER     = os.environ.get("DB_USER",     "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_SSL_CA   = os.environ.get("DB_SSL_CA",   "")


def _connect() -> pymysql.Connection:
    ssl_params = {}
    if DB_SSL_CA and os.path.exists(DB_SSL_CA):
        ssl_params = {"ca": DB_SSL_CA}

    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        ssl=ssl_params if ssl_params else None,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=10,
    )


@contextmanager
def _db():
    #Open a connection, yield it, commit on success, rollback on error.
    #Each operation gets its own connection so there are no cross-thread sharing issues.
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# schema
def init_db():
    #Create the database schema if it does not exist yet.
    #Safe to call on every startup - uses CREATE TABLE IF NOT EXISTS throughout.
    # each CREATE TABLE is a separate execute because MySQL does not support
    # executing multiple statements in one call unless multi=True (avoid that)
    statements = [
        """
        CREATE TABLE IF NOT EXISTS films (
            id                      INT             AUTO_INCREMENT PRIMARY KEY,
            rotten_tomatoes_link    VARCHAR(512)    NOT NULL,
            movie_title             TEXT,
            genres                  TEXT,
            directors               TEXT,
            runtime                 FLOAT,
            original_release_date   VARCHAR(20),
            release_year            INT,
            tomatometer_rating      FLOAT,
            audience_rating         FLOAT,
            divergence_score        FLOAT,
            divergence_label        VARCHAR(100),
            critics_consensus       TEXT,
            tmdb_id                 INT,
            imdb_id                 VARCHAR(20),
            poster_url              TEXT,
            source                  VARCHAR(20)     DEFAULT 'kaggle',
            review_fetch_active     TINYINT(1)      DEFAULT 0,
            last_review_fetched_at  DATETIME,
            reviews_updated_at      DATETIME,
            trend_cached            MEDIUMTEXT,
            trend_analysed_at       DATETIME,
            omdb_last_fetched_at    DATETIME,
            inserted_at             DATETIME        DEFAULT CURRENT_TIMESTAMP,
            updated_at              DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_rt_link (rotten_tomatoes_link(255))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS critic_reviews (
            id                          INT             AUTO_INCREMENT PRIMARY KEY,
            rotten_tomatoes_link        VARCHAR(512)    NOT NULL,
            critic_name                 VARCHAR(255),
            publisher_name              VARCHAR(255),
            review_date                 VARCHAR(20),
            review_type                 VARCHAR(20),
            review_score                VARCHAR(20),
            review_content              MEDIUMTEXT,
            sentiment_fast_label        VARCHAR(20),
            sentiment_fast_confidence   FLOAT,
            sentiment_deep_label        VARCHAR(20),
            sentiment_deep_confidence   FLOAT,
            source                      VARCHAR(20)     DEFAULT 'kaggle',
            external_id                 VARCHAR(64),
            inserted_at                 DATETIME        DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_review (rotten_tomatoes_link(255), source, external_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE INDEX idx_reviews_link
            ON critic_reviews(rotten_tomatoes_link(255))
        """,
        """
        CREATE INDEX idx_films_active
            ON films(review_fetch_active, original_release_date)
        """,
    ]

    with _db() as conn:
        with conn.cursor() as cur:
            for sql in statements:
                try:
                    cur.execute(sql)
                except (pymysql.err.OperationalError, pymysql.err.ProgrammingError) as e:
                    # 1061 = duplicate key name (index/table already exists) - safe to ignore
                    if e.args[0] not in (1061, 1050):
                        raise

    logger.info(f"MySQL schema ready (host={DB_HOST}, db={DB_NAME})")
    _ensure_schema_updates()


def _ensure_schema_updates():
    #Add columns introduced after the initial schema was deployed.
    #MySQL has no "ALTER TABLE ... ADD COLUMN IF NOT EXISTS" syntax, so we
    #catch error 1060 (duplicate column name) and treat it as success.
    additions = [
        # marks audience_rating as an IMDB proxy rather than a real RT score
        # values: 'imdb' for backfill/TMDB films, NULL for Kaggle films
        "ALTER TABLE films ADD COLUMN audience_score_source VARCHAR(20) DEFAULT NULL",
    ]
    with _db() as conn:
        with conn.cursor() as cur:
            for sql in additions:
                try:
                    cur.execute(sql)
                except (pymysql.err.OperationalError, pymysql.err.ProgrammingError) as e:
                    if e.args[0] != 1060:  # 1060 = duplicate column name, safe to ignore
                        raise


# bulk writes
def is_empty() -> bool:
    """True if the films table has no rows - used to decide whether migration is needed."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM films")
            row = cur.fetchone()
            return (row["cnt"] == 0) if row else True


def bulk_insert_films(films: list):
    #Insert films, silently skipping any that already exist
    #(INSERT IGNORE respects the UNIQUE KEY on rotten_tomatoes_link).
    if not films:
        return

    with _db() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT IGNORE INTO films (
                    rotten_tomatoes_link, movie_title, genres, directors,
                    runtime, original_release_date, release_year,
                    tomatometer_rating, audience_rating, divergence_score,
                    divergence_label, critics_consensus, source, review_fetch_active
                ) VALUES (
                    %(rotten_tomatoes_link)s, %(movie_title)s, %(genres)s, %(directors)s,
                    %(runtime)s, %(original_release_date)s, %(release_year)s,
                    %(tomatometer_rating)s, %(audience_rating)s, %(divergence_score)s,
                    %(divergence_label)s, %(critics_consensus)s, %(source)s, %(review_fetch_active)s
                )
            """, films)


def bulk_insert_reviews(reviews: list):
    #INSERT IGNORE - safe to call with duplicates because the UNIQUE KEY on
    #(rotten_tomatoes_link, source, external_id) prevents them from landing twice.
    if not reviews:
        return

    with _db() as conn:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT IGNORE INTO critic_reviews (
                    rotten_tomatoes_link, critic_name, publisher_name,
                    review_date, review_type, review_score, review_content,
                    source, external_id
                ) VALUES (
                    %(rotten_tomatoes_link)s, %(critic_name)s, %(publisher_name)s,
                    %(review_date)s, %(review_type)s, %(review_score)s, %(review_content)s,
                    %(source)s, %(external_id)s
                )
            """, reviews)


# single-film upsert (daily updater for new TMDB films)
def upsert_film(film: dict):
    # Insert a new film or update its metadata if the rt_link already exists.
    # COALESCE on score fields means a NULL from the caller never wipes out
    # a real score that's already in the DB (e.g. backfill insert won't null
    # out Kaggle scores, and a failed OMDb call won't clear a prior result).
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO films (
                    rotten_tomatoes_link, movie_title, genres, directors,
                    runtime, original_release_date, release_year,
                    tomatometer_rating, audience_rating, divergence_score,
                    divergence_label, critics_consensus, tmdb_id, imdb_id,
                    poster_url, source, review_fetch_active, omdb_last_fetched_at,
                    audience_score_source
                ) VALUES (
                    %(rotten_tomatoes_link)s, %(movie_title)s, %(genres)s, %(directors)s,
                    %(runtime)s, %(original_release_date)s, %(release_year)s,
                    %(tomatometer_rating)s, %(audience_rating)s, %(divergence_score)s,
                    %(divergence_label)s, %(critics_consensus)s, %(tmdb_id)s, %(imdb_id)s,
                    %(poster_url)s, %(source)s, %(review_fetch_active)s, %(omdb_last_fetched_at)s,
                    %(audience_score_source)s
                )
                ON DUPLICATE KEY UPDATE
                    tomatometer_rating    = COALESCE(VALUES(tomatometer_rating),    tomatometer_rating),
                    audience_rating       = COALESCE(VALUES(audience_rating),       audience_rating),
                    divergence_score      = COALESCE(VALUES(divergence_score),      divergence_score),
                    divergence_label      = COALESCE(VALUES(divergence_label),      divergence_label),
                    poster_url            = COALESCE(VALUES(poster_url),            poster_url),
                    omdb_last_fetched_at  = COALESCE(VALUES(omdb_last_fetched_at),  omdb_last_fetched_at),
                    audience_score_source = COALESCE(VALUES(audience_score_source), audience_score_source)
            """, {
                "rotten_tomatoes_link": film.get("rotten_tomatoes_link"),
                "movie_title":          film.get("movie_title"),
                "genres":               film.get("genres"),
                "directors":            film.get("directors"),
                "runtime":              film.get("runtime"),
                "original_release_date":film.get("original_release_date"),
                "release_year":         film.get("release_year"),
                "tomatometer_rating":   film.get("tomatometer_rating"),
                "audience_rating":      film.get("audience_rating"),
                "divergence_score":     film.get("divergence_score"),
                "divergence_label":     film.get("divergence_label"),
                "critics_consensus":    film.get("critics_consensus"),
                "tmdb_id":              film.get("tmdb_id"),
                "imdb_id":              film.get("imdb_id"),
                "poster_url":           film.get("poster_url"),
                "source":                film.get("source", "tmdb"),
                "review_fetch_active":   film.get("review_fetch_active", 0),
                "omdb_last_fetched_at":  film.get("omdb_last_fetched_at"),
                "audience_score_source": film.get("audience_score_source"),
            })


# reads
def get_film(rt_link: str) -> dict | None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM films WHERE rotten_tomatoes_link = %s", (rt_link,)
            )
            return cur.fetchone()


def get_reviews_for_film(rt_link: str) -> list:
    #All reviews for one film, newest first.
    #Includes cached IMDB sentiment columns - those will be None if not yet computed.
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM critic_reviews
                WHERE rotten_tomatoes_link = %s
                ORDER BY review_date DESC
            """, (rt_link,))
            return cur.fetchall()


def get_all_films_as_dicts() -> list:
    #Pull every film row at startup to build the in-memory DataFrame.
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM films")
            return cur.fetchall()


def find_film_by_title(title: str) -> dict | None:
    #Case-insensitive exact title match  used when linking Guardian/NYT reviews to known films
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM films
                WHERE LOWER(movie_title) = LOWER(%s)
                LIMIT 1
            """, (title,))
            return cur.fetchone()


def get_active_films() -> list:
    #Films still inside their 90-day review-fetch window, oldest-fetched first
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM films
                WHERE review_fetch_active = 1
                ORDER BY last_review_fetched_at ASC
            """)
            return cur.fetchall()


# sentiment cache
def update_review_sentiment(review_id: int, fast: dict, deep: dict):
    #Write the IMDB classifier result back to the review row.
    #Next time this film is opened, divergence_engine reads these columns
    #instead of re-running the model.
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE critic_reviews SET
                    sentiment_fast_label      = %s,
                    sentiment_fast_confidence = %s,
                    sentiment_deep_label      = %s,
                    sentiment_deep_confidence = %s
                WHERE id = %s
            """, (
                fast.get("sentiment"),
                fast.get("confidence"),
                deep.get("sentiment") if deep else None,
                deep.get("confidence") if deep else None,
                review_id,
            ))


# temporal trend cache
def get_cached_trend(rt_link: str) -> dict | None:
    #Return the cached trend blob if still valid.
    #A trend is stale when reviews_updated_at is newer than trend_analysed_at
    #(meaning new reviews arrived after the last computation)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT trend_cached, trend_analysed_at, reviews_updated_at
                FROM films
                WHERE rotten_tomatoes_link = %s
            """, (rt_link,))
            row = cur.fetchone()

    if not row or not row.get("trend_cached"):
        return None

    analysed = row.get("trend_analysed_at")
    updated  = row.get("reviews_updated_at")

    # new reviews came in after the last trend run - need to recompute
    if updated and analysed and updated > analysed:
        return None

    try:
        return json.loads(row["trend_cached"])
    except (json.JSONDecodeError, TypeError):
        return None


def save_cached_trend(rt_link: str, trend_data: dict):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE films SET
                    trend_cached      = %s,
                    trend_analysed_at = NOW()
                WHERE rotten_tomatoes_link = %s
            """, (json.dumps(trend_data), rt_link))


def mark_reviews_updated(rt_link: str):
    #Bump reviews_updated_at so the trend cache knows it is stale.
    #Called after every batch of new reviews for a film.
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE films SET reviews_updated_at = NOW()
                WHERE rotten_tomatoes_link = %s
            """, (rt_link,))


# review window management
def deactivate_film(rt_link: str):
    #Close the review-fetch window - no more daily pulls for this film
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE films SET review_fetch_active = 0
                WHERE rotten_tomatoes_link = %s
            """, (rt_link,))


def reactivate_film(rt_link: str):
    """
    Re-open the review-fetch window for a film that was already deactivated.
    Used when a film gets a re-release, award nomination, or streaming debut
    and starts attracting new reviews again.

    The effective new window length is not stored here - it comes from the
    early-stopping rule in updater.py: the film stays active until 14 days
    pass without a new review, so buzz that lasts a week keeps it alive,
    and buzz that fades overnight closes it quickly.
    """
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE films SET
                    review_fetch_active    = 1,
                    reviews_updated_at     = NOW(),
                    last_review_fetched_at = NULL
                WHERE rotten_tomatoes_link = %s
            """, (rt_link,))


def update_last_review_fetch(rt_link: str):
    #Record when we last checked for new reviews (even when none were found)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE films SET last_review_fetched_at = NOW()
                WHERE rotten_tomatoes_link = %s
            """, (rt_link,))


# backfill helpers
def get_unscored_tmdb_films(limit: int = 200) -> list:
    #Returns films with an imdb_id that have never had an OMDb call attempted.
    #omdb_last_fetched_at being NULL is the "never tried" marker — avoids
    #re-queuing films where OMDb had no RT score (tomatometer stays NULL but
    #the call was made, so we shouldn't keep retrying them forever).
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, rotten_tomatoes_link, movie_title, imdb_id
                FROM films
                WHERE source IN ('backfill', 'tmdb')
                AND tomatometer_rating IS NULL
                AND imdb_id IS NOT NULL
                AND omdb_last_fetched_at IS NULL
                ORDER BY release_year DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()


def get_backfill_stats() -> dict:
    #Single-query summary used by the backfill progress page.
    #Counts films by source so we can show how Phase 1 and Phase 2 are going.
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                                              AS total_films,
                    SUM(CASE WHEN source = 'kaggle'                               THEN 1 ELSE 0 END) AS kaggle_films,
                    SUM(CASE WHEN source IN ('backfill', 'tmdb')                  THEN 1 ELSE 0 END) AS new_films,
                    SUM(CASE WHEN source IN ('backfill', 'tmdb')
                             AND tomatometer_rating IS NOT NULL                   THEN 1 ELSE 0 END) AS new_films_scored,
                    SUM(CASE WHEN source IN ('backfill', 'tmdb')
                             AND tomatometer_rating IS NULL
                             AND imdb_id IS NOT NULL
                             AND omdb_last_fetched_at IS NULL                     THEN 1 ELSE 0 END) AS new_films_unscored
                FROM films
            """)
            row = cur.fetchone()
            return dict(row) if row else {}


def count_unscored_tmdb_films() -> int:
    #Films that have an imdb_id but have never had an OMDb call attempted yet.
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM films
                WHERE source IN ('backfill', 'tmdb')
                AND tomatometer_rating IS NULL
                AND imdb_id IS NOT NULL
                AND omdb_last_fetched_at IS NULL
            """)
            row = cur.fetchone()
            return row["cnt"] if row else 0


def update_backfill_scores(rt_link: str, tomatometer_rating, audience_rating,
                           audience_score_source: str, divergence_score, divergence_label: str):
    #Write OMDb-derived scores to a backfill film.
    #audience_score_source should be 'imdb' since we use IMDB rating × 10 as
    #the audience proxy (RT Audience Score is not available via free APIs).
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE films SET
                    tomatometer_rating    = %s,
                    audience_rating       = %s,
                    audience_score_source = %s,
                    divergence_score      = %s,
                    divergence_label      = %s,
                    omdb_last_fetched_at  = NOW()
                WHERE rotten_tomatoes_link = %s
            """, (tomatometer_rating, audience_rating, audience_score_source,
                  divergence_score, divergence_label, rt_link))


def get_review_backfill_stats() -> dict:
    #Count historical reviews inserted by the review backfill script (post-2020).
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    SUM(CASE WHEN source = 'guardian' AND review_date >= '2020-10-01' THEN 1 ELSE 0 END) AS guardian_reviews,
                    SUM(CASE WHEN source = 'nyt'      AND review_date >= '2020-10-01' THEN 1 ELSE 0 END) AS nyt_reviews,
                    COUNT(DISTINCT CASE WHEN source IN ('guardian','nyt')
                                        AND review_date >= '2020-10-01'
                                        THEN rotten_tomatoes_link END)                                   AS films_with_reviews
                FROM critic_reviews
            """)
            row = cur.fetchone()
            return {k: (int(v) if v is not None else 0) for k, v in row.items()} if row else {}


def stamp_omdb_attempted(rt_link: str):
    #Mark a film as "OMDb was called but returned nothing useful" so it is
    #never re-queued. Sets omdb_last_fetched_at without touching scores.
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE films SET omdb_last_fetched_at = NOW()
                WHERE rotten_tomatoes_link = %s
            """, (rt_link,))
