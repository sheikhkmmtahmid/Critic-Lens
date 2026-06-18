---
title: CriticLens
emoji: 🎬
colorFrom: yellow
colorTo: gray
sdk: docker
pinned: false
---

# CriticLens

A data science side project that digs into the gap between what critics and audiences think about films. Built because I kept noticing that certain films have wildly different scores on Rotten Tomatoes depending on whether you look at the Tomatometer or the audience score, and I wanted to understand why.

The project started as a static analysis of the Kaggle RT dataset, but I ended up building it into something that actually stays current — new films get picked up automatically, reviews come in daily from The Guardian and the New York Times, and the analysis updates as they do.

## What it does

CriticLens analyses critic vs audience sentiment divergence on films. The core idea is that a film with 90% from critics and 40% from audiences tells a completely different story than either number alone. The data lives in MySQL and stays fresh through a daily background job.

**The main features:**

- **Overview dashboard** — aggregate divergence stats, average gap across all films, and a genre breakdown showing which genres consistently have the biggest critic/audience split
- **Film search** — search any film and get a full divergence breakdown: score circles, critics consensus, temporal trend chart, and critic reviews with IMDB-trained sentiment scores on each one
- **Divergence explanation** — for films with a gap larger than 15 points, an explanation appears below the scores built from the film's actual stored reviews. It computes exact sentiment ratios from pre-labelled review data, extracts representative quotes, and generates a template-based summary — no generative AI, just the reviewers' own words and numbers
- **Review Analyser** — paste any review text and get aspect-level sentiment across 8 film aspects (acting, plot, pacing, direction, visuals, dialogue, ending, soundtrack) plus an IMDB-trained ensemble result
- **Leaderboard** — top 10 films in each direction: most critic-favoured and most audience-favoured by divergence score
- **Daily live updates** — new films discovered via TMDB, scores refreshed via OMDb, fresh critic reviews pulled from The Guardian and NYT every morning at 2am UTC

## Where the data comes from

### Films and scores

