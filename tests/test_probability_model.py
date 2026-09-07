"""The Poisson probability calculation (spec 13, testing bullet 1).

Checked three ways: against the closed form written into the specification,
against the values the specification quotes, and - where SciPy is installed -
against an independent implementation of the Poisson distribution.
"""

from __future__ import annotations

import math

import pytest

from models import FormStats, H2HStats, LeagueStats, MatchStatistics
from probability_model import (
    GLOBAL_BASELINE_GOALS,
    _component_spread,
    dixon_coles_over_15,
    estimate,
    lambda_from_over_15_rate,
    over_15_probability,
    poisson_pmf,
    smoothed_rate,
)


# --------------------------------------------------------------------------
# The closed form
# --------------------------------------------------------------------------
@pytest.mark.parametrize("lam", [0.0, 0.25, 1.0, 2.0, 2.7, 3.0, 4.5, 8.0])
def test_matches_the_specifications_closed_form(lam):
    """``P(Over 1.5) = 1 - exp(-lambda) * (1 + lambda)`` exactly (spec 5.1)."""
    expected = 1.0 - math.exp(-lam) * (1.0 + lam)
    assert over_15_probability(lam) == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("lam", [0.5, 1.0, 2.7, 3.0, 5.0])
def test_equals_one_minus_the_first_two_pmf_terms(lam):
    """Over 1.5 is the complement of exactly the 0-goal and 1-goal cases."""
    assert over_15_probability(lam) == pytest.approx(
        1.0 - poisson_pmf(0, lam) - poisson_pmf(1, lam), abs=1e-12
    )


def test_the_values_quoted_in_the_specification():
    """ "lambda = 2.7 gives about 75%; lambda = 3.0 gives about 80%" (spec 5.1)."""
    assert over_15_probability(2.7) == pytest.approx(0.75, abs=0.01)
    assert over_15_probability(3.0) == pytest.approx(0.80, abs=0.01)


def test_boundary_values():
    assert over_15_probability(0.0) == 0.0
    assert over_15_probability(30.0) == pytest.approx(1.0, abs=1e-9)


def test_strictly_increasing_in_lambda():
    values = [over_15_probability(lam / 10) for lam in range(0, 80)]
    assert all(b > a for a, b in zip(values, values[1:], strict=False))


def test_negative_lambda_is_rejected():
    with pytest.raises(ValueError):
        over_15_probability(-0.1)
    with pytest.raises(ValueError):
        poisson_pmf(1, -0.1)


def test_against_scipy_where_available():
    """Cross-check against an independent Poisson implementation."""
    scipy_stats = pytest.importorskip("scipy.stats")
    for lam in (0.4, 1.3, 2.7, 3.0, 4.8):
        reference = 1.0 - scipy_stats.poisson.cdf(1, lam)
        assert over_15_probability(lam) == pytest.approx(reference, abs=1e-12)


# --------------------------------------------------------------------------
# Inversion and smoothing
# --------------------------------------------------------------------------
@pytest.mark.parametrize("rate", [0.05, 0.3, 0.5, 0.75, 0.8, 0.95])
def test_rate_inversion_round_trips(rate):
    """``lambda_from_over_15_rate`` is the exact inverse of the closed form."""
    assert over_15_probability(lambda_from_over_15_rate(rate)) == pytest.approx(rate, abs=1e-8)


def test_rate_inversion_caps_a_saturated_sample():
    assert lambda_from_over_15_rate(0.0) == 0.0
    assert lambda_from_over_15_rate(1.0) == 12.0


def test_smoothing_pulls_a_saturated_rate_back():
    """10 of 10 becomes 11/12, not 1.0 - see ``smoothed_rate``'s docstring."""
    assert smoothed_rate(1.0, 10) == pytest.approx(11 / 12)
    assert smoothed_rate(0.0, 10) == pytest.approx(1 / 12)
    assert smoothed_rate(0.5, 10) == pytest.approx(0.5)


def test_smoothing_weakens_as_the_sample_grows():
    small = smoothed_rate(1.0, 4)
    large = smoothed_rate(1.0, 40)
    assert small < large < 1.0


