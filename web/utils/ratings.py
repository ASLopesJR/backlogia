# ratings.py
# Shared rating helpers for Steam weighting and average score calculations.

import json

MIN_STEAM_REVIEWS_FOR_COUNT = 10


def steam_review_weight(total_reviews):
    """Return weight based on Steam review volume.

    < 10: ignored for ranking/average
    10-49: low confidence
    50-499: medium confidence
    500+: high confidence
    """
    try:
        count = int(total_reviews or 0)
    except (TypeError, ValueError):
        return 0.0

    if count < MIN_STEAM_REVIEWS_FOR_COUNT:
        return 0.0
    if count < 50:
        return 0.25
    if count < 500:
        return 0.6
    return 1.0


def steam_weighted_review_desc(review_score, total_reviews):
    """Map Steam score + review volume to a sentiment label."""
    try:
        score = float(review_score)
    except (TypeError, ValueError):
        return "User Reviews"

    try:
        count = int(total_reviews or 0)
    except (TypeError, ValueError):
        count = 0

    if count < MIN_STEAM_REVIEWS_FOR_COUNT:
        return "Not enough reviews"

    if count < 50:
        if score <= 19:
            return "Negative"
        if score <= 39:
            return "Mostly Negative"
        if score <= 69:
            return "Mixed"
        if score <= 79:
            return "Mostly Positive"
        return "Positive"

    if count < 500:
        if score <= 19:
            return "Very Negative"
        if score <= 39:
            return "Mostly Negative"
        if score <= 69:
            return "Mixed"
        if score <= 79:
            return "Mostly Positive"
        return "Very Positive"

    if score <= 19:
        return "Overwhelmingly Negative"
    if score <= 39:
        return "Mostly Negative"
    if score <= 69:
        return "Mixed"
    if score <= 79:
        return "Mostly Positive"
    if score <= 94:
        return "Very Positive"
    return "Overwhelmingly Positive"


def parse_extra_data(extra_data):
    """Parse games.extra_data into a dict."""
    if isinstance(extra_data, dict):
        return extra_data
    if not extra_data:
        return {}
    try:
        parsed = json.loads(extra_data)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def steam_total_reviews_from_extra(extra_data):
    """Extract Steam total_reviews from extra_data."""
    data = parse_extra_data(extra_data)
    total_reviews = data.get("total_reviews")
    try:
        return int(total_reviews) if total_reviews is not None else None
    except (TypeError, ValueError):
        return None


# --- IGDB Bayesian weighting ------------------------------------------------

# Minimum number of IGDB votes to be considered "confident".
# Games with fewer votes are pulled toward the prior mean.
IGDB_BAYESIAN_WEIGHT = 25

# Prior mean: neutral IGDB score (0-100 scale) used when a game has few votes.
IGDB_PRIOR_MEAN = 70.0


def igdb_bayesian_rating(rating, count):
    """Apply Bayesian smoothing to an IGDB rating.

    Formula: (count * rating + W * C) / (count + W)
    where W = IGDB_BAYESIAN_WEIGHT and C = IGDB_PRIOR_MEAN.

    Returns None when rating is None.
    If count is None the formula still runs with count=0, which collapses the
    result entirely to the prior mean — so callers that have no count data get
    a neutral score rather than the raw (potentially noisy) value.
    """
    if rating is None:
        return None
    try:
        r = float(rating)
        n = int(count or 0)
    except (TypeError, ValueError):
        return None
    return (n * r + IGDB_BAYESIAN_WEIGHT * IGDB_PRIOR_MEAN) / (n + IGDB_BAYESIAN_WEIGHT)


# --- Steam score helpers -----------------------------------------------------

# Bayesian smoothing for Steam scores.
# W ghost reviews at the prior pull low-review scores toward the mean.
STEAM_BAYESIAN_WEIGHT = 50
STEAM_PRIOR_MEAN = 70.0


def steam_bayesian_score(critics_score, total_reviews):
    """Apply Bayesian smoothing to a Steam review score.

    Formula: (n * score + W * C) / (n + W)
    where W = STEAM_BAYESIAN_WEIGHT and C = STEAM_PRIOR_MEAN.

    Returns None when critics_score is None or total_reviews is below
    MIN_STEAM_REVIEWS_FOR_COUNT.
    """
    if critics_score is None:
        return None
    try:
        n = int(total_reviews or 0)
    except (TypeError, ValueError):
        return None
    if n < MIN_STEAM_REVIEWS_FOR_COUNT:
        return None
    try:
        score = float(critics_score)
    except (TypeError, ValueError):
        return None
    return (n * score + STEAM_BAYESIAN_WEIGHT * STEAM_PRIOR_MEAN) / (n + STEAM_BAYESIAN_WEIGHT)


def effective_steam_score(critics_score, total_reviews):
    """Return confidence-weighted Steam score used for sort/average.

    Returns None when review volume is below threshold.
    """
    if critics_score is None:
        return None

    weight = steam_review_weight(total_reviews)
    if weight <= 0:
        return None

    try:
        return float(critics_score) * weight
    except (TypeError, ValueError):
        return None


def calculate_effective_average_rating(
    critics_score=None,
    steam_total_reviews=None,
    igdb_rating=None,
    igdb_rating_count=None,
    aggregated_rating=None,
    aggregated_rating_count=None,
    total_rating=None,
    total_rating_count=None,
    metacritic_score=None,
    metacritic_user_score=None,
):
    """Calculate weighted average.

    Steam score is Bayesian-smoothed toward STEAM_PRIOR_MEAN using review
    volume, so games with few reviews are pulled toward a neutral baseline
    instead of contributing their raw (potentially perfect) score.
    IGDB ratings are Bayesian-smoothed toward IGDB_PRIOR_MEAN using their
    respective vote counts.
    """
    weighted_sum = 0.0
    total_weight = 0.0

    steam_bayes = steam_bayesian_score(critics_score, steam_total_reviews)
    if steam_bayes is not None:
        weighted_sum += steam_bayes
        total_weight += 1.0

    bayes_igdb = igdb_bayesian_rating(igdb_rating, igdb_rating_count)
    if bayes_igdb is not None:
        weighted_sum += bayes_igdb
        total_weight += 1.0

    bayes_agg = igdb_bayesian_rating(aggregated_rating, aggregated_rating_count)
    if bayes_agg is not None:
        weighted_sum += bayes_agg
        total_weight += 1.0

    bayes_total = igdb_bayesian_rating(total_rating, total_rating_count)
    if bayes_total is not None:
        weighted_sum += bayes_total
        total_weight += 1.0

    if metacritic_score is not None:
        weighted_sum += float(metacritic_score)
        total_weight += 1.0

    if metacritic_user_score is not None:
        weighted_sum += float(metacritic_user_score) * 10
        total_weight += 1.0

    if total_weight <= 0:
        return None

    return round(weighted_sum / total_weight, 1)