The base dataset is the [Rotten Tomatoes Movies and Critic Reviews dataset from Kaggle](https://www.kaggle.com/datasets/stefanoleone992/rotten-tomatoes-movies-and-critic-reviews-dataset), which covers roughly 17,000 films up to October 2020 with about 1.1 million critic reviews already bundled in. That gets imported into MySQL once on first startup and the CSVs are no longer touched after that.

For everything released after October 2020, the app discovers films through the [TMDB API](https://developer.themoviedb.org/). It scans month by month from the Kaggle cutoff date up to today, pulling every film it finds. Scores (Tomatometer and audience rating) come from the [OMDb API](https://www.omdbapi.com/), which re-serves the Rotten Tomatoes data. New releases are picked up daily — if a film came out yesterday, it shows up in the database within 24 hours of the next scheduled update.

### Critic reviews

**Pre-2020 films** — reviews come directly from the Kaggle dataset. These are real Rotten Tomatoes critic reviews, already matched to films, covering most significant releases from the 1920s through October 2020.

**Post-2020 films** — reviews come from two sources that get checked every day:

**The Guardian** — uses their [Open Platform API](https://open-platform.theguardian.com/). The app queries the `film` section filtered to the `film/film` tag, pulling review articles published in the past 24 hours. Each article's headline gets parsed to extract the film title, which is then matched against the database. Guardian reviews include the full body text, which means the divergence explanation has actual substantial quotes to work with. The free tier allows 12 requests per second with a daily limit that comfortably covers the volume here.

**New York Times** — uses the [Article Search API](https://developer.nytimes.com/docs/articlesearch-product/1/overview). Same idea — film reviews published yesterday, headline parsed for title, matched to the database. NYT reviews are shorter (abstract + lead paragraph) but the NYT covers a broad range of releases including a lot of prestige and arthouse films that Guardian sometimes misses. The free tier is 10 requests per minute and 4,000 per day, so the daily pull stays well within that.

Both sources run a title extraction step before trying to match. Guardian headlines tend to follow "Film Title review — subtitle" and NYT tends to use "Review: 'Film Title' is..." so there are separate regex patterns for each. Titles that don't match any film in the database get silently dropped rather than inserted with wrong associations.

The **90-day window** controls how long the app keeps checking for new reviews on any given film. When a film is added, it stays active for 90 days from release, or until 14 days pass with no new reviews from either source — whichever comes first. The 14-day early-close exists because some films stop generating coverage after a week, and there's no point making the same API calls every day for nothing. If a film gets re-reviewed later (awards season, streaming release, retrospective), the system automatically re-opens the window.

### Historical reviews for post-2020 films

The daily job only pulls reviews from the past 24 hours, which meant films released between October 2020 and whenever the app was first deployed had no reviews at all. `backend/review_backfill.py` fixes this by walking month-by-month from October 2020 through today and fetching everything the Guardian and NYT published in each month. The same matching logic applies. This is a one-time operation — once it finishes the daily job takes over and nothing needs to run manually again.

## Closing the Kaggle gap (film backfill)

The Kaggle RT dataset covers films up to roughly October 2020. That leaves several years of releases with no OMDb scores. `backend/backfill.py` closes that gap in two phases:

**Phase 1 — Discovery:** iterates month-by-month from October 2020 to today via the TMDB API. Every film found that isn't already in the database is inserted. On a first run this adds a few thousand films.

**Phase 2 — Scoring:** for each film added in Phase 1 that still has no OMDb data, it calls the OMDb API to fetch the Tomatometer and audience score. The default rate is 800 requests per day to respect OMDb's free-tier daily quota. When the quota is hit (HTTP 401), the job stops immediately and logs a single warning rather than hammering the API with failed calls — a `_QuotaExhausted` exception propagates up and breaks all loops cleanly. The remaining films are picked up by the regular nightly scoring run until the backlog is clear.

**Progress page:** navigate to `/backfill.html` to see live phase status, DB counts, and an ETA for how many days until all films are scored. The page polls every 5 seconds and infers state from the database if no progress file exists.

Run the backfill manually:
```bash
python backend/backfill.py
```

This is a one-time operation. Once all films are scored the nightly updater takes over.

## How the live data works

Every day at 2am UTC a background job runs four steps:

1. **TMDB** — finds films released in the last 24 hours and adds any that aren't already in the database
2. **OMDb** — refreshes the Rotten Tomatoes % and IMDB score for every film that's still in its active review window
3. **The Guardian** — pulls film review articles published yesterday and links them to matching films
4. **NYT** — same for the New York Times Arts section

The scheduler runs inside the FastAPI process via APScheduler, so no cron jobs or separate workers are needed. If the server restarts, the scheduler starts again automatically on the next boot.

You can also trigger it manually without waiting for 2am:
```
POST /api/admin/trigger-update
```

**Sentiment caching:** IMDB sentiment scores for individual critic reviews are computed once on the first request for that film and then stored in MySQL. Every subsequent request just reads the cached result — no model inference on repeat visits.

**Trend caching:** temporal trend analysis is cached per film in MySQL and only recomputed when new reviews have been added since the last computation.

## Technical approach

**Aspect extraction** uses `facebook/bart-large-mnli` via HuggingFace's zero-shot classification pipeline. BART handles open-ended labels without task-specific fine-tuning, so adding a new aspect is just a label string change. Reviews get classified against 8 film aspect labels with `multi_label=True`. Input is truncated to ~400 words because BART has a 1024 token limit and crashes silently if you go over.

**Per-aspect sentiment** uses `nlptown/bert-base-multilingual-uncased-sentiment`, a multilingual BERT fine-tuned on product reviews across 6 languages. It outputs 1-5 stars which maps cleanly to Negative/Neutral/Positive. For each aspect the analyser first tries to find sentences that mention aspect-relevant keywords, and scores those. Falls back to the full review if nothing matches.

**IMDB-trained sentiment classifiers** — two models trained on the IMDB 50k reviews dataset, run as an ensemble:
- **TF-IDF + SGD Classifier**: trains in ~15 seconds on startup, supports incremental learning via `partial_fit()` so new labelled reviews update the model immediately without full retraining
- **DistilBERT (fine-tuned)**: `distilbert-base-uncased` fine-tuned on 25k IMDB reviews for 2 epochs. Trained once and saved to `models/distilbert-imdb/`. On subsequent startups it loads from disk in seconds. New samples are buffered and a continued fine-tune run triggers every 20 samples, with replay from original IMDB data to prevent catastrophic forgetting.

When the two models disagree on a review, DistilBERT's prediction wins but at reduced confidence (multiplied by 0.82). When they agree the result is flagged as `agreement=true` and the confidence is averaged.

**Temporal trend analysis** groups critic reviews by month and runs the Mann-Kendall trend test via `pymannkendall`. Mann-Kendall is non-parametric, which matters here because review counts per month are noisy and definitely not normally distributed. A rolling 3-month average smooths out months where only one or two reviews landed.

**Database layer** is MySQL (tested locally with MySQL 9.x, deployable to TiDB Cloud for a managed production setup). Films stay in a pandas DataFrame in memory for fast search and leaderboard queries. Reviews are fetched per-film from MySQL on demand so the full 1.1M row table never has to load into memory.

**Frontend** is a plain HTML/CSS/JS SPA with no build step — FastAPI serves it directly from `frontend/`. Navigation is hash-based so refreshing on any section returns to that section. Charts use Chart.js loaded from CDN.

<!-- METRICS_START -->
## Model performance

Both classifiers are trained on the IMDB 50k reviews dataset with an 80/10/10 train/val/test split.

| Model | Train Acc | Val Acc | Test Acc | Train samples |
|---|---|---|---|---|
| TF-IDF + SGD Classifier | 91.6% | 89.3% | 90.1% | 40,000 |
| DistilBERT (fine-tuned) | — | 89.1% | 89.1% | 20,000 (2 epochs) |

DistilBERT doesn't track training accuracy during fine-tuning (eval only), hence the dash. Metrics are regenerated after each training run and saved to `models/accuracy_report.json`.
<!-- METRICS_END -->

## API endpoints

```
GET  /api/health
GET  /api/stats/overview
GET  /api/films/search?q=...
GET  /api/films/{rotten_tomatoes_link}/divergence
GET  /api/films/{rotten_tomatoes_link}/temporal
GET  /api/films/{rotten_tomatoes_link}/explain
GET  /api/divergence/leaderboard
GET  /api/divergence/genre
POST /api/analyse/review
POST /api/train/review
GET  /api/models/accuracy

POST /api/admin/trigger-update
GET  /api/admin/update-status
POST /api/admin/reactivate/{rotten_tomatoes_link}
GET  /api/admin/backfill-progress
POST /api/admin/score-backlog
```

## Setup

### 1. Clone and create a virtual environment

```bash
git clone <repo>
cd criticlens
python -m venv venv

# Windows
.\venv\Scripts\activate

# Mac/Linux
source venv/bin/activate
```

### 2. Set up MySQL

You need a MySQL database running somewhere. For local development MySQL Community Server works fine. For production TiDB Cloud has a free tier that is fully MySQL-compatible.

**Create the database:**
```sql
CREATE DATABASE IF NOT EXISTS criticlens CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'criticlens_user'@'localhost' IDENTIFIED BY 'your_password_here';
GRANT ALL PRIVILEGES ON criticlens.* TO 'criticlens_user'@'localhost';
```

**Set the connection env vars:**
```bash
DB_HOST=localhost
DB_PORT=3306
DB_NAME=criticlens
DB_USER=criticlens_user
DB_PASSWORD=your_password_here

# TiDB Cloud only
DB_SSL_CA=/path/to/ca.pem
DB_PORT=4000
```

The app creates its own tables on startup — no SQL schema file to run.

### 3. Install PyTorch

```bash
# CUDA (NVIDIA GPU)
pip install torch --index-url https://download.pytorch.org/whl/cu124

# CPU only
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### 4. Install remaining dependencies

```bash
pip install -r backend/requirements.txt
```

### 5. Configure API keys

None of these are required — the app runs without them, it just won't pull live data.

| Variable | Where to get it | What it does |
|---|---|---|
| `TMDB_API_KEY` | themoviedb.org → Settings → API | Discovers new film releases daily |
| `OMDB_API_KEY` | omdbapi.com (free, 1000 req/day) | Keeps RT scores up to date |
| `GUARDIAN_API_KEY` | open-platform.theguardian.com (free) | Pulls film reviews from The Guardian |
| `NYT_API_KEY` | developer.nytimes.com → enable Article Search API | Pulls film reviews from the NYT |

### 6. Start the app

```bash
python -m uvicorn backend.main:app --port 8000
```

Open `http://localhost:8000`.

**First startup timeline (fresh install, no existing data):**

| Step | What happens | Time |
|---|---|---|
| 1 | Kaggle datasets downloaded | 3–10 min (once only) |
| 2 | MySQL schema created | ~1 sec |
| 3 | Films + reviews migrated from CSV to MySQL | 5–10 min (once only) |
| 4 | Films loaded into memory from MySQL | ~2 sec |
| 5 | TF-IDF + SGD trained on 50k IMDB reviews | ~15 sec (once only) |
| 6 | DistilBERT fine-tuned on 25k IMDB reviews | 5–15 min GPU / 60–90 min CPU (once only) |
| 7 | BART and BERT-multilingual loaded from HuggingFace | 30–60 sec |
| 8 | App ready | — |

If the database is already populated and models are already trained (e.g. deploying to a new server with an existing TiDB Cloud cluster), steps 1–3 and 5–6 are all skipped. Startup takes about 1–2 minutes in that case.

## Deploying to Hugging Face Spaces + TiDB Cloud

### Step 1 — Export your local data to TiDB Cloud

On your local machine, dump the database:
```bash
mysqldump -u criticlens_user -p criticlens > criticlens_dump.sql
```

Create a free Serverless cluster at [tidbcloud.com](https://tidbcloud.com), then import the dump via their web console (Import → Local File) or the TiDB CLI. The schema and data are fully MySQL-compatible so the import works without changes.

Download the CA certificate from your cluster's connection settings page. You'll need this for the SSL connection.

### Step 2 — Create a Docker Space on Hugging Face

Go to [huggingface.co/new-space](https://huggingface.co/new-space), choose Docker as the SDK, and set it to public or private as you prefer.

### Step 3 — Set Space secrets

In the Space settings under Repository Secrets, add all of these:

```
DB_HOST         your-cluster.tidbcloud.com
DB_PORT         4000
DB_NAME         criticlens
DB_USER         your_tidb_user
DB_PASSWORD     your_tidb_password
DB_SSL_CA       /app/ca.pem   (if you mount the cert — see note below)
TMDB_API_KEY    ...
OMDB_API_KEY    ...
GUARDIAN_API_KEY ...
NYT_API_KEY     ...
```

For the CA cert, the simplest approach is to add it to your repo as `ca.pem` and reference `/app/ca.pem` in `DB_SSL_CA`. TiDB Cloud Serverless clusters all use the same DigiCert root so the cert doesn't contain any credentials.

### Step 4 — Push the code

```bash
git remote add space https://huggingface.co/spaces/your-username/criticlens
git push space main
```

HF Spaces will build the Docker image and start the container. The first boot takes a few minutes while BART and the multilingual BERT load from HuggingFace's model hub. After that it's fast.

The pre-trained IMDB classifiers (`models/`) are baked into the image, so there's no training step on startup. The database is already populated from the dump, so there's no migration step either. The nightly update job starts automatically and keeps the data fresh.

## Project structure

```
criticlens/
├── Dockerfile
├── .dockerignore
├── backend/
│   ├── main.py               FastAPI app, all routes, APScheduler setup
│   ├── db.py                 MySQL connection, schema, all queries
│   ├── migrate.py            One-time CSV → MySQL migration
│   ├── backfill.py           One-time post-2020 film discovery + OMDb scoring
│   ├── review_backfill.py    One-time historical Guardian + NYT review import
│   ├── updater.py            Daily TMDB/OMDb/Guardian/NYT update job
│   ├── data_loader.py        Kaggle download, startup sequence, films DataFrame
│   ├── imdb_classifier.py    TF-IDF + DistilBERT training, inference, incremental updates
│   ├── aspect_extractor.py   BART zero-shot aspect detection
│   ├── sentiment_analyser.py BERT per-aspect sentiment scoring
│   ├── divergence_engine.py  Film lookup, search, leaderboard, sentiment caching, explain
│   ├── temporal_analyser.py  Mann-Kendall trend analysis, trend caching
│   └── models.py             Pydantic response models
├── frontend/
│   ├── index.html            Main SPA (Overview, Film Search, Review Analyser, Leaderboard)
│   ├── backfill.html         Admin progress page for the one-time backfill runs
│   ├── app.js                All frontend logic (hash routing, search, charts, explain)
│   ├── styles.css            Cinema noir theme with glassmorphism
│   └── Background.png        Full-page background image
├── data/                     CSVs downloaded here on first run (excluded from Docker image)
└── models/                   Trained models saved here, included in Docker image
```

## Requirements

Python 3.10+. Tested on Python 3.12. GPU is auto-detected for DistilBERT training and inference — CUDA is used automatically if available, no config change needed.
