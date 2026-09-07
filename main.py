"""CLI entry point and orchestration.

    python main.py                             prompt for settings, then analyse today (EAT)
    python main.py --no-prompt                 skip all prompts, use defaults

    python main.py --window 14:00-22:00        explicit kick-off window (EAT)
    python main.py --next-hours 6              kick-off within the next 6 hours
    python main.py --min-probability 75        minimum model probability (percent)
    python main.py --min-odds 1.35             minimum Over 1.5 decimal odds

    python main.py --date 2026-09-08           analyse a specific date
    python main.py --fixtures data.csv         use a manual fixture list
    python main.py --output-dir results        override the output folder
    python main.py --dry-run                   analyse without writing Excel

Exit codes: ``0`` success, ``1`` completed with recoverable errors, ``2`` fatal.

This program places no bets and takes no authenticated action of any kind. It
reads public information, estimates probabilities, and writes a spreadsheet.
"""

from __future__ import annotations

import argparse
import sys
import time as time_module
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import gsb_scraper
import odds as odds_module
import probability_model
from config import Config, load_dotenv_file
from excel_writer import ExcelStore, WorkbookLockedError, build_summary_rows, merge_records
from models import (
    STATUS_ANALYSED,
    STATUS_OUTSIDE_WINDOW,
    AnalysisWindow,
    Fixture,
    MatchRecord,
    MatchStatistics,
    RunLogEntry,
    SourceRecord,
    now_utc,
    to_eat,
)
from prompts import PromptAborted, ValidationError, resolve_configuration, safe_write
from ranking import rank_and_categorise
from reporting import RunOutcome, print_report
from statistics_provider import StatisticsProvider
from utils.logging import CountingHandler, get_logger, setup_logging
from utils.naming import load_aliases

__version__ = "1.0.0"

EXIT_OK = 0
EXIT_RECOVERABLE = 1
EXIT_FATAL = 2

log = get_logger("main")