def test_smoothing_keeps_a_saturated_sample_out_of_the_tail():
    """The point of the correction: no 9-goal expectation from a 10-match run."""
    unsmoothed = lambda_from_over_15_rate(0.999)
    smoothed = lambda_from_over_15_rate(smoothed_rate(1.0, 10))
    assert unsmoothed > 8.0
    assert smoothed < 4.5


# --------------------------------------------------------------------------
# Dixon-Coles
# --------------------------------------------------------------------------
def test_dixon_coles_with_zero_rho_is_the_plain_model():
    assert dixon_coles_over_15(1.5, 1.2, 0.0) == pytest.approx(over_15_probability(2.7), abs=1e-12)


def test_dixon_coles_lowers_the_estimate_for_positive_rho():
    """A positive rho adds weight to 1-0 and 0-1, which are Under 1.5."""
    assert dixon_coles_over_15(1.5, 1.2, 0.08) < over_15_probability(2.7)


def test_dixon_coles_rejects_rho_outside_range():
    for rho in (-0.1, 1.0, 1.5):
        with pytest.raises(ValueError):
            dixon_coles_over_15(1.5, 1.2, rho)


def test_dixon_coles_is_used_only_when_switched_on(config):
    stats = _full_statistics()
    plain = estimate(stats, config)

    config.use_dixon_coles = True
    config.dixon_coles_rho = 0.10
    corrected = estimate(stats, config)

    assert corrected.lambda_total == pytest.approx(plain.lambda_total)
    assert corrected.probability < plain.probability
    assert "Dixon-Coles" in corrected.note_text


# --------------------------------------------------------------------------
# Blending, shrinkage and the section 5.4 output
# --------------------------------------------------------------------------
def _form(matches=10, scored=1.6, conceded=1.3, over=0.8) -> FormStats:
    return FormStats(
        matches=matches,
        over15_rate=over,
        avg_scored=scored,
        avg_conceded=conceded,
        avg_total=scored + conceded,
        count_2plus=round(over * matches),
        source="test",
    )


def _full_statistics() -> MatchStatistics:
    stats = MatchStatistics()
    stats.home_form = _form()
    stats.away_form = _form(scored=1.4, conceded=1.5)
    stats.home_home_split = _form(matches=6, scored=1.8, conceded=1.1)
    stats.away_away_split = _form(matches=6, scored=1.2, conceded=1.7)
    stats.h2h = H2HStats(
        meetings=5, over15_rate=0.8, avg_total_goals=2.9, count_2plus=4, source="test"
    )
    stats.league = LeagueStats(
        name="Test League", matches=200, avg_goals=2.7, over15_rate=0.75, source="test"
    )
    return stats


def test_a_full_evidence_set_produces_a_usable_estimate(config):
    result = estimate(_full_statistics(), config)
    assert result.usable
    assert 0.0 < result.probability < 1.0
    assert result.lambda_total == pytest.approx(result.lambda_home + result.lambda_away, abs=1e-9)
    assert result.data_completeness == pytest.approx(1.0)
    assert result.confidence in {"High", "Medium", "Low"}


def test_probability_follows_the_closed_form_from_the_reported_lambda(config):
    """Spec 5.4 requires the reported lambda_total to explain the probability."""
    result = estimate(_full_statistics(), config)
    assert result.probability == pytest.approx(over_15_probability(result.lambda_total))


def test_no_statistics_at_all_is_refused_rather_than_guessed(config):
    result = estimate(MatchStatistics(), config)
    assert not result.usable
    assert result.probability is None
    assert "insufficient historical data" in result.reason


def test_a_league_baseline_alone_is_not_treated_as_evidence(config):
    """A league average dressed up as a match estimate would be a fabrication."""
    stats = MatchStatistics()
    stats.league = LeagueStats(name="L", matches=300, avg_goals=2.8, over15_rate=0.76, source="t")
    result = estimate(stats, config)
    assert not result.usable


def test_a_missing_component_is_reweighted_not_zeroed(config):
    """Spec 5.2 - a component with no data drops out and its weight is shared."""
    full = _full_statistics()
    without_h2h = _full_statistics()
    without_h2h.h2h = H2HStats()

    result = estimate(without_h2h, config)
    assert result.usable
    assert "h2h" in result.note_text
    # Weights are renormalised, so they still sum to one.
    assert sum(weight for _, _, weight in result.components.values()) == pytest.approx(1.0)
    assert "h2h" not in result.components
    assert estimate(full, config).components.keys() > result.components.keys()


