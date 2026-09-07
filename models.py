"""Domain records shared by every stage of the pipeline.

Two conventions hold throughout:

* **Every timestamp is stored in UTC** and rendered in EAT only for display
  (spec 1). ``EAT`` below is the sportsbook's local zone, Africa/Kampala.
* **Every probability and rate is a fraction in 0..1**, never a percentage.
  Excel renders them with a percent format, and the console multiplies by 100.
  Configuration thresholds are the one exception: the spec states them in
  percent (``MIN_PROBABILITY = 70.0``), so they are converted at the point of
  comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

try:  # Windows may lack the IANA database unless `tzdata` is installed.
    from zoneinfo import ZoneInfo

    EAT = ZoneInfo("Africa/Kampala")
except Exception:  # pragma: no cover - fallback path
    EAT = timezone(timedelta(hours=3), name="EAT")

UTC = timezone.utc

#: Status values written to the workbook (spec 10.2).
STATUS_ANALYSED = "Analysed"
STATUS_OUTSIDE_WINDOW = "Outside analysis window"
STATUS_NOT_IN_LATEST_RUN = "Not in latest run"

#: Category labels (spec 8).
CATEGORY_TOP = "TOP PICK"
CATEGORY_GOOD = "GOOD PICK"
CATEGORY_HIGH_ODDS = "HIGH-ODDS VALUE PICK"

CONFIDENCE_ORDER = {"Low": 0, "Medium": 1, "High": 2, "": -1}


def now_utc() -> datetime:
    """Current time, timezone-aware, in UTC."""
    return datetime.now(tz=UTC)


def to_utc(value: datetime) -> datetime:
    """Coerce *value* to an aware UTC datetime, assuming UTC if naive."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_eat(value: datetime) -> datetime:
    """Render *value* in East Africa Time."""
    return to_utc(value).astimezone(EAT)


def match_day_of(kickoff_utc: datetime) -> date:
    """The EAT calendar day a kick-off belongs to (spec 1)."""
    return to_eat(kickoff_utc).date()


def eat_day_bounds(day: date) -> tuple[datetime, datetime]:
    """The first and last instant of an EAT calendar day, as aware datetimes."""
    start = datetime.combine(day, time(0, 0), tzinfo=EAT)
    end = datetime.combine(day, time(23, 59, 59), tzinfo=EAT)
    return start, end


@dataclass(frozen=True)
class AnalysisWindow:
    """The kick-off range actually analysed within a match day (spec 1, 2.1).

    Held as concrete aware datetimes rather than clock times so that the
    "next N hours" form can be clamped to the end of the match day once, at
    resolution time, and then simply asked whether it contains a kick-off.
    """

    start: datetime
    end: datetime
    kind: str = "full-day"  # full-day | next-hours | custom

    @classmethod
    def full_day(cls, day: date) -> AnalysisWindow:
        start, end = eat_day_bounds(day)
        return cls(start=start, end=end, kind="full-day")

    @classmethod
    def from_times(cls, day: date, start: time, end: time, kind: str = "custom") -> AnalysisWindow:
        return cls(
            start=datetime.combine(day, start, tzinfo=EAT),
            end=datetime.combine(day, end, tzinfo=EAT),
            kind=kind,
        )

    @classmethod
    def next_hours(cls, day: date, hours: float, reference: datetime | None = None) -> AnalysisWindow:
        """From *reference* (default now) forward *hours*, clamped to the day.

        The start is clamped to the beginning of the match day too, so asking
        for "the next 6 hours" while analysing tomorrow yields tomorrow morning
        rather than a window that has already begun today.
        """
        day_start, day_end = eat_day_bounds(day)
        ref = to_eat(reference or now_utc())
        start = min(max(ref, day_start), day_end)
        end = min(ref + timedelta(hours=hours), day_end)
        if end < start:
            end = start
        return cls(start=start, end=end, kind="next-hours")

    def contains(self, kickoff_utc: datetime) -> bool:
        """Is this kick-off inside the window? Both ends are inclusive."""
        moment = to_utc(kickoff_utc)
        return to_utc(self.start) <= moment <= to_utc(self.end)

    @property
    def label(self) -> str:
        """``14:30 - 22:30`` for display and for the workbook."""
        return f"{to_eat(self.start):%H:%M} - {to_eat(self.end):%H:%M}"

    @property
    def is_full_day(self) -> bool:
        return self.kind == "full-day"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.label} EAT"


