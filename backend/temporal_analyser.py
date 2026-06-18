"""
Temporal trend analysis - how critic sentiment shifts over time for a film.

Key changes from the original:
  - Reviews come from MySQL per-film (get_reviews_for_film) instead of being
    filtered from a full in-memory DataFrame.
  - Computed trends are cached in the MySQL films table.
    A cached trend is served as-is unless new reviews were inserted after the
    last computation (reviews_updated_at > trend_analysed_at).
  - This means the first request after a new review batch pays the compute cost,
    all later requests just read the JSON blob from MySQL.
"""

import pandas as pd
import numpy as np
import logging

from data_loader import get_reviews_for_film
import db

logger = logging.getLogger(__name__)


def compute_temporal_drift(rotten_tomatoes_link: str) -> dict:
    # return the cached result if the reviews haven't changed since we last ran
    cached = db.get_cached_trend(rotten_tomatoes_link)
    if cached:
        logger.debug(f"Serving cached trend for {rotten_tomatoes_link}")
        return cached

    reviews = get_reviews_for_film(rotten_tomatoes_link)

    if not reviews:
        return {
            "warning":      "Data not loaded",
            "trend":        "no trend",
            "p_value":      None,
            "tau":          None,
            "monthly_data": [],
        }

    film_reviews = pd.DataFrame(reviews)

    if len(film_reviews) < 3:
        return {
            "warning":      "Not enough reviews for temporal analysis (need at least 3)",
            "review_count": len(film_reviews),
            "trend":        "no trend",
            "p_value":      None,
            "tau":          None,
            "monthly_data": [],
        }

    # parse the review dates
    film_reviews["review_date"] = pd.to_datetime(film_reviews["review_date"], errors="coerce")
    film_reviews = film_reviews.dropna(subset=["review_date"])

    if len(film_reviews) < 3:
        return {
            "warning":      "Too many unparseable review dates",
            "review_count": len(film_reviews),
            "trend":        "no trend",
            "p_value":      None,
            "tau":          None,
            "monthly_data": [],
        }

    film_reviews["period"] = film_reviews["review_date"].dt.to_period("M")

    # normalise review_type - sometimes it arrives lowercase
    film_reviews["review_type_norm"] = film_reviews["review_type"].str.strip().str.capitalize()

    monthly = film_reviews.groupby("period").apply(
        lambda g: pd.Series({
            "fresh_count":  (g["review_type_norm"] == "Fresh").sum(),
            "rotten_count": (g["review_type_norm"] == "Rotten").sum(),
        })
    ).reset_index()

    monthly["total"]       = monthly["fresh_count"] + monthly["rotten_count"]
    monthly["fresh_ratio"] = np.where(
        monthly["total"] > 0,
        monthly["fresh_count"] / monthly["total"],
        np.nan,
    )

    # rolling 3-month average smooths out months with very few reviews
    monthly = monthly.sort_values("period")
    monthly["fresh_ratio_rolling"] = monthly["fresh_ratio"].rolling(window=3, min_periods=1).mean()

    # Mann-Kendall trend test on the raw monthly fresh ratio
    trend_result = {"trend": "no trend", "p_value": None, "tau": None}
    ratio_series = monthly["fresh_ratio"].dropna().tolist()

    if len(ratio_series) >= 3:
        try:
            import pymannkendall as mk
            mk_result = mk.original_test(ratio_series)
            trend_map = {"increasing": "increasing", "decreasing": "decreasing", "no trend": "no trend"}
            trend_result = {
                "trend":   trend_map.get(mk_result.trend, "no trend"),
                "p_value": float(mk_result.p),
                "tau":     float(mk_result.Tau),
            }
        except Exception as e:
            logger.warning(f"Mann-Kendall failed: {e}")

    monthly_data = []
    for _, row in monthly.iterrows():
        monthly_data.append({
            "period":       str(row["period"]),
            "fresh_count":  int(row["fresh_count"]),
            "rotten_count": int(row["rotten_count"]),
            "fresh_ratio":  float(row["fresh_ratio"]) if pd.notna(row["fresh_ratio"]) else None,
        })

    result = {
        "trend":        trend_result["trend"],
        "p_value":      trend_result["p_value"],
        "tau":          trend_result["tau"],
        "monthly_data": monthly_data,
    }

    # persist so subsequent requests don't have to recompute
    db.save_cached_trend(rotten_tomatoes_link, result)

    return result