def test_small_samples_are_pulled_toward_the_league_baseline(config):
    """Spec 5.3 - three matches must not carry the weight of twenty."""
    baseline = 2.60
    thin = MatchStatistics()
    thin.home_form = _form(matches=3, scored=3.0, conceded=2.5)
    thin.away_form = _form(matches=3, scored=3.0, conceded=2.5)
    thin.league = LeagueStats(name="L", matches=300, avg_goals=baseline, source="t")

    thick = MatchStatistics()
    thick.home_form = _form(matches=30, scored=3.0, conceded=2.5)
    thick.away_form = _form(matches=30, scored=3.0, conceded=2.5)
    thick.league = LeagueStats(name="L", matches=300, avg_goals=baseline, source="t")

    thin_result = estimate(thin, config)
    thick_result = estimate(thick, config)

    assert baseline < thin_result.lambda_total < thick_result.lambda_total
    assert "small sample" in thin_result.note_text


def test_the_global_prior_is_flagged_and_never_reported_as_a_statistic(config):
    stats = _full_statistics()
    stats.league = LeagueStats()  # no measured baseline
    result = estimate(stats, config)
    assert result.usable
    assert "global prior" in result.note_text
    assert f"{GLOBAL_BASELINE_GOALS:.2f}" in result.note_text
    assert "league" not in result.components


def test_confidence_falls_when_the_components_disagree(config):
    agreeing = _full_statistics()
    disagreeing = _full_statistics()
    disagreeing.h2h = H2HStats(
        meetings=5, over15_rate=0.2, avg_total_goals=0.6, count_2plus=1, source="test"
    )

    assert estimate(agreeing, config).spread < estimate(disagreeing, config).spread
    assert estimate(disagreeing, config).confidence == "Low"


def test_confidence_spread_is_scale_free():
    """Doubling every component leaves the disagreement measure unchanged.

    An absolute spread would double, and would rate the high-scoring fixture
    Low for the very same relative agreement - see the confidence constants.
    """
    low = {name: ((h, a), 1.0) for name, (h, a) in _COMPONENT_TOTALS.items()}
    high = {name: ((2 * h, 2 * a), 1.0) for name, (h, a) in _COMPONENT_TOTALS.items()}
    assert _component_spread(low) == pytest.approx(_component_spread(high))


#: Four components proposing different expected goals, used above.
_COMPONENT_TOTALS = {
    "recent_form": (1.5, 1.3),
    "home_away_split": (1.7, 1.1),
    "over15_rate": (1.4, 1.5),
    "h2h": (1.6, 1.2),
}


def test_spread_is_the_component_range_over_their_mean():
    components = {
        "a": ((1.0, 1.0), 1.0),  # total 2.0
        "b": ((2.0, 2.0), 1.0),  # total 4.0
    }
    assert _component_spread(components) == pytest.approx((4.0 - 2.0) / 3.0)


def test_spread_is_undefined_for_a_single_component():
    assert _component_spread({"a": ((1.0, 1.0), 1.0)}) is None


def test_lambda_is_clamped_to_the_configured_range(config):
    config.lambda_ceiling = 3.0
    stats = _full_statistics()
    stats.home_form = _form(matches=30, scored=6.0, conceded=5.0)
    stats.away_form = _form(matches=30, scored=6.0, conceded=5.0)
    result = estimate(stats, config)
    assert result.lambda_total <= 3.0 + 1e-9
    assert result.lambda_home + result.lambda_away == pytest.approx(result.lambda_total)


def test_supporting_context_moves_the_estimate_only_at_its_own_weight(config):
    """Spec 4.5 - speculative context must not materially move the estimate."""
    from models import ContextInfo

    neutral = estimate(_full_statistics(), config)

    with_context = _full_statistics()
    with_context.context = ContextInfo(
        notes="both first-choice strikers fit", home_multiplier=2.0, away_multiplier=2.0
    )
    boosted = estimate(with_context, config)

    # A doubling of both sides moves lambda by the 5% context weight, not 100%.
    ratio = boosted.lambda_total / neutral.lambda_total
    assert 1.0 < ratio <= 1.0 + config.blend.context + 1e-9
