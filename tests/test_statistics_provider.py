"""External statistics: aggregation, provenance, and never fabricating (spec 4, 12).

The rule under test throughout: when a source cannot supply a figure it is
marked unavailable and lowers ``data_completeness``. Nothing is invented.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import MATCH_DAY, build_repository, make_fixture

from statistics_provider import (
    MatchResult,
    ResultsRepository,
    StatisticsProvider,
    country_in_league_name,
    division_for_league,
    season_codes,
    summarise_form,
    summarise_h2h,
    summarise_league,
)
from utils.naming import normalise_team


# ==========================================================================
# The result store
# ==========================================================================
def test_results_are_indexed_by_team_and_league():
    repository = ResultsRepository()
    repository.add(MatchResult(date(2026, 8, 1), "L", "Arsenal", "Chelsea", 2, 1, "test"))
    assert len(repository) == 1
    assert repository.team_names == ["Arsenal", "Chelsea"]


def test_only_matches_before_the_fixture_are_considered():
    """A model must never see a result from after the match it is estimating."""
    repository = ResultsRepository()
    repository.add(MatchResult(MATCH_DAY - timedelta(days=7), "L", "A", "B", 2, 1, "t"))
    repository.add(MatchResult(MATCH_DAY + timedelta(days=7), "L", "A", "B", 5, 5, "t"))

    played = repository.matches_for(normalise_team("A"), before=MATCH_DAY)
    assert len(played) == 1
    assert played[0].total_goals == 3


def test_venue_filtering_gives_the_home_and_away_splits():
    repository = ResultsRepository()
    repository.add(MatchResult(date(2026, 8, 1), "L", "A", "X", 2, 0, "t"))
    repository.add(MatchResult(date(2026, 8, 8), "L", "Y", "A", 1, 1, "t"))

    key = normalise_team("A")
    assert len(repository.matches_for(key, before=MATCH_DAY, venue="home")) == 1
    assert len(repository.matches_for(key, before=MATCH_DAY, venue="away")) == 1
    assert len(repository.matches_for(key, before=MATCH_DAY)) == 2


def test_head_to_head_finds_meetings_at_either_venue():
    repository = ResultsRepository()
    repository.add(MatchResult(date(2026, 8, 1), "L", "A", "B", 2, 1, "t"))
    repository.add(MatchResult(date(2026, 5, 1), "L", "B", "A", 0, 3, "t"))
    repository.add(MatchResult(date(2026, 6, 1), "L", "A", "C", 1, 1, "t"))

    meetings = repository.head_to_head(normalise_team("A"), normalise_team("B"), before=MATCH_DAY)
    assert len(meetings) == 2


def test_a_name_below_the_similarity_threshold_is_not_linked():
    """Spec 12 - flag for review rather than assuming a link."""
    repository = ResultsRepository()
    repository.add(MatchResult(date(2026, 8, 1), "L", "Arsenal", "Chelsea", 2, 1, "t"))
    key, score = repository.resolve_team("Kampala City", 85.0)
    assert key is None and score < 85.0


# ==========================================================================
# Aggregation (spec 4.1-4.4)
# ==========================================================================
def test_form_aggregates_scored_conceded_and_over_rates():
    results = [
        MatchResult(date(2026, 8, 1), "L", "A", "X", 2, 1, "t"),  # A scored 2, conceded 1
        MatchResult(date(2026, 8, 8), "L", "Y", "A", 0, 1, "t"),  # A scored 1, conceded 0
    ]
    form = summarise_form(results, normalise_team("A"), "test")

    assert form.matches == 2
    assert form.avg_scored == pytest.approx(1.5)
    assert form.avg_conceded == pytest.approx(0.5)
    assert form.avg_total == pytest.approx(2.0)
    assert form.over15_rate == pytest.approx(0.5)  # 3 goals and 1 goal
    assert form.count_2plus == 1
    assert form.available is True


def test_an_empty_form_sample_is_unavailable_not_zero():
    form = summarise_form([], normalise_team("A"), "test")
    assert form.matches == 0
    assert form.avg_total is None
    assert form.over15_rate is None
    assert form.available is False


def test_head_to_head_drops_meetings_older_than_the_configured_age(config):
    """Spec 4.1 - discount meetings older than roughly three years."""
    recent = MatchResult(MATCH_DAY - timedelta(days=200), "L", "A", "B", 2, 1, "t")
    ancient = MatchResult(MATCH_DAY - timedelta(days=365 * 6), "L", "A", "B", 0, 0, "t")

    h2h = summarise_h2h([recent, ancient], normalise_team("A"), MATCH_DAY, config, "L", "test")
    assert h2h.meetings == 1
    assert h2h.avg_total_goals == pytest.approx(3.0)


def test_older_meetings_carry_less_weight(config):
    """Two 3-goal games and one recent 0-goal game must not average to 2.0."""
    fresh = MatchResult(MATCH_DAY - timedelta(days=30), "L", "A", "B", 0, 0, "t")
    older = [
        MatchResult(MATCH_DAY - timedelta(days=900), "L", "A", "B", 2, 1, "t"),
        MatchResult(MATCH_DAY - timedelta(days=1000), "L", "A", "B", 2, 1, "t"),
    ]
    weighted = summarise_h2h([fresh, *older], normalise_team("A"), MATCH_DAY, config, "L", "t")
    unweighted = sum(m.total_goals for m in [fresh, *older]) / 3
    assert weighted.avg_total_goals < unweighted


def test_a_meeting_in_another_competition_is_halved(config):
    """Spec 4.1 - materially different circumstances carry less weight."""
    same = MatchResult(MATCH_DAY - timedelta(days=100), "Premier League", "A", "B", 4, 0, "t")
    other = MatchResult(MATCH_DAY - timedelta(days=100), "Cup", "A", "B", 0, 0, "t")

    both = summarise_h2h(
        [same, other], normalise_team("A"), MATCH_DAY, config, "Premier League", "t"
    )
    assert both.avg_total_goals > 2.0  # the 4-goal league game outweighs the cup tie


def test_an_empty_head_to_head_is_unavailable():
    from config import Config

    h2h = summarise_h2h([], normalise_team("A"), MATCH_DAY, Config(), "L", "test")
    assert h2h.meetings == 0 and h2h.available is False


def test_league_baselines_are_computed_from_the_results():
    results = [
        MatchResult(date(2026, 8, 1), "L", "A", "B", 2, 1, "t"),
        MatchResult(date(2026, 8, 2), "L", "C", "D", 1, 0, "t"),
    ]
    league = summarise_league(results, "L", "test")
    assert league.matches == 2
    assert league.avg_goals == pytest.approx(2.0)
    assert league.over15_rate == pytest.approx(0.5)
    assert league.available is True


def test_an_empty_league_is_unavailable():
    assert summarise_league([], "L", "test").available is False


# ==========================================================================
# League mapping - the guard against analysing the wrong country
# ==========================================================================
def test_known_competitions_map_to_their_division_code():
    assert division_for_league("English Premier League") == "E0"
    assert division_for_league("La Liga") == "SP1"
    assert division_for_league("Bundesliga") == "D1"
    assert division_for_league("Serie A") == "I1"


def test_a_named_country_blocks_a_cross_border_match():
    """ "Uganda Premier League" must never be analysed on English results."""
    assert country_in_league_name("Uganda Premier League") == "uganda"
    assert division_for_league("Uganda Premier League") is None
    assert division_for_league("Kenya Premier League") is None


def test_an_unqualified_premier_league_still_means_england():
    assert division_for_league("Premier League") == "E0"


def test_an_unknown_competition_maps_to_nothing():
    assert division_for_league("Some Invitational Trophy") is None
    assert division_for_league("") is None


def test_season_codes_run_newest_first():
    assert season_codes(date(2026, 9, 8), 2) == ["2627", "2526"]
    assert season_codes(date(2026, 3, 1), 1) == ["2526"]  # before July is last season


# ==========================================================================
# Per-fixture assembly (spec 4, 12)
# ==========================================================================
def _provider(config, repository) -> StatisticsProvider:
    provider = StatisticsProvider(config, repository)
    provider._loaded = True  # the repository is supplied directly
    return provider


def test_a_fixture_with_full_history_gets_every_statistic(config):
    provider = _provider(config, build_repository())
    stats = provider.statistics_for(make_fixture("Alpha", "Beta", league="Test League"))

    assert stats.home_form.available
    assert stats.away_form.available
    assert stats.home_home_split.available
    assert stats.away_away_split.available
    assert stats.h2h.available
    assert stats.league.available
    assert stats.completeness() == pytest.approx(1.0)


def test_an_unknown_team_lowers_completeness_rather_than_being_invented(config):
    """Spec 12 - the headline rule. Nothing is fabricated to fill the gap."""
    provider = _provider(config, build_repository())
    stats = provider.statistics_for(make_fixture("Wholly Unknown FC", "Beta"))

    assert stats.home_form.available is False
    assert stats.home_form.avg_total is None
    assert stats.completeness() < 1.0
    assert any("not linked" in flag for flag in stats.review_flags)


def test_every_statistic_is_attributed_to_a_source(config):
    """Spec 4, 10.4 - record the source and retrieval time for each statistic."""
    provider = _provider(config, build_repository())
    fixture = make_fixture("Alpha", "Beta")
    stats = provider.statistics_for(fixture)

    assert stats.sources
    for record in stats.sources:
        assert record.statistic.startswith(fixture.label)
        assert record.retrieved_at is not None


def test_missing_statistics_are_recorded_as_unavailable(config):
    provider = _provider(config, build_repository())
    stats = provider.statistics_for(make_fixture("Wholly Unknown FC", "Beta"))
    notes = {record.statistic.split(": ")[-1]: record.notes for record in stats.sources}
    assert notes["home form"] == "unavailable"


def test_per_fixture_provenance_accumulates_for_the_workbook(config):
    provider = _provider(config, build_repository())
    provider.statistics_for(make_fixture("Alpha", "Beta"))
    provider.statistics_for(make_fixture("Alpha", "Beta", hour=20))
    assert len(provider.fixture_sources) == 14  # seven statistics per fixture


def test_supporting_context_stays_neutral_without_a_source(config):
    """Spec 4.5 - speculative news must not materially move the estimate."""
    provider = _provider(config, build_repository())
    stats = provider.statistics_for(make_fixture("Alpha", "Beta"))
    assert stats.context.home_multiplier == 1.0
    assert stats.context.away_multiplier == 1.0
    assert stats.context.available is False


def test_a_failing_loader_does_not_abort_the_run(config):
    """Spec 12 - one bad source is survivable."""
    from statistics_provider import ResultsLoader

    class Broken(ResultsLoader):
        name = "broken source"

        def load(self, repository, fixtures):
            raise RuntimeError("feed is down")

    provider = StatisticsProvider(config, ResultsRepository())
    provider.build_loaders = lambda: [Broken()]
    provider.load([make_fixture()])

    assert provider.sources
    assert "loader failed" in provider.sources[0].notes


def test_the_local_results_loader_reads_a_csv(config, tmp_path):
    path = tmp_path / "results.csv"
    path.write_text(
        "date,league,home,away,home_goals,away_goals\n"
        "2026-08-01,Test League,Alpha,Beta,2,1\n"
        "2026-08-08,Test League,Beta,Alpha,0,0\n",
        encoding="utf-8",
    )
    from statistics_provider import LocalResultsLoader

    repository = ResultsRepository()
    sources = LocalResultsLoader(str(path)).load(repository, [])
    assert len(repository) == 2
    assert "2 completed matches" in sources[0].notes


def test_a_missing_results_csv_is_reported_not_fatal(config, tmp_path):
    from statistics_provider import LocalResultsLoader

    sources = LocalResultsLoader(str(tmp_path / "absent.csv")).load(ResultsRepository(), [])
    assert "unavailable" in sources[0].notes


def test_the_pandas_and_csv_parsers_agree(config):
    """The fast path must produce exactly what the fallback produces."""
    pytest.importorskip("pandas")
    from statistics_provider import FootballDataCoUkLoader

    payload = (
        b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n"
        b"E0,01/08/2026,Arsenal,Chelsea,2,1,H\n"
        b"E0,08/08/2026,Chelsea,Arsenal,0,0,D\n"
    )

    loader = FootballDataCoUkLoader(config)
    with_pandas = ResultsRepository()
    assert loader._parse(payload, with_pandas, "Premier League") == 2

    without_pandas = ResultsRepository()
    loader._rows_via_pandas = staticmethod(lambda _text: None)
    assert loader._parse(payload, without_pandas, "Premier League") == 2
    assert len(with_pandas) == len(without_pandas) == 2
