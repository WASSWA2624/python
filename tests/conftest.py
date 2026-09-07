"""Shared fixtures for the test suite.

The tests import the application modules from the project root, which
``pyproject.toml`` puts on the path via ``pythonpath = ["."]``.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # belt and braces for a bare `pytest tests/...`
    sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from models import EAT, UTC, Fixture, MatchRecord  # noqa: E402
from statistics_provider import MatchResult, ResultsRepository  # noqa: E402
from utils.naming import reset_aliases  # noqa: E402

MATCH_DAY = date(2026, 9, 8)


@pytest.fixture(autouse=True)
def _clean_aliases():
    """Alias overrides are module-level state; no test may leak them."""
    reset_aliases()
    yield
    reset_aliases()


@pytest.fixture
def config() -> Config:
    """A configuration with the spec's defaults and no network access."""
    cfg = Config()
    cfg.stats_providers = "none"
    cfg.use_cache = False
    cfg.alias_file = ""
    return cfg


def eat(hour: int, minute: int = 0, day: date = MATCH_DAY) -> datetime:
    """An aware UTC datetime for a wall-clock time in EAT on *day*."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=EAT).astimezone(UTC)


def make_fixture(
    home: str = "Alpha",
    away: str = "Beta",
    *,
    league: str = "Test League",
    hour: int = 18,
    minute: int = 0,
    day: date = MATCH_DAY,
    over: float | None = 1.40,
    under: float | None = 2.90,
) -> Fixture:
    return Fixture(
        league=league,
        home_team=home,
        away_team=away,
        kickoff_utc=eat(hour, minute, day),
        odds_over_15=over,
        odds_under_15=under,
        source="test",
    )


def make_record(
    home: str = "Alpha",
    away: str = "Beta",
    *,
    probability: float | None = 0.85,
    odds: float | None = 1.50,
    value: float | None = 0.10,
    confidence: str = "High",
    hour: int = 18,
    day: date = MATCH_DAY,
    form_matches: int = 10,
    **overrides,
) -> MatchRecord:
    """A record already carrying everything qualification looks at."""
    record = MatchRecord(
        match_key="",
        league="Test League",
        home_team=home,
        away_team=away,
        kickoff_utc=eat(hour, 0, day),
        odds_over_15=odds,
        model_probability=probability,
        confidence=confidence,
        data_completeness=1.0,
        value=value,
        ev=None if (probability is None or odds is None) else probability * odds - 1,
        home_form_matches=form_matches,
        away_form_matches=form_matches,
        home_form_over15_rate=0.9,
        away_form_over15_rate=0.9,
    )
    record.match_key = record.match_key or _key_for(record)
    for name, value_ in overrides.items():
        setattr(record, name, value_)
    return record


def _key_for(record: MatchRecord) -> str:
    from utils.naming import make_match_key

    return make_match_key(record.league, record.home_team, record.away_team, record.kickoff_utc)


def build_repository(
    *,
    home: str = "Alpha",
    away: str = "Beta",
    league: str = "Test League",
    matches: int = 12,
    home_goals: int = 2,
    away_goals: int = 1,
    reference: date = MATCH_DAY,
) -> ResultsRepository:
    """A repository of synthetic *completed* results, for model tests.

    These are inputs to the model, not statistics presented as real: every one
    is a fabricated fixture between test teams.
    """
    repository = ResultsRepository()
    for index in range(matches):
        played = reference - timedelta(days=7 * (index + 1))
        # Alternate the venue so the home/away splits both have a sample.
        if index % 2 == 0:
            repository.add(
                MatchResult(
                    played, league, home, f"Opponent {index}", home_goals, away_goals, "test"
                )
            )
            repository.add(
                MatchResult(
                    played, league, f"Opponent {index}", away, home_goals, away_goals, "test"
                )
            )
        else:
            repository.add(
                MatchResult(
                    played, league, f"Opponent {index}", home, away_goals, home_goals, "test"
                )
            )
            repository.add(
                MatchResult(
                    played, league, away, f"Opponent {index}", home_goals, away_goals, "test"
                )
            )
    # A handful of meetings between the two sides themselves.
    for index in range(4):
        repository.add(
            MatchResult(
                reference - timedelta(days=120 * (index + 1)),
                league,
                home,
                away,
                2,
                1,
                "test",
            )
        )
    return repository
