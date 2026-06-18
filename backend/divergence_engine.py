"""
Divergence engine - builds the per-film analysis payload.

Key change from the original: reviews come from MySQL per-film
(get_reviews_for_film) instead of being filtered out of a 1M-row DataFrame.

IMDB sentiment is now compute-once/serve-from-cache:
  - First request for a film: classifier runs, result is written to MySQL.
  - Subsequent requests: MySQL returns the cached labels; no model inference needed.
"""

import re
import pandas as pd
import logging

from data_loader import get_movies_df, get_reviews_for_film
import imdb_classifier
import db

logger = logging.getLogger(__name__)


def compute_film_divergence(rotten_tomatoes_link: str) -> dict | None:
    movies_df = get_movies_df()

    if movies_df is None:
        raise RuntimeError("Data not loaded")

    film_rows = movies_df[movies_df["rotten_tomatoes_link"] == rotten_tomatoes_link]

    if film_rows.empty:
        return None

    film = film_rows.iloc[0]

    def safe_val(val):
        if pd.isna(val):
            return None
        return val

    release_date = None
    if pd.notna(film.get("original_release_date")):
        try:
            release_date = str(film["original_release_date"])[:10]
        except Exception:
            release_date = None

    result = {
        "movie_title":           safe_val(film.get("movie_title")),
        "tomatometer_rating":    safe_val(film.get("tomatometer_rating")),
        "audience_rating":       safe_val(film.get("audience_rating")),
        "divergence_score":      safe_val(film.get("divergence_score")),
        "divergence_label":      safe_val(film.get("divergence_label")),
        "genres":                safe_val(film.get("genres")),
        "directors":             safe_val(film.get("directors")),
        "original_release_date": release_date,
        "release_year":          safe_val(film.get("release_year")),
        "critics_consensus":     safe_val(film.get("critics_consensus")),
        "runtime":               safe_val(film.get("runtime")),
        "rotten_tomatoes_link":   rotten_tomatoes_link,
        "poster_url":             safe_val(film.get("poster_url")),
        # 'imdb' means audience_rating is IMDB×10 proxy, not a real RT audience score
        # NULL means it's a genuine RT score from the Kaggle dataset
        "audience_score_source":  safe_val(film.get("audience_score_source")),
    }

    # pull the top 5 reviews from MySQL for this film
    reviews = get_reviews_for_film(rotten_tomatoes_link)
    top_reviews = []

    for row in reviews[:5]:
        content = row.get("review_content")

        sentiment_fast = None
        sentiment_deep = None

        if row.get("sentiment_fast_label"):
            # cached from a previous request - no need to run the model again
            sentiment_fast = {
                "sentiment":  row["sentiment_fast_label"],
                "confidence": float(row.get("sentiment_fast_confidence") or 0.0),
                "model":      "tfidf_sgd",
            }
            if row.get("sentiment_deep_label"):
                sentiment_deep = {
                    "sentiment":  row["sentiment_deep_label"],
                    "confidence": float(row.get("sentiment_deep_confidence") or 0.0),
                    "model":      "distilbert",
                }

        elif content and imdb_classifier.is_ready():
            # first time this film has been opened - compute and cache
            try:
                sentiment_fast = imdb_classifier.predict_fast(content)
                sentiment_deep = imdb_classifier.predict_deep(content)

                review_id = row.get("id")
                if review_id:
                    db.update_review_sentiment(
                        review_id,
                        sentiment_fast,
                        sentiment_deep or {},
                    )
            except Exception as e:
                logger.warning(f"IMDB classifier failed on review: {e}")

        top_reviews.append({
            "review_content": content,
            "critic_name":    row.get("critic_name"),
            "publisher_name": row.get("publisher_name"),
            "review_type":    row.get("review_type"),
            "review_score":   row.get("review_score"),
            "sentiment_fast": sentiment_fast,
            "sentiment_deep": sentiment_deep,
        })

    result["critic_reviews"] = top_reviews
    return result


