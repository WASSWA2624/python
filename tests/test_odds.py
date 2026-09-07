"""Margin removal and the value / EV metrics (spec 6, testing bullet 2)."""

from __future__ import annotations

import pytest

from odds import (
    assess,
    expected_value,
    fair_probability,
    implied_probability,
    kelly_fraction,
    overround,
    validate_decimal_odds,
    value_edge,
)


# --------------------------------------------------------------------------
# Raw implied probability
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("odds", "expected"),
    [(2.00, 0.50), (1.25, 0.80), (1.50, 2 / 3), (4.00, 0.25), (1.01, 1 / 1.01)],
)
def test_implied_probability_is_one_over_the_price(odds, expected):
    assert implied_probability(odds) == pytest.approx(expected)


@pytest.mark.parametrize("odds", [None, 0, 1.0, 0.5, -2.0, "abc", float("nan")])
def test_unusable_prices_are_rejected_not_coerced(odds):
    """Decimal odds of 1.0 or less are a data error, not a price."""
    assert validate_decimal_odds(odds) is None
    assert implied_probability(odds) is None


def test_numeric_strings_are_accepted():
    assert validate_decimal_odds("1.85") == pytest.approx(1.85)


# --------------------------------------------------------------------------
# Margin removal (spec 6.1)
# --------------------------------------------------------------------------
def test_overround_is_the_book_total():
    assert overround(1.25, 4.00) == pytest.approx(0.80 + 0.25)


def test_overround_needs_both_sides():
    assert overround(1.25, None) is None
    assert overround(None, 4.00) is None


def test_fair_probability_removes_the_margin():
    """``implied_fair = (1/odds_over) / overround``, as the spec writes it."""
    fair, book, adjusted = fair_probability(1.25, 4.00)
    assert book == pytest.approx(1.05)
    assert fair == pytest.approx(0.80 / 1.05)
    assert adjusted is True


def test_the_fair_pair_sums_to_one():
    """That is the whole point of removing the margin."""
    over_fair, book, _ = fair_probability(1.25, 4.00)
    under_fair, _, _ = fair_probability(4.00, 1.25)
    assert over_fair + under_fair == pytest.approx(1.0)
    assert book > 1.0


def test_fair_is_always_below_raw_on_a_real_book():
    """The raw figure overstates; comparing against it biases toward no value."""
    raw = implied_probability(1.25)
    fair, _, _ = fair_probability(1.25, 4.00)
    assert fair < raw


def test_a_missing_under_price_falls_back_to_raw_and_is_flagged():
    """Spec 6.1 - fall back to implied_raw and flag the row."""
    fair, book, adjusted = fair_probability(1.25, None)
    assert fair == pytest.approx(0.80)
    assert book is None
    assert adjusted is False

    assessment = assess(0.85, 1.25, None)
    assert assessment.margin_adjusted is False
    assert any("margin-unadjusted" in w for w in assessment.warnings)


def test_a_missing_over_price_yields_nothing_rather_than_a_guess():
    fair, book, adjusted = fair_probability(None, 4.00)
    assert (fair, book, adjusted) == (None, None, False)


# --------------------------------------------------------------------------
# Value and EV (spec 6.2)
# --------------------------------------------------------------------------
def test_value_is_the_probability_edge():
    assert value_edge(0.85, 0.78) == pytest.approx(0.07)
    assert value_edge(0.70, 0.78) == pytest.approx(-0.08)


def test_expected_value_is_the_return_per_unit_staked():
    assert expected_value(0.85, 1.25) == pytest.approx(0.85 * 1.25 - 1)
    assert expected_value(0.80, 1.25) == pytest.approx(0.0)


def test_missing_inputs_give_no_metric_rather_than_zero():
    assert value_edge(None, 0.78) is None
    assert value_edge(0.85, None) is None
    assert expected_value(None, 1.25) is None
    assert expected_value(0.85, None) is None


def test_ev_is_comparable_across_odds_levels_where_value_is_not():
    """Spec 6.2 - EV is the more comparable figure across different prices."""
    short = assess(0.90, 1.15, 6.60)
    long = assess(0.45, 2.60, 1.60)
    # A similar probability edge at a much longer price is worth far more.
    assert short.value == pytest.approx(long.value, abs=0.03)
    assert long.ev > short.ev


def test_kelly_is_reported_but_never_negative():
    assert kelly_fraction(0.85, 1.50) > 0
    assert kelly_fraction(0.40, 1.50) == 0.0
    assert kelly_fraction(0.85, None) is None


# --------------------------------------------------------------------------
# The full assessment
# --------------------------------------------------------------------------
def test_assessment_reports_all_three_figures_the_spec_asks_for():
    assessment = assess(0.85, 1.25, 4.00)
    assert assessment.implied_raw == pytest.approx(0.80)
    assert assessment.implied_fair == pytest.approx(0.80 / 1.05)
    assert assessment.overround == pytest.approx(1.05)
    assert assessment.margin_adjusted is True
    assert assessment.warnings == []


def test_value_is_computed_against_the_fair_figure_not_the_raw_one():
    """The single most important rule in section 6."""
    assessment = assess(0.85, 1.25, 4.00)
    assert assessment.value == pytest.approx(0.85 - assessment.implied_fair)
    assert assessment.value != pytest.approx(0.85 - assessment.implied_raw)


def test_an_impossible_book_is_flagged_as_unreliable():
    """An overround below 1.0 cannot occur on a real two-way market."""
    assessment = assess(0.85, 3.00, 3.00)  # book of 0.667
    assert any("implausible" in w for w in assessment.warnings)


def test_a_wildly_wide_book_is_flagged_as_probably_mismatched():
    assessment = assess(0.85, 1.20, 1.30)  # book of 1.60
    assert any("implausible" in w for w in assessment.warnings)


def test_a_normal_book_raises_no_warning():
    assert assess(0.85, 1.30, 3.40).warnings == []


def test_has_positive_edge_reflects_the_sign_of_value():
    assert assess(0.90, 1.30, 3.40).has_positive_edge
    assert not assess(0.50, 1.30, 3.40).has_positive_edge
    assert not assess(None, 1.30, 3.40).has_positive_edge
