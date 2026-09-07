"""Qualification, combined score, and categories (spec 7, 8, 9).

Scoring methodology
-------------------
Ranking is never on odds alone and never on probability alone::

    score = (W_PROBABILITY * normalised_probability)
          + (W_VALUE       * normalised_value)

Defaults are ``W_PROBABILITY = 0.60`` and ``W_VALUE = 0.40``; both are
configurable and are required to sum to 1.0.

Both components are min-max normalised to 0..1 **across the analysed set**
before weighting, so neither dominates through scale alone - raw value edges
live in the tenths while probabilities live near 1. When the set has no spread
in a component (one fixture, or every fixture identical), that component
contributes a neutral 0.5 rather than an arbitrary 0 or 1.

Ties break on higher model probability, then on higher confidence.

The normalisation set is the **full retained set** for the date, not just this
run's fixtures, so re-running with a narrower window does not silently rescale
everything (spec 10.2).
"""

from __future__ import annotations

from dataclasses import dataclass

from config import Config
from models import (
    CATEGORY_GOOD,
    CATEGORY_HIGH_ODDS,
    CATEGORY_TOP,
    CONFIDENCE_ORDER,
    STATUS_ANALYSED,
    STATUS_NOT_IN_LATEST_RUN,
    STATUS_OUTSIDE_WINDOW,
    AnalysisWindow,
    MatchRecord,
)

#: Exclusion reasons, in the order section 9 lists them. Kept as constants so
#: the console report can name the binding constraint precisely (spec 11).
REASON_OUTSIDE_WINDOW = "Kick-off outside analysis window"
REASON_NOT_IN_RUN = "Not offered in the latest run"
REASON_NO_ESTIMATE = "No probability could be estimated"
REASON_INSUFFICIENT_DATA = "Insufficient historical data"
REASON_CONTRADICTED = "Recent statistics contradict the estimate"
REASON_BELOW_PROBABILITY = "Below minimum probability"
REASON_UNRELIABLE = "Data unreliable or self-contradictory"
REASON_NO_ODDS = "Over 1.5 price unavailable"
REASON_ODDS_RANGE = "Odds outside configured range"
REASON_NEGATIVE_EDGE = "Edge below minimum value"


@dataclass
class RankingSummary:
    """Counts the console report and Summary sheet need (spec 10.4, 11)."""

    analysed: int = 0
    qualified: int = 0
    by_category: dict[str, int] = None  # type: ignore[assignment]
    exclusions: dict[str, int] = None  # type: ignore[assignment]
    best_probability_pct: float | None = None
    best_probability_label: str = ""
    binding_constraint: str = ""

    def __post_init__(self) -> None:
        if self.by_category is None:
            self.by_category = {}
        if self.exclusions is None:
            self.exclusions = {}


# --------------------------------------------------------------------------
# Section 9 - qualification
# --------------------------------------------------------------------------
def evaluate_qualification(record: MatchRecord, config: Config) -> tuple[bool, str]:
    """Decide whether *record* may be recommended, and why not if not.

    Checks run in the order section 9 lists them so the reported reason is the
    first thing genuinely wrong with the fixture, not an incidental one.
    """
    if record.status == STATUS_OUTSIDE_WINDOW:
        return False, REASON_OUTSIDE_WINDOW
    if record.status == STATUS_NOT_IN_LATEST_RUN:
        return False, REASON_NOT_IN_RUN

    if record.model_probability is None:
        return False, record.exclusion_reason or REASON_NO_ESTIMATE

    # Insufficient historical data (below the configured minimum sample).
    home_n = record.home_form_matches or 0
    away_n = record.away_form_matches or 0
    if home_n < config.min_form_matches or away_n < config.min_form_matches:
        return False, (
            f"{REASON_INSUFFICIENT_DATA} (form samples {home_n}/{away_n}, "
            f"minimum {config.min_form_matches})"
        )
    if config.require_h2h_sample and (record.h2h_matches or 0) < config.min_h2h_matches:
        return False, (
            f"{REASON_INSUFFICIENT_DATA} (head-to-head {record.h2h_matches or 0}, "
            f"minimum {config.min_h2h_matches})"
        )

    # Recent statistics materially contradict the selection.
    observed = [
        rate
        for rate in (
            record.home_form_over15_rate,
            record.away_form_over15_rate,
            record.h2h_over15_rate,
        )
        if rate is not None
    ]
    if observed:
        best_observed = max(observed)
        if record.model_probability - best_observed > config.contradiction_margin:
            return False, (
                f"{REASON_CONTRADICTED} (model {record.model_probability * 100:.1f}% vs "
                f"best observed Over 1.5 rate {best_observed * 100:.1f}%)"
            )

    # The estimated probability falls below the minimum.
    if record.probability_pct is None or record.probability_pct < config.min_probability:
        return False, (
            f"{REASON_BELOW_PROBABILITY} ({record.probability_pct:.1f}% < "
            f"{config.min_probability:.1f}%)"
        )

    # Available data is unreliable or self-contradictory.
    if record.unreliable_reason:
        return False, f"{REASON_UNRELIABLE}: {record.unreliable_reason}"

    # The odds sit outside the configured range, or the edge is negative.
    if record.odds_over_15 is None:
        return False, REASON_NO_ODDS
    if record.odds_over_15 < config.min_odds:
        return False, (
            f"{REASON_ODDS_RANGE} ({record.odds_over_15:.2f} < {config.min_odds:.2f})"
        )
    if record.odds_over_15 > config.max_odds:
        return False, (
            f"{REASON_ODDS_RANGE} ({record.odds_over_15:.2f} > {config.max_odds:.2f})"
        )
    if record.value is None:
        return False, f"{REASON_NEGATIVE_EDGE} (value could not be computed)"
    if record.value < config.min_value:
        return False, (
            f"{REASON_NEGATIVE_EDGE} ({record.value * 100:+.1f} points < "
            f"{config.min_value * 100:+.1f})"
        )

    return True, ""