@dataclass
class SourceRecord:
    """Provenance for one statistic (spec 4, 10.4 Data Sources)."""

    statistic: str
    source: str
    endpoint: str = ""
    retrieved_at: datetime | None = None
    notes: str = ""


@dataclass
class FormStats:
    """Recent-form aggregate for one team (spec 4.2/4.3)."""

    matches: int = 0
    over15_rate: float | None = None
    avg_scored: float | None = None
    avg_conceded: float | None = None
    avg_total: float | None = None
    count_2plus: int | None = None
    source: str = ""

    @property
    def available(self) -> bool:
        return self.matches > 0 and self.avg_total is not None


@dataclass
class H2HStats:
    """Head-to-head aggregate (spec 4.1)."""

    meetings: int = 0
    over15_rate: float | None = None
    avg_total_goals: float | None = None
    count_2plus: int | None = None
    recent_results: list[str] = field(default_factory=list)
    home_goal_share: float | None = None
    source: str = ""

    @property
    def available(self) -> bool:
        return self.meetings > 0 and self.avg_total_goals is not None


@dataclass
class LeagueStats:
    """League context (spec 4.4)."""

    name: str = ""
    matches: int = 0
    avg_goals: float | None = None
    over15_rate: float | None = None
    home_goal_share: float | None = None
    trend: str = ""
    source: str = ""

    @property
    def available(self) -> bool:
        return self.avg_goals is not None


@dataclass
class ContextInfo:
    """Supporting context (spec 4.5). Defaults to strictly neutral."""

    notes: str = ""
    #: Multiplicative adjustments to expected goals; 1.0 means "no information".
    home_multiplier: float = 1.0
    away_multiplier: float = 1.0
    source: str = ""

    @property
    def available(self) -> bool:
        return self.home_multiplier != 1.0 or self.away_multiplier != 1.0


@dataclass
class MatchStatistics:
    """Everything section 4 asks for, for one fixture."""

    h2h: H2HStats = field(default_factory=H2HStats)
    home_form: FormStats = field(default_factory=FormStats)
    away_form: FormStats = field(default_factory=FormStats)
    home_home_split: FormStats = field(default_factory=FormStats)
    away_away_split: FormStats = field(default_factory=FormStats)
    league: LeagueStats = field(default_factory=LeagueStats)
    context: ContextInfo = field(default_factory=ContextInfo)
    sources: list[SourceRecord] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)

    def completeness(self) -> float:
        """Share of the section 4 inputs that were actually available.

        Context is deliberately excluded from the denominator: it is optional
        by nature, and counting it would cap completeness below 1.0 for every
        fixture even when all statistical inputs were present.
        """
        parts = [
            self.h2h.available,
            self.home_form.available,
            self.away_form.available,
            self.home_home_split.available,
            self.away_away_split.available,
            self.league.available,
        ]
        return sum(1 for p in parts if p) / len(parts)


@dataclass
class Fixture:
    """An upcoming fixture and its markets, as collected (spec 3.2)."""

    league: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    odds_over_15: float | None = None
    odds_under_15: float | None = None
    odds_over_25: float | None = None
    odds_under_25: float | None = None
    odds_home: float | None = None
    odds_draw: float | None = None
    odds_away: float | None = None
    odds_btts_yes: float | None = None
    odds_btts_no: float | None = None
    collected_at: datetime = field(default_factory=now_utc)
    source: str = ""

    def __post_init__(self) -> None:
        self.kickoff_utc = to_utc(self.kickoff_utc)
        self.collected_at = to_utc(self.collected_at)

    @property
    def match_key(self) -> str:
        from utils.naming import make_match_key

        return make_match_key(self.league, self.home_team, self.away_team, self.kickoff_utc)

    @property
    def kickoff_eat(self) -> datetime:
        return to_eat(self.kickoff_utc)

    @property
    def match_day(self) -> date:
        return match_day_of(self.kickoff_utc)

    @property
    def label(self) -> str:
        return f"{self.home_team} vs {self.away_team}"


