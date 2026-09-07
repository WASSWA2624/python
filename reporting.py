"""The console report (spec 11).

Restates the settings that were actually used, gives the headline counts, lists
the top selections with a rationale grounded in the real figures, and closes
with the estimate disclaimer.

When nothing qualifies it says so plainly and names the binding constraint, so
a user who has over-tightened a threshold can see that immediately rather than
wondering whether the run failed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from config import DISCLAIMER, Config
from models import AnalysisWindow, MatchRecord
from ranking import RankingSummary, ranked_selections

#: How many selections the console lists. The workbook always holds them all.
TOP_N = 10


@dataclass
class RunOutcome:
    """What the run did, as the report needs to describe it."""

    match_day: date
    window: AnalysisWindow
    config: Config
    fixtures_found: int
    in_window: int
    analysed: int
    summary: RankingSummary
    records: Sequence[MatchRecord]
    output_path: Path | None = None
    rows_added: int = 0
    rows_updated: int = 0
    fixture_source: str = ""
    warnings: Sequence[str] = ()
    errors: Sequence[str] = ()
    dry_run: bool = False


def format_probability(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def format_points(value: float | None) -> str:
    """A probability edge, in percentage points."""
    return "-" if value is None else f"{value * 100:+.1f}"


def format_odds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def build_rationale(record: MatchRecord) -> str:
    """One or two sentences grounded in this fixture's actual figures.

    Only clauses whose data really exists are included - an absent statistic is
    left out rather than described with a placeholder (spec 12).
    """
    parts: list[str] = []

    opening = f"Over 1.5 probability {format_probability(record.model_probability)}"
    if record.lambda_total is not None:
        opening += f" (expected total goals {record.lambda_total:.2f})"
    parts.append(opening + ".")

    form_clauses: list[str] = []
    if record.home_form_2plus is not None and record.home_form_matches:
        form_clauses.append(
            f"{record.home_team} has produced 2+ goals in "
            f"{record.home_form_2plus} of their last {record.home_form_matches}"
        )
    if record.away_form_2plus is not None and record.away_form_matches:
        form_clauses.append(
            f"{record.away_team} in {record.away_form_2plus} of {record.away_form_matches}"
        )
    if form_clauses:
        parts.append("; ".join(form_clauses) + ".")

    if record.h2h_matches and record.h2h_2plus is not None:
        parts.append(
            f"Their last {record.h2h_matches} meetings went Over 1.5 {record.h2h_2plus} times."
        )

    if record.implied_fair is not None and record.value is not None:
        adjusted = (
            "Fair implied probability"
            if record.margin_adjusted
            else "Implied probability (margin unadjusted)"
        )
        parts.append(
            f"{adjusted} {format_probability(record.implied_fair)}, giving an edge of "
            f"{format_points(record.value)} points."
        )

    if record.confidence:
        completeness = (
            f", data completeness {record.data_completeness * 100:.0f}%"
            if record.data_completeness is not None
            else ""
        )
        parts.append(f"Confidence: {record.confidence}{completeness}.")

    return " ".join(parts)


def _selection_table(selections: Sequence[MatchRecord]) -> list[str]:
    """The rank table, sized to its contents."""
    headers = ("Rank", "Match", "Odds", "Model Prob", "Value", "Confidence")
    rows = [
        (
            str(record.rank or ""),
            record.label,
            format_odds(record.odds_over_15),
            format_probability(record.model_probability),
            format_points(record.value),
            record.confidence or "-",
        )
        for record in selections
    ]
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers))
    ]
    lines = [" | ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))]
    lines.append("-+-".join("-" * width for width in widths))
    for row in rows:
        lines.append(" | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return lines


def build_report(outcome: RunOutcome) -> list[str]:
    """The full console report, as lines."""
    config = outcome.config
    lines: list[str] = []

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"DATE: {_long_date(outcome.match_day)}")
    lines.append(f"WINDOW: {outcome.window.label} EAT")
    lines.append(
        f"THRESHOLDS: min probability {config.min_probability:.1f}% | "
        f"min odds {config.min_odds:.2f}"
    )
    if outcome.fixture_source:
        lines.append(f"FIXTURES FROM: {outcome.fixture_source}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"MATCHES FOUND: {outcome.fixtures_found}")
    lines.append(f"IN WINDOW: {outcome.in_window}")
    lines.append(f"MATCHES ANALYSED: {outcome.analysed}")
    lines.append(f"QUALIFIED (OVER 1.5): {outcome.summary.qualified}")

    selections = ranked_selections(list(outcome.records))
    if selections:
        by_category = outcome.summary.by_category
        breakdown = ", ".join(
            f"{count} {name.lower()}{'s' if count != 1 else ''}"
            for name, count in sorted(by_category.items(), key=lambda kv: -kv[1])
        )
        if breakdown:
            lines.append(f"CATEGORIES: {breakdown}")
        lines.append("")

        shown = selections[:TOP_N]
        heading = f"TOP {len(shown)} SELECTION" + ("S" if len(shown) != 1 else "")
        lines.append(heading)
        lines.extend(_selection_table(shown))
        lines.append("")

        for record in shown:
            lines.append(f"  #{record.rank} {record.label}  [{record.category}]")
            lines.append(f"     {build_rationale(record)}")
            lines.append("")
    else:
        lines.append("")
        lines.extend(_nothing_qualified_lines(outcome))
        lines.append("")

    if outcome.dry_run:
        lines.append("Dry run: no workbook was written.")
    elif outcome.output_path is not None:
        lines.append(
            f"Saved to: {outcome.output_path} "
            f"({outcome.rows_updated} rows updated, {outcome.rows_added} added)"
        )

    if outcome.warnings:
        lines.append(f"Warnings: {len(outcome.warnings)} (see the log and the Run Log sheet)")
    if outcome.errors:
        lines.append(f"Errors: {len(outcome.errors)} (see the log and the Run Log sheet)")

    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")
    return lines


def _nothing_qualified_lines(outcome: RunOutcome) -> list[str]:
    """Say plainly that nothing qualified, and name the binding constraint."""
    config = outcome.config
    summary = outcome.summary
    lines = ["NO MATCHES QUALIFIED."]

    if outcome.fixtures_found == 0:
        lines.append(
            "  No fixtures were collected at all. Check the fixture source, or supply "
            "a list with --fixtures <path.csv>."
        )
        return lines
    if outcome.in_window == 0:
        lines.append(
            f"  None of the {outcome.fixtures_found} fixtures found kick off inside "
            f"{outcome.window.label} EAT. Widen the window with --window or --next-hours."
        )
        return lines

    if summary.best_probability_pct is not None:
        lines.append(
            f"  No matches met the {config.min_probability:.1f}% minimum within "
            f"{outcome.window.label} EAT; the best was "
            f"{summary.best_probability_pct:.1f}% ({summary.best_probability_label})."
        )
    if summary.binding_constraint:
        count = summary.exclusions.get(summary.binding_constraint, 0)
        lines.append(f"  Binding constraint: {summary.binding_constraint} ({count} fixtures).")
    if summary.exclusions:
        lines.append("  Exclusions by reason:")
        for reason, count in sorted(summary.exclusions.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {count:>4}  {reason}")
    return lines


def _long_date(value: date) -> str:
    """``7 September 2026``.

    Built from the parts rather than with ``%-d``, which is a glibc extension
    and raises on Windows - the platform the spec targets.
    """
    return f"{value.day} {value:%B %Y}"


def print_report(outcome: RunOutcome, writer=None) -> list[str]:
    """Emit the report and return its lines (so tests can assert on them)."""
    from prompts import safe_write

    lines = build_report(outcome)
    emit = writer or safe_write
    for line in lines:
        emit(line)
    return lines
