"""The ranking score and category assignment (spec 7, 8, 9; testing bullet 3)."""

from __future__ import annotations

import pytest
from conftest import make_record

from models import (
    CATEGORY_GOOD,
    CATEGORY_HIGH_ODDS,
    CATEGORY_TOP,
    STATUS_ANALYSED,
    STATUS_NOT_IN_LATEST_RUN,
    STATUS_OUTSIDE_WINDOW,
)
from ranking import (
    REASON_BELOW_PROBABILITY,
    REASON_CONTRADICTED,
    REASON_INSUFFICIENT_DATA,
    REASON_NEGATIVE_EDGE,
    REASON_NO_ODDS,
    REASON_ODDS_RANGE,
    REASON_OUTSIDE_WINDOW,
    REASON_UNRELIABLE,
    assign_category,
    compute_scores,
    evaluate_qualification,
    rank_and_categorise,
    ranked_selections,
)


# --------------------------------------------------------------------------
# Section 7 - the combined score
# --------------------------------------------------------------------------
def test_score_is_the_weighted_sum_of_the_normalised_components(config):
    """``score = W_P * norm(probability) + W_V * norm(value)``."""
    records = [
        make_record("A", "B", probability=0.90, value=0.20),  # best on both
        make_record("C", "D", probability=0.70, value=0.00),  # worst on both
        make_record("E", "F", probability=0.80, value=0.10),  # midway
    ]
    compute_scores(records, config)

    assert records[0].score == pytest.approx(1.0)
    assert records[1].score == pytest.approx(0.0)
    assert records[2].score == pytest.approx(0.6 * 0.5 + 0.4 * 0.5)


def test_ranking_is_never_on_probability_alone(config):
    """A lower probability at a far better price must be able to win."""
    high_probability = make_record("A", "B", probability=0.92, value=0.01, odds=1.30)
    strong_value = make_record("C", "D", probability=0.75, value=0.40, odds=2.60)
    config.w_probability, config.w_value = 0.30, 0.70

    compute_scores([high_probability, strong_value], config)
    assert strong_value.score > high_probability.score


def test_ranking_is_never_on_value_alone(config):
    high_probability = make_record("A", "B", probability=0.92, value=0.01)
    strong_value = make_record("C", "D", probability=0.75, value=0.40)
    config.w_probability, config.w_value = 0.90, 0.10

    compute_scores([high_probability, strong_value], config)
    assert high_probability.score > strong_value.score


def test_weights_must_sum_to_one(config):
    config.w_probability, config.w_value = 0.7, 0.4
    assert any("sum to 1.0" in problem for problem in config.validate())


def test_a_set_with_no_spread_scores_neutrally_rather_than_arbitrarily(config):
    records = [make_record("A", "B", probability=0.8, value=0.1) for _ in range(3)]
    compute_scores(records, config)
    assert all(record.score == pytest.approx(0.5) for record in records)


def test_a_single_record_is_not_forced_to_zero_or_one(config):
    record = make_record()
    compute_scores([record], config)
    assert record.score == pytest.approx(0.5)


def test_records_missing_a_component_are_left_unscored(config):
    scored = make_record("A", "B")
    unscored = make_record("C", "D", probability=None, value=None)
    compute_scores([scored, unscored], config)
    assert scored.score is not None
    assert unscored.score is None


def test_ties_break_on_probability_then_confidence(config):
    """Same score, so the documented tiebreak decides the order."""
    lower = make_record("A", "B", probability=0.80, value=0.10, confidence="High")
    higher = make_record("C", "D", probability=0.90, value=0.10, confidence="Low")
    config.w_probability, config.w_value = 0.0, 1.0  # score depends on value alone

    rank_and_categorise([lower, higher], config)
    assert higher.rank == 1 and lower.rank == 2

    # With identical probabilities, confidence decides.
    a = make_record("E", "F", probability=0.85, value=0.10, confidence="Low")
    b = make_record("G", "H", probability=0.85, value=0.10, confidence="High")
    rank_and_categorise([a, b], config)
    assert b.rank == 1 and a.rank == 2


# --------------------------------------------------------------------------
# Section 8 - categories
# --------------------------------------------------------------------------
def test_top_pick_needs_probability_value_completeness_and_confidence(config):
    record = make_record(probability=0.86, value=0.05, confidence="High")
    assert assign_category(record, config) == CATEGORY_TOP


def test_a_thin_data_set_is_not_a_top_pick(config):
    record = make_record(probability=0.86, value=0.05, confidence="High")
    record.data_completeness = 0.20
    assert assign_category(record, config) != CATEGORY_TOP


def test_low_confidence_is_not_a_top_pick(config):
    record = make_record(probability=0.86, value=0.05, confidence="Low")
    assert assign_category(record, config) != CATEGORY_TOP


def test_high_odds_value_pick_is_a_long_price_with_a_clear_edge(config):
    record = make_record(probability=0.72, value=0.12, odds=2.40, confidence="Medium")
    assert assign_category(record, config) == CATEGORY_HIGH_ODDS


def test_a_long_price_without_an_edge_is_not_a_value_pick(config):
    record = make_record(probability=0.72, value=0.01, odds=2.40, confidence="Medium")
    assert assign_category(record, config) == CATEGORY_GOOD


def test_anything_else_qualifying_is_a_good_pick(config):
    record = make_record(probability=0.74, value=0.03, odds=1.45, confidence="Medium")
    assert assign_category(record, config) == CATEGORY_GOOD