@dataclass
class MatchRecord:
    """A fixture plus every derived figure - the unit that is persisted.

    One row of ``All Matches``. Ranking operates on a list of these, and the
    Excel layer serialises them, so a record loaded back from a previous run is
    indistinguishable from one produced by the current run. That is what makes
    re-ranking across the full retained set (spec 10.2) possible.
    """

    # --- identity -------------------------------------------------------
    match_key: str
    league: str
    home_team: str
    away_team: str
    kickoff_utc: datetime

    # --- markets --------------------------------------------------------
    odds_over_15: float | None = None
    odds_under_15: float | None = None
    odds_over_25: float | None = None
    odds_under_25: float | None = None
    odds_home: float | None = None
    odds_draw: float | None = None
    odds_away: float | None = None
    odds_btts_yes: float | None = None
    odds_btts_no: float | None = None

    # --- model (spec 5) -------------------------------------------------
    lambda_home: float | None = None
    lambda_away: float | None = None
    lambda_total: float | None = None
    model_probability: float | None = None  # fraction 0..1
    confidence: str = ""
    data_completeness: float | None = None
    model_notes: str = ""

    # --- odds comparison (spec 6) ---------------------------------------
    implied_raw: float | None = None
    implied_fair: float | None = None
    overround: float | None = None
    margin_adjusted: bool = False
    value: float | None = None  # model probability minus fair implied
    ev: float | None = None
    kelly: float | None = None

    # --- statistics (spec 4) --------------------------------------------
    h2h_matches: int | None = None
    h2h_over15_rate: float | None = None
    h2h_avg_goals: float | None = None
    h2h_2plus: int | None = None
    h2h_recent: str = ""
    home_form_matches: int | None = None
    home_form_over15_rate: float | None = None
    home_avg_scored: float | None = None
    home_avg_conceded: float | None = None
    home_avg_total: float | None = None
    home_form_2plus: int | None = None
    away_form_matches: int | None = None
    away_form_over15_rate: float | None = None
    away_avg_scored: float | None = None
    away_avg_conceded: float | None = None
    away_avg_total: float | None = None
    away_form_2plus: int | None = None
    home_home_matches: int | None = None
    home_home_over15_rate: float | None = None
    home_home_avg_total: float | None = None
    away_away_matches: int | None = None
    away_away_over15_rate: float | None = None
    away_away_avg_total: float | None = None
    league_avg_goals: float | None = None
    league_over15_rate: float | None = None
    league_trend: str = ""
    context_notes: str = ""
    review_flags: str = ""
    #: Set when the inputs are self-contradictory or a name link could not
    #: be trusted; drives the section 9 reliability exclusion.
    unreliable_reason: str = ""

    # --- ranking (spec 7, 8, 9) -----------------------------------------
    score: float | None = None
    rank: int | None = None
    category: str = ""
    qualified: bool = False
    status: str = STATUS_ANALYSED
    exclusion_reason: str = ""

    # --- bookkeeping ----------------------------------------------------
    first_seen_utc: datetime | None = None
    last_updated_utc: datetime | None = None
    collected_at_utc: datetime | None = None
    fixture_source: str = ""
    stats_sources: str = ""

    # ------------------------------------------------------------------
    @classmethod
    def from_fixture(cls, fixture: Fixture) -> MatchRecord:
        return cls(
            match_key=fixture.match_key,
            league=fixture.league,
            home_team=fixture.home_team,
            away_team=fixture.away_team,
            kickoff_utc=fixture.kickoff_utc,
            odds_over_15=fixture.odds_over_15,
            odds_under_15=fixture.odds_under_15,
            odds_over_25=fixture.odds_over_25,
            odds_under_25=fixture.odds_under_25,
            odds_home=fixture.odds_home,
            odds_draw=fixture.odds_draw,
            odds_away=fixture.odds_away,
            odds_btts_yes=fixture.odds_btts_yes,
            odds_btts_no=fixture.odds_btts_no,
            collected_at_utc=fixture.collected_at,
            fixture_source=fixture.source,
        )

    @property
    def kickoff_eat(self) -> datetime:
        return to_eat(self.kickoff_utc)

    @property
    def match_day(self) -> date:
        return match_day_of(self.kickoff_utc)

    @property
    def label(self) -> str:
        return f"{self.home_team} vs {self.away_team}"

    @property
    def probability_pct(self) -> float | None:
        """Model probability as a percentage, for threshold comparison."""
        return None if self.model_probability is None else self.model_probability * 100.0

    @property
    def confidence_rank(self) -> int:
        return CONFIDENCE_ORDER.get(self.confidence, -1)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class RunLogEntry:
    """One row of the ``Run Log`` sheet (spec 10.4)."""

    started_at: datetime
    finished_at: datetime
    duration_seconds: float
    window: str
    min_probability: float
    min_odds: float
    fixtures_found: int
    rows_added: int
    rows_updated: int
    warnings: int
    errors: int
    warning_detail: str = ""
    error_detail: str = ""
    config_summary: str = ""