# --------------------------------------------------------------------------
# Section 7 - combined score
# --------------------------------------------------------------------------
def _normalise(values: list[float]) -> list[float]:
    """Min-max to 0..1; a set with no spread maps to a neutral 0.5."""
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low <= 1e-12:
        return [0.5] * len(values)
    return [(v - low) / (high - low) for v in values]


def compute_scores(records: list[MatchRecord], config: Config) -> None:
    """Assign ``score`` to every record that has both components.

    Normalisation spans every record carrying a probability and a value, which
    is the analysed set for the date. Records missing either keep ``score =
    None``; they cannot qualify anyway.
    """
    scorable = [r for r in records if r.model_probability is not None and r.value is not None]
    if not scorable:
        for record in records:
            record.score = None
        return

    norm_probability = _normalise([r.model_probability for r in scorable])
    norm_value = _normalise([r.value for r in scorable])

    for record in records:
        record.score = None
    for record, np_, nv in zip(scorable, norm_probability, norm_value):
        record.score = config.w_probability * np_ + config.w_value * nv


def _sort_key(record: MatchRecord) -> tuple[float, float, int, str]:
    """Score, then probability, then confidence; name last for determinism."""
    return (
        -(record.score if record.score is not None else -1.0),
        -(record.model_probability if record.model_probability is not None else -1.0),
        -CONFIDENCE_ORDER.get(record.confidence, -1),
        record.label.lower(),
    )


# --------------------------------------------------------------------------
# Section 8 - categories
# --------------------------------------------------------------------------
def assign_category(record: MatchRecord, config: Config) -> str:
    """Group a qualifying match (spec 8). Never pads: a match that fits no
    stronger group is simply a GOOD PICK, and nothing is promoted to fill a
    category out."""
    probability_pct = record.probability_pct or 0.0
    value = record.value if record.value is not None else 0.0
    completeness = record.data_completeness if record.data_completeness is not None else 0.0
    confidence_ok = CONFIDENCE_ORDER.get(record.confidence, -1) >= CONFIDENCE_ORDER.get(
        config.top_pick_min_confidence, 1
    )

    if (
        probability_pct >= config.top_pick_min_probability
        and value >= config.top_pick_min_value
        and completeness >= config.top_pick_min_completeness
        and confidence_ok
    ):
        return CATEGORY_TOP

    if (
        record.odds_over_15 is not None
        and record.odds_over_15 >= config.high_odds_min_odds
        and value >= config.high_odds_min_value
    ):
        return CATEGORY_HIGH_ODDS

    return CATEGORY_GOOD


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def apply_window(records: list[MatchRecord], window: AnalysisWindow) -> None:
    """Mark fixtures outside the window, and clear the mark when back inside.

    Re-marking matters as much as marking: widening the window on a later run
    must bring a previously-excluded fixture back into consideration.
    """
    for record in records:
        if window.contains(record.kickoff_utc):
            if record.status == STATUS_OUTSIDE_WINDOW:
                record.status = STATUS_ANALYSED
        elif record.status != STATUS_NOT_IN_LATEST_RUN:
            record.status = STATUS_OUTSIDE_WINDOW


def rank_and_categorise(
    records: list[MatchRecord],
    config: Config,
    window: AnalysisWindow | None = None,
) -> RankingSummary:
    """Score, qualify, rank and categorise the full retained set.

    Mutates the records in place and returns the counts the report needs.
    ``Ranked Selections`` is rebuilt from this every run (spec 2.6), which is
    why the whole set is re-evaluated rather than only the fresh fixtures.
    """
    if window is not None:
        apply_window(records, window)

    compute_scores(records, config)

    summary = RankingSummary()
    qualifying: list[MatchRecord] = []

    for record in records:
        qualified, reason = evaluate_qualification(record, config)
        record.qualified = qualified
        record.exclusion_reason = "" if qualified else reason
        record.rank = None
        record.category = ""
        # "Analysed" means considered in *this* run: inside the window and
        # carrying an estimate. Retained rows from an earlier, wider run keep
        # their figures but are not counted again (spec 11).
        if record.model_probability is not None and record.status == STATUS_ANALYSED:
            summary.analysed += 1
        if qualified:
            qualifying.append(record)
        elif reason:
            head = reason.split(" (")[0].split(":")[0]
            summary.exclusions[head] = summary.exclusions.get(head, 0) + 1

    qualifying.sort(key=_sort_key)
    for position, record in enumerate(qualifying, start=1):
        record.rank = position
        record.category = assign_category(record, config)
        summary.by_category[record.category] = summary.by_category.get(record.category, 0) + 1

    summary.qualified = len(qualifying)

    in_play = [
        r
        for r in records
        if r.model_probability is not None
        and r.status not in (STATUS_OUTSIDE_WINDOW, STATUS_NOT_IN_LATEST_RUN)
    ]
    if in_play:
        best = max(in_play, key=lambda r: r.model_probability or 0.0)
        summary.best_probability_pct = best.probability_pct
        summary.best_probability_label = best.label
    if not qualifying and summary.exclusions:
        summary.binding_constraint = max(summary.exclusions.items(), key=lambda kv: kv[1])[0]

    return summary


def ranked_selections(records: list[MatchRecord]) -> list[MatchRecord]:
    """The qualifying records in rank order - the ``Ranked Selections`` sheet."""
    return sorted(
        (r for r in records if r.qualified and r.rank is not None),
        key=lambda r: r.rank or 0,
    )