def explain_divergence(rotten_tomatoes_link: str) -> dict | None:
    """
    Explains WHY critics and audiences diverged on a specific film.

    Uses only data already in the database — no extra ML inference needed.
    Pulls all stored reviews, computes Rotten/Fresh breakdown and sentiment
    stats, extracts short representative quotes, then writes a plain-English
    summary so users understand the gap, not just the number.
    """
    movies_df = get_movies_df()
    if movies_df is None:
        return None

    film_rows = movies_df[movies_df["rotten_tomatoes_link"] == rotten_tomatoes_link]
    if film_rows.empty:
        return None

    film       = film_rows.iloc[0]
    tomatometer = film.get("tomatometer_rating")
    audience    = film.get("audience_rating")
    divergence  = film.get("divergence_score")

    if tomatometer is None or pd.isna(tomatometer) or \
       audience    is None or pd.isna(audience)    or \
       divergence  is None or pd.isna(divergence):
        return {"explanation": None, "quotes": [], "stats": {}, "has_data": False}

    tomatometer = float(tomatometer)
    audience    = float(audience)
    divergence  = float(divergence)
    gap         = abs(round(divergence))

    # pull all stored reviews — we need more than the 5 shown in the UI
    reviews = get_reviews_for_film(rotten_tomatoes_link)

    rotten_reviews = [r for r in reviews if r.get("review_type") == "Rotten"]
    fresh_reviews  = [r for r in reviews if r.get("review_type") == "Fresh"]

    # use the pre-computed sentiment labels we stored during the Kaggle migration
    neg_reviews = [
        r for r in reviews
        if (r.get("sentiment_deep_label") or r.get("sentiment_fast_label")) in ("NEGATIVE", "Negative")
    ]
    pos_reviews = [
        r for r in reviews
        if (r.get("sentiment_deep_label") or r.get("sentiment_fast_label")) in ("POSITIVE", "Positive")
    ]

    def _best_quote(review: dict) -> str | None:
        text = (review.get("review_content") or "").strip()
        if not text:
            return None
        # prefer the first sentence that's punchy (30–200 chars, not just "Read more")
        for chunk in re.split(r"(?<=[.!?])\s+", text):
            chunk = chunk.strip().strip('"\'')
            if 30 <= len(chunk) <= 200 and not chunk.lower().startswith("read"):
                return chunk
        return None

    def _make_quote(review: dict) -> dict | None:
        q = _best_quote(review)
        if not q:
            return None
        return {
            "text":      q,
            "critic":    review.get("critic_name", ""),
            "publisher": review.get("publisher_name", ""),
            "type":      review.get("review_type", ""),
        }

    # pick representative quotes based on which direction the gap goes
    if divergence < -20:
        # audiences loved it, critics didn't — lead with rotten reviews
        candidate_reviews = rotten_reviews[:12] + fresh_reviews[:4]
    elif divergence > 20:
        # critics loved it, audiences didn't — lead with fresh reviews
        candidate_reviews = fresh_reviews[:12] + rotten_reviews[:4]
    else:
        candidate_reviews = reviews[:10]

    quotes = [q for q in (_make_quote(r) for r in candidate_reviews) if q][:3]

    # plain-English explanation — specific numbers, no vague hand-waving
    total = len(reviews)
    if total == 0:
        explanation = (
            f"Critics scored this film {round(tomatometer)}% while audiences gave it "
            f"{round(audience)}% — a {gap}-point gap. "
            f"No critic reviews are stored yet, so we can't explain the specific reasons for the split."
        )
    elif divergence < -20:
        rotten_pct = round(len(rotten_reviews) / total * 100) if total else 0
        explanation = (
            f"Audiences rated this film {gap} points higher than critics. "
            f"Of the {total} critic reviews stored, {len(rotten_reviews)} ({rotten_pct}%) are Rotten. "
            f"Critics were broadly negative — the reviews below show what put them off. "
            f"Audiences, however, gave it {round(audience)}%, suggesting they connected with "
            f"the film on different terms than critics typically apply: "
            f"subject matter, emotional impact, or personal relevance often matter more to "
            f"general viewers than craft or objectivity."
        )
    elif divergence > 20:
        fresh_pct = round(len(fresh_reviews) / total * 100) if total else 0
        explanation = (
            f"Critics rated this film {gap} points higher than audiences. "
            f"Of the {total} critic reviews stored, {len(fresh_reviews)} ({fresh_pct}%) are Fresh. "
            f"Critics responded strongly to its qualities — the reviews below show what they valued. "
            f"Audiences gave it only {round(audience)}%, which often happens when a film rewards "
            f"patience, prior knowledge, or a taste for unconventional storytelling that broader "
            f"audiences don't always share."
        )
    else:
        explanation = (
            f"Critics ({round(tomatometer)}%) and audiences ({round(audience)}%) broadly agree on "
            f"this film — the {gap}-point gap is within normal variation and doesn't represent "
            f"a meaningful divergence."
        )

    return {
        "has_data":    True,
        "explanation": explanation,
        "quotes":      quotes,
        "stats": {
            "total_reviews":      total,
            "rotten_count":       len(rotten_reviews),
            "fresh_count":        len(fresh_reviews),
            "negative_sentiment": len(neg_reviews),
            "positive_sentiment": len(pos_reviews),
        },
        "direction": "audience_higher" if divergence < 0 else "critic_higher",
        "gap":        gap,
    }


