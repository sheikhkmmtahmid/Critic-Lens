import logging
import time
import sys
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# add the backend dir to sys.path so local imports work from any cwd
sys.path.insert(0, os.path.dirname(__file__))

import data_loader
import aspect_extractor
import sentiment_analyser
import divergence_engine
import temporal_analyser
import imdb_classifier
import updater
import db

from models import (
    HealthResponse,
    FilmDivergenceResponse,
    TemporalDriftResponse,
    LeaderboardResponse,
    ReviewAnalysisResponse,
    ReviewAnalysisRequest,
    IncrementalTrainRequest,
    IncrementalTrainResponse,
    IMDBEnsemble,
    IMDBSentiment,
    OverviewResponse,
    FilmSearchResult,
    GenreDivergence,
    AspectResult,
)

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_models_loaded = False
_scheduler     = None
_last_update   = None   # ISO timestamp of the most recent completed daily update


# startup / shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _models_loaded, _scheduler

    logger.info("Starting up CriticLens...")

    data_loader.load_data()

    # train IMDB classifiers (TF-IDF + DistilBERT) - blocks until done
    logger.info("--- Step 1/2: Training IMDB sentiment classifiers ---")
    imdb_classifier.load_models()

    # load aspect extraction + per-aspect sentiment models
    logger.info("--- Step 2/2: Loading HuggingFace aspect models ---")
    try:
        aspect_extractor.load_model()
        sentiment_analyser.load_model()
        _models_loaded = True
        logger.info("All models ready - CriticLens is up")
    except Exception as e:
        logger.error(f"Aspect model loading failed: {e}")
        _models_loaded = False

    # schedule the daily update job at 2am every day
    _scheduler = _start_scheduler()

    yield

    # clean shutdown
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    logger.info("Shutting down CriticLens")


def _start_scheduler():
    #Set up APScheduler to call run_daily_update() at 02:00 every day
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger

        sched = BackgroundScheduler(timezone="UTC")
        sched.add_job(
            _run_update_and_reload,
            trigger=CronTrigger(hour=2, minute=0),
            id="daily_update",
            name="Daily film + review update",
            replace_existing=True,
        )
        sched.start()
        logger.info("Scheduler started - daily update job fires at 02:00 UTC")
        return sched
    except Exception as e:
        logger.error(f"Could not start scheduler: {e}")
        return None


def _run_update_and_reload():
    #Wrapper called by the scheduler.
    #Runs the full API update then refreshes the in-memory films DataFrame
    #so new TMDB films appear without a server restart.
    global _last_update
    try:
        updater.run_daily_update()
        data_loader.reload_films()
        _last_update = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        logger.info(f"Daily update completed at {_last_update}")
    except Exception as e:
        logger.error(f"Daily update failed: {e}", exc_info=True)


