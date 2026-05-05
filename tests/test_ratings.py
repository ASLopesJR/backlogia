"""tests/test_ratings.py

Tests for Steam review-volume weighting and weighted average rating logic.
"""

from web.utils.ratings import (
    calculate_effective_average_rating,
    effective_steam_score,
    igdb_bayesian_rating,
    steam_bayesian_score,
    steam_review_weight,
    steam_weighted_review_desc,
    IGDB_BAYESIAN_WEIGHT,
    IGDB_PRIOR_MEAN,
    STEAM_BAYESIAN_WEIGHT,
    STEAM_PRIOR_MEAN,
)


def test_steam_score_below_threshold_is_ignored():
    assert effective_steam_score(95, 9) is None


def test_steam_score_low_volume_is_downweighted():
    # 10-49 reviews => 0.25 weight
    assert effective_steam_score(80, 10) == 20.0


def test_steam_review_weight_tiers():
    assert steam_review_weight(0) == 0.0
    assert steam_review_weight(9) == 0.0
    assert steam_review_weight(10) == 0.25
    assert steam_review_weight(49) == 0.25
    assert steam_review_weight(50) == 0.6
    assert steam_review_weight(499) == 0.6
    assert steam_review_weight(500) == 1.0


def test_steam_review_desc_tiers_match_spec():
    assert steam_weighted_review_desc(100, 9) == "Not enough reviews"
    assert steam_weighted_review_desc(85, 20) == "Positive"
    assert steam_weighted_review_desc(85, 100) == "Very Positive"
    assert steam_weighted_review_desc(97, 2000) == "Overwhelmingly Positive"


def test_average_rating_uses_volume_weighted_steam_score():
    # Steam Bayesian with 10 reviews at 95%:
    #   (10*95 + 50*70) / (10+50) = 4450/60 ≈ 74.2
    # IGDB 90 with 1000 votes: Bayesian ≈ 89.5 (weight=1.0)
    # Average ≈ (74.2 + 89.5) / 2 ≈ 81.9
    avg = calculate_effective_average_rating(
        critics_score=95,
        steam_total_reviews=10,
        igdb_rating=90,
        igdb_rating_count=1000,
    )
    assert avg is not None
    assert 80.0 <= avg <= 84.0


def test_average_rating_ignores_steam_when_below_min_reviews():
    avg = calculate_effective_average_rating(
        critics_score=100,
        steam_total_reviews=3,
        igdb_rating=80,
        igdb_rating_count=500,
    )
    # Steam excluded; only IGDB Bayesian value contributes.
    expected = igdb_bayesian_rating(80, 500)
    assert avg == round(expected, 1)


def test_steam_bayesian_score_below_threshold_is_none():
    assert steam_bayesian_score(100, 9) is None
    assert steam_bayesian_score(100, 0) is None
    assert steam_bayesian_score(None, 100) is None


def test_steam_bayesian_score_collapses_to_prior_with_min_reviews():
    # With exactly MIN reviews the score is still pulled strongly toward the prior.
    result = steam_bayesian_score(100.0, 10)
    assert result is not None
    assert result < 100.0
    assert result > STEAM_PRIOR_MEAN


def test_steam_bayesian_score_high_volume_approaches_raw():
    # With many reviews the Bayesian score should be very close to the raw score.
    result = steam_bayesian_score(99.0, 10_000)
    assert result is not None
    assert abs(result - 99.0) < 0.5


def test_average_rating_steam_low_reviews_ranked_below_high_reviews():
    # A game with 10 reviews at 100% should score LOWER than one with 1000
    # reviews at 99%. This was the original bug: the weight cancelled out,
    # making 100%/10 reviews beat 99%/1000 reviews.
    avg_few = calculate_effective_average_rating(
        critics_score=100,
        steam_total_reviews=10,
    )
    avg_many = calculate_effective_average_rating(
        critics_score=99,
        steam_total_reviews=1000,
    )
    assert avg_few is not None
    assert avg_many is not None
    assert avg_many > avg_few


# ---------------------------------------------------------------------------
# IGDB Bayesian weighting
# ---------------------------------------------------------------------------


def test_igdb_bayesian_rating_returns_none_for_none_rating():
    assert igdb_bayesian_rating(None, 100) is None


def test_igdb_bayesian_rating_no_votes_collapses_to_prior():
    # count=0  =>  (0*r + W*C) / (0+W) = C
    result = igdb_bayesian_rating(100.0, 0)
    assert result == IGDB_PRIOR_MEAN


def test_igdb_bayesian_rating_very_high_count_approaches_raw():
    # With many votes the smoothed value should be very close to the raw rating.
    result = igdb_bayesian_rating(90.0, 10_000)
    assert abs(result - 90.0) < 0.1


def test_igdb_bayesian_rating_low_count_pulls_toward_prior():
    # A perfect 100 with only 5 votes should be noticeably below 100.
    result = igdb_bayesian_rating(100.0, 5)
    assert result < 100.0
    assert result > IGDB_PRIOR_MEAN


def test_igdb_bayesian_rating_none_count_treated_as_zero():
    # None count behaves like count=0.
    assert igdb_bayesian_rating(90.0, None) == igdb_bayesian_rating(90.0, 0)


def test_average_rating_igdb_uses_bayesian():
    # A game with 1 IGDB vote of 100 should score lower than one with 1000 votes of 90.
    avg_few = calculate_effective_average_rating(igdb_rating=100.0, igdb_rating_count=1)
    avg_many = calculate_effective_average_rating(igdb_rating=90.0, igdb_rating_count=1000)
    assert avg_few is not None
    assert avg_many is not None
    assert avg_many > avg_few