def get_top_divergent_films(n: int = 20) -> dict:
    movies_df = get_movies_df()

    if movies_df is None:
        return {"critic_favoured": [], "audience_favoured": []}

    valid = movies_df.dropna(subset=["tomatometer_rating", "audience_rating", "divergence_score"])

    critic_favoured   = valid.nlargest(n // 2,  "divergence_score")
    audience_favoured = valid.nsmallest(n // 2, "divergence_score")

    def row_to_dict(row):
        return {
            "movie_title":        str(row["movie_title"])         if pd.notna(row.get("movie_title"))        else None,
            "tomatometer_rating": float(row["tomatometer_rating"]) if pd.notna(row.get("tomatometer_rating")) else None,
            "audience_rating":    float(row["audience_rating"])    if pd.notna(row.get("audience_rating"))    else None,
            "divergence_score":   float(row["divergence_score"])   if pd.notna(row.get("divergence_score"))   else None,
            "divergence_label":   str(row["divergence_label"])     if pd.notna(row.get("divergence_label"))   else None,
            "genres":             str(row["genres"])               if pd.notna(row.get("genres"))             else None,
            "rotten_tomatoes_link": str(row["rotten_tomatoes_link"]) if pd.notna(row.get("rotten_tomatoes_link")) else None,
        }

    return {
        "critic_favoured":   [row_to_dict(r) for _, r in critic_favoured.iterrows()],
        "audience_favoured": [row_to_dict(r) for _, r in audience_favoured.iterrows()],
    }


def search_films(query: str) -> list:
    movies_df = get_movies_df()

    if movies_df is None or not query:
        return []

    mask    = movies_df["movie_title"].str.contains(query, case=False, na=False)
    matches = movies_df[mask].head(10)

    results = []
    for _, row in matches.iterrows():
        results.append({
            "movie_title":        str(row["movie_title"])         if pd.notna(row.get("movie_title"))        else None,
            "tomatometer_rating": float(row["tomatometer_rating"]) if pd.notna(row.get("tomatometer_rating")) else None,
            "audience_rating":    float(row["audience_rating"])    if pd.notna(row.get("audience_rating"))    else None,
            "divergence_score":   float(row["divergence_score"])   if pd.notna(row.get("divergence_score"))   else None,
            "rotten_tomatoes_link": str(row["rotten_tomatoes_link"]) if pd.notna(row.get("rotten_tomatoes_link")) else None,
        })

    return results