# ==========================================================================
# Command line
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Analyse upcoming soccer fixtures and estimate the probability of "
            "Over 1.5 goals. Analysis and recommendation only - this tool never "
            "places a bet."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    window_group = parser.add_mutually_exclusive_group()
    window_group.add_argument(
        "--window", metavar="HH:MM-HH:MM", help="explicit kick-off window (EAT)"
    )
    window_group.add_argument(
        "--next-hours", type=float, metavar="N", help="kick-off within the next N hours"
    )

    parser.add_argument(
        "--min-probability", type=float, metavar="PCT",
        help="minimum model probability, in percent",
    )
    parser.add_argument(
        "--min-odds", type=float, metavar="ODDS", help="minimum Over 1.5 decimal odds"
    )
    parser.add_argument("--no-prompt", action="store_true", help="skip all prompts, use defaults")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="analyse a specific match date (EAT)")
    parser.add_argument("--fixtures", metavar="PATH", help="use a manual fixture list (CSV)")
    parser.add_argument("--output-dir", metavar="DIR", help="override the output folder")
    parser.add_argument("--dry-run", action="store_true", help="analyse without writing Excel")

    parser.add_argument(
        "--allow-scraping", action="store_true",
        help=(
            "opt in to collecting fixtures from the sportsbook with a browser. "
            "Off by default: see the access policy in gsb_scraper.py"
        ),
    )
    parser.add_argument("--results", metavar="PATH", help="a local results CSV for statistics")
    parser.add_argument(
        "--stats-providers", metavar="LIST",
        help="comma-separated statistics providers, or 'none'",
    )
    parser.add_argument("--no-cache", action="store_true", help="ignore cached pages and feeds")
    parser.add_argument(
        "--dixon-coles", nargs="?", const=0.08, type=float, metavar="RHO",
        help="apply the Dixon-Coles low-score correction (default rho 0.08)",
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="console log level",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def apply_non_promptable_flags(config: Config, args: argparse.Namespace) -> None:
    """Flags that are not part of the section 2 prompt flow."""
    from config import SOURCE_FLAG

    if args.output_dir:
        config.set("output_dir", args.output_dir, SOURCE_FLAG)
    if args.results:
        config.set("results_csv", args.results, SOURCE_FLAG)
    if args.stats_providers:
        config.set("stats_providers", args.stats_providers, SOURCE_FLAG)
    if args.allow_scraping:
        config.set("allow_scraping", True, SOURCE_FLAG)
    if args.no_cache:
        config.set("use_cache", False, SOURCE_FLAG)
    if args.log_level:
        config.set("log_level", args.log_level, SOURCE_FLAG)
    if args.dixon_coles is not None:
        config.set("use_dixon_coles", True, SOURCE_FLAG)
        config.set("dixon_coles_rho", float(args.dixon_coles), SOURCE_FLAG)


def resolve_match_day(raw: str | None) -> date:
    """The match day, in EAT. Defaults to today in the sportsbook's zone."""
    if not raw:
        return to_eat(now_utc()).date()
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError(f"--date must be YYYY-MM-DD (got {raw!r})") from exc


# ==========================================================================
# Analysis of one fixture
# ==========================================================================
def build_record(
    fixture: Fixture,
    stats: MatchStatistics,
    model: probability_model.ModelResult,
    assessment: odds_module.OddsAssessment,
) -> MatchRecord:
    """Fold the fixture, its statistics and its estimates into one row."""
    record = MatchRecord.from_fixture(fixture)

    record.lambda_home = model.lambda_home
    record.lambda_away = model.lambda_away
    record.lambda_total = model.lambda_total
    record.model_probability = model.probability
    record.confidence = model.confidence if model.usable else "Low"
    record.data_completeness = model.data_completeness
    record.model_notes = model.note_text

    record.implied_raw = assessment.implied_raw
    record.implied_fair = assessment.implied_fair
    record.overround = assessment.overround
    record.margin_adjusted = assessment.margin_adjusted
    record.value = assessment.value
    record.ev = assessment.ev
    record.kelly = assessment.kelly

    record.h2h_matches = stats.h2h.meetings or None
    record.h2h_over15_rate = stats.h2h.over15_rate
    record.h2h_avg_goals = stats.h2h.avg_total_goals
    record.h2h_2plus = stats.h2h.count_2plus
    record.h2h_recent = " | ".join(stats.h2h.recent_results)

    record.home_form_matches = stats.home_form.matches or None
    record.home_form_over15_rate = stats.home_form.over15_rate
    record.home_avg_scored = stats.home_form.avg_scored
    record.home_avg_conceded = stats.home_form.avg_conceded
    record.home_avg_total = stats.home_form.avg_total
    record.home_form_2plus = stats.home_form.count_2plus

    record.away_form_matches = stats.away_form.matches or None
    record.away_form_over15_rate = stats.away_form.over15_rate
    record.away_avg_scored = stats.away_form.avg_scored
    record.away_avg_conceded = stats.away_form.avg_conceded
    record.away_avg_total = stats.away_form.avg_total
    record.away_form_2plus = stats.away_form.count_2plus

    record.home_home_matches = stats.home_home_split.matches or None
    record.home_home_over15_rate = stats.home_home_split.over15_rate
    record.home_home_avg_total = stats.home_home_split.avg_total
    record.away_away_matches = stats.away_away_split.matches or None
    record.away_away_over15_rate = stats.away_away_split.over15_rate
    record.away_away_avg_total = stats.away_away_split.avg_total

    record.league_avg_goals = stats.league.avg_goals
    record.league_over15_rate = stats.league.over15_rate
    record.league_trend = stats.league.trend
    record.context_notes = stats.context.notes
    record.review_flags = "; ".join(stats.review_flags)
    record.stats_sources = ", ".join(sorted({s.source for s in stats.sources if s.source}))

    # Section 9: available data is unreliable or self-contradictory. Only the
    # odds-plausibility warnings mean the *inputs* disagree with each other; a
    # merely missing Under 1.5 price is reported, not treated as unreliable.
    contradictions = [w for w in assessment.warnings if "implausible" in w]
    record.unreliable_reason = "; ".join(contradictions)

    if not model.usable:
        record.model_probability = None
        record.exclusion_reason = model.reason
        record.status = STATUS_ANALYSED
    return record


def analyse_fixture(
    fixture: Fixture,
    provider: StatisticsProvider,
    config: Config,
) -> MatchRecord:
    """Statistics, model, and odds comparison for one fixture."""
    stats = provider.statistics_for(fixture)
    model = probability_model.estimate(stats, config)
    assessment = odds_module.assess(
        model.probability, fixture.odds_over_15, fixture.odds_under_15
    )
    return build_record(fixture, stats, model, assessment)


def record_for_unanalysed(fixture: Fixture, status: str, reason: str) -> MatchRecord:
    """A collected fixture that was deliberately not analysed this run."""
    record = MatchRecord.from_fixture(fixture)
    record.status = status
    record.exclusion_reason = reason
    return record


# ==========================================================================
# The run
# ==========================================================================
def run(argv: Sequence[str] | None = None) -> int:
    started = now_utc()
    started_clock = time_module.perf_counter()

    parser = build_parser()
    args = parser.parse_args(argv)

    load_dotenv_file(".env")
    try:
        config = Config.from_env()
    except ValueError as exc:
        safe_write(f"error: {exc}")
        return EXIT_FATAL

    apply_non_promptable_flags(config, args)
    counter: CountingHandler = setup_logging(config.log_level, config.log_file)

    try:
        match_day = resolve_match_day(args.date)
    except ValidationError as exc:
        safe_write(f"error: {exc}")
        return EXIT_FATAL

    # --- section 2: settings --------------------------------------------
    try:
        setup = resolve_configuration(config, args, match_day)
    except PromptAborted as exc:
        safe_write(f"\nCancelled ({exc}). Nothing was written.")
        return EXIT_OK
    except ValidationError as exc:
        safe_write(f"error: {exc}")
        return EXIT_FATAL

    config, window = setup.config, setup.window
    try:
        config.require_valid()
    except ValueError as exc:
        safe_write(f"error: {exc}")
        return EXIT_FATAL

    if not setup.prompted:
        # An unattended run must still be self-documenting (spec 2.5).
        log.info("effective configuration", extra={"config": config.one_line()})
        safe_write(
            f"Analysing {match_day.isoformat()} | window {window.label} EAT | "
            f"min probability {config.min_probability:.1f}% | min odds {config.min_odds:.2f}"
        )
    for warning in setup.warnings:
        log.warning(warning)

    loaded = load_aliases(config.alias_file)
    if loaded:
        log.info("team aliases loaded", extra={"count": loaded, "file": config.alias_file})

    # --- section 3: collect ---------------------------------------------
    try:
        fixtures, fixture_source = gsb_scraper.collect_fixtures(
            config, match_day, fixtures_path=args.fixtures
        )
    except gsb_scraper.CollectionError as exc:
        log.error("fixture collection failed", extra={"error": str(exc)})
        safe_write(f"\nCould not collect fixtures:\n  {exc}")
        return EXIT_FATAL

    log.info(
        "fixtures collected",
        extra={"count": len(fixtures), "day": match_day.isoformat(), "source": fixture_source},
    )

    in_window = [f for f in fixtures if window.contains(f.kickoff_utc)]
    outside_window = [f for f in fixtures if not window.contains(f.kickoff_utc)]

    # --- section 4 and 5: statistics and estimates -----------------------
    provider = StatisticsProvider(config)
    try:
        provider.load(in_window)
    except Exception as exc:  # noqa: BLE001 - statistics are best-effort
        log.error("statistics could not be loaded", extra={"error": str(exc)})

    fresh: list[MatchRecord] = []
    for fixture in in_window:
        try:
            fresh.append(analyse_fixture(fixture, provider, config))
        except Exception as exc:  # noqa: BLE001 - one match must not abort the run
            log.error(
                "analysis failed for a fixture; skipping it",
                extra={"match": fixture.label, "league": fixture.league, "error": str(exc)},
                exc_info=True,
            )
            fresh.append(
                record_for_unanalysed(fixture, STATUS_ANALYSED, f"Analysis failed: {exc}")
            )

    for fixture in outside_window:
        fresh.append(
            record_for_unanalysed(
                fixture, STATUS_OUTSIDE_WINDOW, "Kick-off outside analysis window"
            )
        )

    # --- section 10: merge, rank, persist --------------------------------
    store = ExcelStore(match_day, config)
    try:
        existing = store.load()
    except Exception as exc:  # noqa: BLE001
        log.error("could not read the existing workbook", extra={"error": str(exc)})
        existing = []

    merged = merge_records(existing, fresh)
    summary = rank_and_categorise(merged.records, config, window)

    finished = now_utc()
    duration = time_module.perf_counter() - started_clock

    output_path: Path | None = None
    if not args.dry_run:
        run_log = RunLogEntry(
            started_at=started,
            finished_at=finished,
            duration_seconds=duration,
            window=window.label,
            min_probability=config.min_probability,
            min_odds=config.min_odds,
            fixtures_found=len(fixtures),
            rows_added=merged.added,
            rows_updated=merged.updated,
            warnings=counter.warning_count,
            errors=counter.error_count,
            warning_detail=counter.summary(counter.warnings),
            error_detail=counter.summary(counter.errors),
            config_summary=config.one_line(),
        )
        summary_rows = build_summary_rows(
            match_day=match_day,
            window=window,
            config=config,
            first_run=store.first_run_timestamp(),
            latest_run=finished,
            found=len(fixtures),
            in_window=len(in_window),
            analysed=summary.analysed,
            qualified=summary.qualified,
            by_category=summary.by_category,
            fixture_source=fixture_source,
        )
        sources: list[SourceRecord] = list(provider.sources)
        try:
            output_path = store.save(
                merged.records,
                window=window,
                summary_rows=summary_rows,
                run_log=run_log,
                sources=sources,
            )
        except WorkbookLockedError as exc:
            log.error("workbook locked", extra={"path": str(store.path)})
            safe_write(f"\nerror: {exc}")
            return EXIT_FATAL
        except Exception as exc:  # noqa: BLE001
            log.error("writing the workbook failed", extra={"error": str(exc)}, exc_info=True)
            safe_write(f"\nerror: could not write {store.path}: {exc}")
            return EXIT_FATAL

    # --- section 11: report ----------------------------------------------
    print_report(
        RunOutcome(
            match_day=match_day,
            window=window,
            config=config,
            fixtures_found=len(fixtures),
            in_window=len(in_window),
            analysed=summary.analysed,
            summary=summary,
            records=merged.records,
            output_path=output_path,
            rows_added=merged.added,
            rows_updated=merged.updated,
            fixture_source=fixture_source,
            warnings=counter.warnings,
            errors=counter.errors,
            dry_run=args.dry_run,
        )
    )

    return EXIT_RECOVERABLE if counter.error_count else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point with top-level error handling and the documented exit codes."""
    try:
        return run(argv)
    except PromptAborted:
        safe_write("\nCancelled. Nothing was written.")
        return EXIT_OK
    except KeyboardInterrupt:
        safe_write("\nInterrupted. Nothing was written.")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - last resort, must not traceback at the user
        log.error("fatal error", extra={"error": str(exc)}, exc_info=True)
        safe_write(f"\nfatal: {exc}")
        return EXIT_FATAL


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):  # keep the em dash safe on Windows
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best effort only
            pass
    raise SystemExit(main())