# app
app = FastAPI(title="CriticLens API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# timing header on every response - useful for profiling slow model endpoints
@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    response.headers["X-Process-Time"] = f"{time.time() - t0:.3f}s"
    return response


# existing endpoints (unchanged)
@app.get("/api/health")
def health():
    return {
        "status":                "ok",
        "models_loaded":         _models_loaded,
        "imdb_tfidf_ready":      imdb_classifier.is_tfidf_ready(),
        "imdb_distilbert_ready": imdb_classifier.is_distilbert_ready(),
    }


@app.get("/api/films/search")
def search_films(q: str = ""):
    if not q:
        return []
    return divergence_engine.search_films(q)


@app.get("/api/films/{rotten_tomatoes_link:path}/divergence")
def get_film_divergence(rotten_tomatoes_link: str):
    result = divergence_engine.compute_film_divergence(rotten_tomatoes_link)
    if result is None:
        raise HTTPException(status_code=404, detail="Film not found")
    return result


@app.get("/api/films/{rotten_tomatoes_link:path}/explain")
def explain_divergence(rotten_tomatoes_link: str):
    result = divergence_engine.explain_divergence(rotten_tomatoes_link)
    if result is None:
        raise HTTPException(status_code=404, detail="Film not found")
    return result


@app.get("/api/films/{rotten_tomatoes_link:path}/temporal")
def get_temporal(rotten_tomatoes_link: str):
    return temporal_analyser.compute_temporal_drift(rotten_tomatoes_link)


@app.get("/api/divergence/leaderboard")
def get_leaderboard():
    return divergence_engine.get_top_divergent_films(n=20)


@app.post("/api/analyse/review", response_model=ReviewAnalysisResponse)
def analyse_review(body: ReviewAnalysisRequest):
    if not _models_loaded:
        raise HTTPException(status_code=503, detail="Models not loaded yet")

    review_text = body.review_text.strip()
    if not review_text:
        raise HTTPException(status_code=400, detail="review_text is empty")

    aspects_dict  = aspect_extractor.extract_aspects(review_text)
    aspect_results = []
    all_sentiments = []

    for asp in aspect_extractor.ASPECT_LABELS:
        confidence = aspects_dict.get(asp, 0.0)

        if confidence > 0.15:
            sent = sentiment_analyser.score_aspect_sentiment(review_text, asp)
            aspect_results.append(AspectResult(
                aspect=asp,
                confidence=confidence,
                sentiment=sent["sentiment"],
                sentiment_confidence=sent["confidence"],
                stars=sent["stars"],
            ))
            all_sentiments.append(sent["sentiment"])
        else:
            aspect_results.append(AspectResult(
                aspect=asp,
                confidence=confidence,
                sentiment="Neutral",
                sentiment_confidence=0.0,
                stars=3,
            ))

    if all_sentiments:
        pos = all_sentiments.count("Positive")
        neg = all_sentiments.count("Negative")
        neu = all_sentiments.count("Neutral")
        if pos > neg and pos > neu:
            overall = "Positive"
        elif neg > pos and neg > neu:
            overall = "Negative"
        else:
            overall = "Mixed"
    else:
        overall = "Mixed"

    imdb_result = None
    if imdb_classifier.is_ready():
        try:
            ensemble = imdb_classifier.predict_ensemble(review_text)
            imdb_result = IMDBEnsemble(
                sentiment=ensemble["sentiment"],
                confidence=ensemble["confidence"],
                agreement=ensemble["agreement"],
                model=ensemble["model"],
                tfidf=IMDBSentiment(**ensemble["tfidf"]),
                distilbert=IMDBSentiment(**ensemble["distilbert"]),
            )
        except Exception as e:
            logger.warning(f"IMDB ensemble failed: {e}")

    return ReviewAnalysisResponse(
        aspects=aspect_results,
        overall_sentiment=overall,
        imdb_sentiment=imdb_result,
    )


@app.post("/api/train/review", response_model=IncrementalTrainResponse)
def train_review(body: IncrementalTrainRequest):
    if not imdb_classifier.is_ready():
        raise HTTPException(status_code=503, detail="IMDB models not loaded yet")

    sentiment = body.sentiment.strip().lower()
    if sentiment not in ("positive", "negative"):
        raise HTTPException(status_code=400, detail="sentiment must be 'positive' or 'negative'")

    review_text = body.review_text.strip()
    if not review_text:
        raise HTTPException(status_code=400, detail="review_text is empty")

    try:
        result = imdb_classifier.incremental_update(review_text, sentiment)
        msg = "TF-IDF updated immediately."
        if result["distilbert_updated"]:
            msg += " DistilBERT re-trained on accumulated buffer."
        else:
            remaining = result["buffer_threshold"] - result["buffer_size"]
            msg += (
                f" DistilBERT buffer: {result['buffer_size']}/{result['buffer_threshold']}"
                f" ({remaining} more needed to trigger re-training)."
            )
        return IncrementalTrainResponse(
            success=True,
            message=msg,
            tfidf_updated=result["tfidf_updated"],
            distilbert_updated=result["distilbert_updated"],
            buffer_size=result["buffer_size"],
            buffer_threshold=result["buffer_threshold"],
        )
    except Exception as e:
        logger.error(f"Incremental training failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/models/accuracy")
def get_model_accuracy():
    report = imdb_classifier.get_accuracy_report()
    if not report:
        return {"message": "Accuracy report not yet generated - models may still be training"}
    return report


@app.get("/api/films/browse")
def browse_films(
    from_year: int = None,
    to_year: int = None,
    sort_by: str = "divergence",
    page: int = 1,
    per_page: int = 20,
):
    #Browse films with optional year range + sort.
    #Returns paginated results - the dataset is 17k rows so we never want
    #to dump all of them to the frontend at once.
    movies_df = data_loader.get_movies_df()
    if movies_df is None:
        raise HTTPException(status_code=503, detail="Data not loaded")

    df = movies_df  # filtering creates new views, no copy needed yet

    if from_year is not None:
        df = df[df["release_year"].notna() & (df["release_year"] >= from_year)]
    if to_year is not None:
        df = df[df["release_year"].notna() & (df["release_year"] <= to_year)]

    if sort_by == "divergence":
        # most polarising films first (highest absolute gap)
        df = df.copy()
        df["_abs_div"] = df["divergence_score"].abs()
        df = df.sort_values("_abs_div", ascending=False, na_position="last").drop(columns=["_abs_div"])
    elif sort_by == "critics":
        df = df.sort_values("tomatometer_rating", ascending=False, na_position="last")
    elif sort_by == "audience":
        df = df.sort_values("audience_rating", ascending=False, na_position="last")
    elif sort_by == "newest":
        df = df.sort_values("release_year", ascending=False, na_position="last")
    elif sort_by == "oldest":
        df = df.sort_values("release_year", ascending=True, na_position="last")

    total   = len(df)
    per_page = max(1, min(per_page, 100))
    page    = max(1, page)
    start   = (page - 1) * per_page
    page_df = df.iloc[start:start + per_page]

    def _str(val):
        return val if (val is not None and pd.notna(val)) else None

    films = []
    for _, row in page_df.iterrows():
        ry  = row.get("release_year")
        rt  = row.get("tomatometer_rating")
        aud = row.get("audience_rating")
        div = row.get("divergence_score")
        films.append({
            "rotten_tomatoes_link": _str(row.get("rotten_tomatoes_link")),
            "movie_title":          _str(row.get("movie_title")),
            "release_year":         int(ry)    if ry  is not None and pd.notna(ry)  else None,
            "genres":               _str(row.get("genres")),
            "tomatometer_rating":   float(rt)  if rt  is not None and pd.notna(rt)  else None,
            "audience_rating":      float(aud) if aud is not None and pd.notna(aud) else None,
            "divergence_score":     float(div) if div is not None and pd.notna(div) else None,
            "divergence_label":     _str(row.get("divergence_label")),
        })

    return {
        "films":       films,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": (total + per_page - 1) // per_page if total > 0 else 0,
    }


@app.get("/api/stats/overview", response_model=OverviewResponse)
def get_overview():
    movies_df = data_loader.get_movies_df()

    if movies_df is None:
        raise HTTPException(status_code=503, detail="Data not loaded")

    total_films = len(movies_df)

    valid   = movies_df.dropna(subset=["divergence_score"])
    avg_div = float(valid["divergence_score"].mean()) if len(valid) > 0 else None

    critic_film   = None
    audience_film = None

    if len(valid) > 0:
        critic_row    = valid.loc[valid["divergence_score"].idxmax()]
        critic_film   = str(critic_row["movie_title"]) if pd.notna(critic_row.get("movie_title")) else None
        audience_row  = valid.loc[valid["divergence_score"].idxmin()]
        audience_film = str(audience_row["movie_title"]) if pd.notna(audience_row.get("movie_title")) else None

    genre_avgs = []
    try:
        genre_df = movies_df.dropna(subset=["genres", "divergence_score"]).copy()
        genre_df["genre_list"] = genre_df["genres"].str.split(",")
        exploded = genre_df.explode("genre_list")
        exploded["genre_clean"] = exploded["genre_list"].str.strip()
        genre_stats = (
            exploded.groupby("genre_clean")["divergence_score"]
            .agg(["mean", "count"])
            .reset_index()
        )
        genre_stats = genre_stats[genre_stats["count"] >= 5]
        genre_stats = genre_stats.nlargest(8, "mean")
        for _, row in genre_stats.iterrows():
            genre_avgs.append(GenreDivergence(
                genre=str(row["genre_clean"]),
                avg_divergence=float(row["mean"]),
            ))
    except Exception as e:
        logger.warning(f"Genre stats failed: {e}")

    return OverviewResponse(
        total_films=total_films,
        avg_divergence=avg_div,
        most_divergent_critic_film=critic_film,
        most_divergent_audience_film=audience_film,
        genre_divergence_averages=genre_avgs,
    )


# new admin endpoints
@app.post("/api/admin/trigger-update")
def trigger_update(background_tasks: BackgroundTasks):
    #Manually kick off a full update cycle without waiting for the 2am cron.
    #The update runs in the background so this endpoint returns immediately.
    background_tasks.add_task(_run_update_and_reload)
    return {"message": "Update started in background"}


@app.post("/api/admin/reactivate/{rotten_tomatoes_link:path}")
def reactivate_film(rotten_tomatoes_link: str):
    #Manually re-open review fetching for a film.
    #Use this when a film gets an Oscar nomination, re-release, or streaming
    #debut and you want to capture the new wave of reviews immediately.
    #The film stays active until 14 consecutive days pass with no new reviews.
    film = db.get_film(rotten_tomatoes_link)
    if not film:
        raise HTTPException(status_code=404, detail="Film not found")

    db.reactivate_film(rotten_tomatoes_link)
    return {
        "message":  f"'{film.get('movie_title', rotten_tomatoes_link)}' reactivated",
        "rt_link":  rotten_tomatoes_link,
        "note":     f"Will deactivate automatically after {updater.EARLY_STOP_DAYS} days with no new reviews",
    }


@app.get("/api/admin/update-status")
def update_status():
    #Show when the last daily update ran and whether the scheduler is alive
    scheduler_running = _scheduler is not None and _scheduler.running

    next_run = None
    if scheduler_running:
        try:
            job      = _scheduler.get_job("daily_update")
            next_run = str(job.next_run_time) if job and job.next_run_time else None
        except Exception:
            pass

    return {
        "scheduler_running": scheduler_running,
        "last_update":       _last_update,
        "next_scheduled_run":next_run,
        "tmdb_configured":   bool(updater.TMDB_API_KEY),
        "omdb_configured":   bool(updater.OMDB_API_KEY),
        "guardian_configured":bool(updater.GUARDIAN_API_KEY),
        "nyt_configured":    bool(updater.NYT_API_KEY),
    }


def _run_backlog_scoring():
    #Scores up to 800 unscored backfill films via OMDb. Runs in a background task
    try:
        from updater import OmdbClient, _score_backfill_batch, OMDB_API_KEY
        if not OMDB_API_KEY:
            logger.warning("score-backlog: OMDB_API_KEY not set, skipping")
            return
        omdb = OmdbClient()
        _score_backfill_batch(omdb, batch_size=800)
    except Exception as e:
        logger.error(f"Manual backlog scoring failed: {e}", exc_info=True)


@app.post("/api/admin/score-backlog")
def score_backlog(background_tasks: BackgroundTasks):
    #Immediately kick off a backlog scoring batch without waiting for 2am
    background_tasks.add_task(_run_backlog_scoring)
    return {"message": "Scoring batch started — watch the Scored count on the progress page"}


@app.get("/api/admin/backfill-progress")
def backfill_progress():
    """
    Returns live progress for the backfill script.
    Combines:
      - the JSON progress file written by _ProgressTracker (per-month detail)
      - live DB counts from get_backfill_stats() so the numbers update even
        if the running script pre-dates the progress file (like the current run)
    """
    import json as _json

    backfill_file = os.path.join(os.path.dirname(__file__), "backfill_progress.json")
    review_file   = os.path.join(os.path.dirname(__file__), "review_backfill_progress.json")

    def _load_json(path):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return _json.load(fh)
            except Exception:
                pass
        return None

    file_data    = _load_json(backfill_file)
    review_data  = _load_json(review_file)

    try:
        db_stats = db.get_backfill_stats()
    except Exception:
        db_stats = {}

    try:
        review_stats = db.get_review_backfill_stats()
    except Exception:
        review_stats = {}

    return {
        "file_progress":          file_data,
        "review_progress":        review_data,
        "db_stats":               db_stats,
        "review_db_stats":        review_stats,
        "progress_file_exists":   file_data is not None,
        "review_file_exists":     review_data is not None,
    }


# SPA fallback - must be last so API routes above take precedence
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