def test_categories_are_never_padded(config):
    """Spec 8 - if only two matches meet the criteria, report two."""
    records = [
        make_record("A", "B", probability=0.86, value=0.06, confidence="High"),
        make_record("C", "D", probability=0.84, value=0.04, confidence="High"),
        make_record("E", "F", probability=0.50, value=0.02),  # below the minimum
    ]
    summary = rank_and_categorise(records, config)
    assert summary.qualified == 2
    assert len(ranked_selections(records)) == 2
    assert sum(summary.by_category.values()) == 2


def test_nothing_qualifying_reports_nothing(config):
    records = [make_record("A", "B", probability=0.20, value=-0.30)]
    summary = rank_and_categorise(records, config)
    assert summary.qualified == 0
    assert ranked_selections(records) == []
    assert summary.binding_constraint


# --------------------------------------------------------------------------
# Section 9 - qualification
# --------------------------------------------------------------------------
def test_a_healthy_record_qualifies(config):
    qualified, reason = evaluate_qualification(make_record(), config)
    assert qualified and reason == ""


def test_below_the_minimum_probability_is_excluded_with_the_figures(config):
    record = make_record(probability=0.65)
    qualified, reason = evaluate_qualification(record, config)
    assert not qualified
    assert REASON_BELOW_PROBABILITY in reason
    assert "65.0%" in reason and "70.0%" in reason


def test_a_thin_form_sample_is_excluded(config):
    record = make_record(form_matches=2)
    qualified, reason = evaluate_qualification(record, config)
    assert not qualified and REASON_INSUFFICIENT_DATA in reason


def test_a_thin_head_to_head_only_excludes_when_configured_to(config):
    record = make_record(h2h_matches=1)
    assert evaluate_qualification(record, config)[0] is True

    config.require_h2h_sample = True
    qualified, reason = evaluate_qualification(record, config)
    assert not qualified and REASON_INSUFFICIENT_DATA in reason


def test_recent_statistics_that_contradict_the_estimate_exclude_it(config):
    """Spec 9 - a model far above every observed rate is not to be trusted."""
    record = make_record(probability=0.95)
    record.home_form_over15_rate = 0.40
    record.away_form_over15_rate = 0.45
    record.h2h_over15_rate = 0.50
    qualified, reason = evaluate_qualification(record, config)
    assert not qualified and REASON_CONTRADICTED in reason


def test_unreliable_data_excludes_the_match(config):
    record = make_record(unreliable_reason="implausible overround 0.667")
    qualified, reason = evaluate_qualification(record, config)
    assert not qualified and REASON_UNRELIABLE in reason


@pytest.mark.parametrize("odds", [1.10, 6.00])
def test_odds_outside_the_configured_range_are_excluded(config, odds):
    qualified, reason = evaluate_qualification(make_record(odds=odds), config)
    assert not qualified and REASON_ODDS_RANGE in reason


def test_a_missing_price_is_excluded(config):
    qualified, reason = evaluate_qualification(make_record(odds=None), config)
    assert not qualified and reason == REASON_NO_ODDS


def test_a_negative_edge_is_excluded(config):
    qualified, reason = evaluate_qualification(make_record(value=-0.05), config)
    assert not qualified and REASON_NEGATIVE_EDGE in reason


def test_a_fixture_outside_the_window_is_excluded_for_that_reason(config):
    record = make_record()
    record.status = STATUS_OUTSIDE_WINDOW
    qualified, reason = evaluate_qualification(record, config)
    assert not qualified and reason == REASON_OUTSIDE_WINDOW


def test_a_fixture_absent_from_this_run_is_excluded_but_kept(config):
    record = make_record()
    record.status = STATUS_NOT_IN_LATEST_RUN
    summary = rank_and_categorise([record], config)
    assert record.qualified is False
    assert record in [record]  # never deleted
    assert summary.qualified == 0


def test_exclusion_reasons_are_recorded_on_the_record(config):
    """Spec 9 - excluded matches stay in the record with the reason."""
    record = make_record(probability=0.40)
    rank_and_categorise([record], config)
    assert record.qualified is False
    assert record.exclusion_reason
    assert record.rank is None and record.category == ""


def test_the_binding_constraint_names_the_commonest_reason(config):
    records = [make_record(f"A{i}", "B", probability=0.55) for i in range(3)]
    records.append(make_record("C", "D", odds=9.0))
    summary = rank_and_categorise(records, config)
    assert summary.binding_constraint == REASON_BELOW_PROBABILITY
    assert summary.best_probability_pct == pytest.approx(85.0)


def test_qualification_is_re_evaluated_when_thresholds_change(config):
    """Spec 2.6 - Ranked Selections is rebuilt under the current thresholds."""
    records = [make_record(probability=0.75)]

    rank_and_categorise(records, config)
    assert records[0].qualified is True

    config.min_probability = 80.0
    rank_and_categorise(records, config)
    assert records[0].qualified is False
    assert REASON_BELOW_PROBABILITY in records[0].exclusion_reason

    config.min_probability = 70.0
    rank_and_categorise(records, config)
    assert records[0].qualified is True


def test_analysed_counts_only_this_runs_fixtures(config):
    """A retained row from an earlier, wider run is not counted again."""
    current = make_record("A", "B")
    retained = make_record("C", "D")
    retained.status = STATUS_NOT_IN_LATEST_RUN
    summary = rank_and_categorise([current, retained], config)
    assert summary.analysed == 1
    assert current.status == STATUS_ANALYSED
