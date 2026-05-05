"""tests/test_ratings.py

Tests for Steam review-volume weighting and weighted average rating logic.
"""

from web.utils.ratings import (
    calculate_effective_average_rating,
    effective_steam_score,
    igdb_bayesian_rating,
    steam_review_weight,
    steam_weighted_review_desc,
    IGDB_BAYESIAN_WEIGHT,
    IGDB_PRIOR_MEAN,
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
    # Steam contributes 95 * 0.25 = 23.75 (weight=0.25).
    # IGDB 90 with 1000 votes: bayesian ≈ 90.0 (weight=1.0).
    # Average is (23.75 + ~90) / (0.25 + 1) ≈ 91.0
    avg = calculate_effective_average_rating(
        critics_score=95,
        steam_total_reviews=10,
        igdb_rating=90,
        igdb_rating_count=1000,
    )
    assert avg is not None
    # With 1000 votes Bayesian barely moves from 90; result should be ~91.
    assert 90.0 <= avg <= 92.0


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
