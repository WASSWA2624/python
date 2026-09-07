"""Fixture collection: the manual CSV path, parsing, and hygiene (spec 3, 12).

Nothing here touches the network. The live collector is exercised only for its
access policy, which is the part that must never regress: automated collection
is off by default and an explicit ``Disallow`` is never overridden (spec 0).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import MATCH_DAY, eat, make_fixture

from gsb_scraper import (
    BotProtectionDetected,
    CollectionError,
    CollectionNotPermitted,
    GsbScraper,
    check_robots,
    classify_over_under,
    collect_fixtures,
    dedupe_fixtures,
    extract_fixtures_from_json,
    filter_upcoming,
    looks_like_bot_protection,
    parse_decimal_odds,
    parse_fixtures_csv,
    parse_kickoff,
    restrict_to_day,
)
from models import EAT, UTC

CSV_HEADER = "League,Home Team,Away Team,Date,Time,Over 1.5,Under 1.5\n"


def write_csv(tmp_path, body: str, header: str = CSV_HEADER):
    path = tmp_path / "fixtures.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


# ==========================================================================
# Value parsing
# ==========================================================================
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1.45", 1.45), ("1,45", 1.45), (2.10, 2.10), (" 3.5 ", 3.5), (7, 7.0)],
)
def test_decimal_odds_are_parsed_forgivingly(raw, expected):
    assert parse_decimal_odds(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "-", "n/a", "SUS", "abc", "1.00", "0.5", 1.0])
def test_unusable_prices_become_none_rather_than_a_guess(raw):
    assert parse_decimal_odds(raw) is None


def test_a_naive_kickoff_is_read_in_the_sportsbooks_timezone():
    """Spec 1 - EAT is the sportsbook's local zone."""
    parsed = parse_kickoff("2026-09-08 18:00", assume="EAT")
    assert parsed == datetime(2026, 9, 8, 18, 0, tzinfo=EAT).astimezone(UTC)
    assert parsed.tzinfo is not None


def test_an_explicit_offset_is_honoured():
    assert parse_kickoff("2026-09-08T15:00:00Z") == datetime(2026, 9, 8, 15, 0, tzinfo=UTC)


def test_epoch_seconds_and_milliseconds_are_recognised():
    seconds = 1789200000
    assert parse_kickoff(seconds) == parse_kickoff(seconds * 1000)


def test_a_bare_clock_time_needs_a_reference_day():
    assert parse_kickoff("18:30", assume="EAT") is None
    assert parse_kickoff("18:30", assume="EAT", reference_day=MATCH_DAY) == eat(18, 30)


def test_an_unreadable_kickoff_is_none():
    assert parse_kickoff("sometime on Tuesday") is None
    assert parse_kickoff(None) is None


# ==========================================================================
# The manual CSV path (spec 3.4)
# ==========================================================================
def test_a_csv_fixture_list_is_loaded(tmp_path):
    path = write_csv(tmp_path, "Premier League,Arsenal,Chelsea,2026-09-08,18:00,1.30,3.40\n")
    fixtures = parse_fixtures_csv(path, reference_day=MATCH_DAY)

    assert len(fixtures) == 1
    fixture = fixtures[0]
    assert fixture.home_team == "Arsenal"
    assert fixture.away_team == "Chelsea"
    assert fixture.league == "Premier League"
    assert fixture.odds_over_15 == pytest.approx(1.30)
    assert fixture.odds_under_15 == pytest.approx(3.40)
    assert fixture.kickoff_eat.hour == 18


def test_header_names_are_matched_case_insensitively_through_aliases(tmp_path):
    path = write_csv(
        tmp_path,
        "Premier League,Arsenal,Chelsea,2026-09-08,18:00,1.30,3.40\n",
        header="COMPETITION,home,away,match date,kickoff_time,O1.5,U1.5\n",
    )
    assert len(parse_fixtures_csv(path, reference_day=MATCH_DAY)) == 1


def test_the_optional_markets_are_captured(tmp_path):
    path = write_csv(
        tmp_path,
        "L,Arsenal,Chelsea,2026-09-08,18:00,1.30,3.40,1.85,2.5,3.4,2.7\n",
        header=CSV_HEADER.rstrip("\n") + ",Over 2.5,1,X,2\n",
    )
    fixture = parse_fixtures_csv(path, reference_day=MATCH_DAY)[0]
    assert fixture.odds_over_25 == pytest.approx(1.85)
    assert (fixture.odds_home, fixture.odds_draw, fixture.odds_away) == (2.5, 3.4, 2.7)


def test_a_missing_required_column_is_a_clear_error(tmp_path):
    path = write_csv(tmp_path, "18:00,1.30\n", header="Time,Over 1.5\n")
    with pytest.raises(CollectionError, match="missing required column"):
        parse_fixtures_csv(path, reference_day=MATCH_DAY)


def test_a_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(CollectionError, match="not found"):
        parse_fixtures_csv(tmp_path / "absent.csv")


def test_a_row_with_no_teams_is_skipped_not_fatal(tmp_path):
    path = write_csv(
        tmp_path,
        "L,,Chelsea,2026-09-08,18:00,1.30,3.40\nL,Arsenal,Chelsea,2026-09-08,19:00,1.30,3.40\n",
    )
    assert len(parse_fixtures_csv(path, reference_day=MATCH_DAY)) == 1


def test_a_row_with_an_unreadable_kickoff_is_skipped_not_fatal(tmp_path):
    path = write_csv(
        tmp_path,
        "L,Arsenal,Chelsea,later,soon,1.30,3.40\nL,A,B,2026-09-08,19:00,1.30,3.40\n",
    )
    assert len(parse_fixtures_csv(path, reference_day=MATCH_DAY)) == 1


def test_a_missing_price_is_left_missing(tmp_path):
    """Spec 12 - never fabricate; an absent price stays absent."""
    path = write_csv(tmp_path, "L,Arsenal,Chelsea,2026-09-08,18:00,,\n")
    fixture = parse_fixtures_csv(path, reference_day=MATCH_DAY)[0]
    assert fixture.odds_over_15 is None
    assert fixture.odds_under_15 is None


# ==========================================================================
# Fixture-set hygiene (spec 3.3, 12)
# ==========================================================================
def test_duplicate_listings_collapse_on_the_match_key():
    first = make_fixture("Man Utd", "Arsenal", over=1.30, under=None)
    second = make_fixture("Manchester United", "Arsenal", over=1.32, under=3.10)

    merged = dedupe_fixtures([first, second])
    assert len(merged) == 1
    assert merged[0].odds_over_15 == pytest.approx(1.32)  # later listing wins
    assert merged[0].odds_under_15 == pytest.approx(3.10)


def test_a_missing_price_never_overwrites_one_we_already_have():
    priced = make_fixture("A", "B", over=1.30, under=3.40)
    unpriced = make_fixture("A", "B", over=None, under=None)
    merged = dedupe_fixtures([priced, unpriced])
    assert merged[0].odds_over_15 == pytest.approx(1.30)


def test_matches_that_have_already_started_are_excluded():
    """Spec 1 - "upcoming" means strictly in the future."""
    now = eat(18, 0)
    started = make_fixture("A", "B", hour=17)
    upcoming = make_fixture("C", "D", hour=19)

    remaining = filter_upcoming([started, upcoming], reference=now)
    assert [f.home_team for f in remaining] == ["C"]


def test_a_fixture_exactly_at_the_reference_time_is_not_upcoming():
    now = eat(18, 0)
    assert filter_upcoming([make_fixture(hour=18)], reference=now) == []


def test_fixtures_are_restricted_to_the_eat_match_day():
    today = make_fixture("A", "B", hour=23, minute=30)
    tomorrow = make_fixture("C", "D", hour=1, day=MATCH_DAY + timedelta(days=1))

    kept = restrict_to_day([today, tomorrow], MATCH_DAY)
    assert [f.home_team for f in kept] == ["A"]


def test_the_full_day_is_collected_before_the_window_is_applied(tmp_path):
    """Spec 3.1 - the window is a later filter, never a collection limit."""
    rows = "".join(
        f"L,Home{hour},Away{hour},2026-09-08,{hour:02d}:00,1.40,2.90\n" for hour in range(1, 24)
    )
    fixtures, description = collect_fixtures(
        _config_for(tmp_path),
        MATCH_DAY,
        fixtures_path=str(write_csv(tmp_path, rows)),
        reference=datetime(2026, 9, 7, 0, 0, tzinfo=UTC),
    )
    assert len(fixtures) == 23
    assert "manual fixture list" in description


def _config_for(tmp_path):
    from config import Config

    config = Config()
    config.output_dir = str(tmp_path)
    config.use_cache = False
    return config


# ==========================================================================
# Structured JSON
# ==========================================================================
def test_fixtures_are_read_out_of_a_json_payload():
    payload = {
        "events": [
            {
                "competition": {"name": "Premier League"},
                "homeTeam": {"name": "Arsenal"},
                "awayTeam": {"name": "Chelsea"},
                "startTime": "2026-09-08T15:00:00Z",
                "markets": [
                    {"name": "Over/Under 1.5", "selection": "Over 1.5", "odds": 1.30},
                    {"name": "Over/Under 1.5", "selection": "Under 1.5", "odds": 3.40},
                ],
            }
        ]
    }
    fixtures = extract_fixtures_from_json(payload)
    assert len(fixtures) == 1
    assert fixtures[0].home_team == "Arsenal"
    assert fixtures[0].odds_over_15 == pytest.approx(1.30)
    assert fixtures[0].odds_under_15 == pytest.approx(3.40)


@pytest.mark.parametrize(
    ("text", "side"),
    [
        ("Over 1.5", "over"),
        ("Under 1.5", "under"),
        ("Over/Under 1.5 | Over 1.5 | 1.30", "over"),
        ("Over/Under 1.5 | Under 1.5 | 3.40", "under"),
        ("Total Goals Over 1.5", "over"),
        ("Totals Under 1.5", "under"),
        ("o", "over"),
        ("u", "under"),
    ],
)
def test_the_market_name_does_not_decide_the_selection(text, side):
    """A market called "Over/Under 1.5" names both sides and settles neither.

    Reading it as an Over would lose the Under price, and with it the margin
    removal section 6.1 requires.
    """
    assert classify_over_under(text) == side


@pytest.mark.parametrize("text", ["Over/Under 1.5", "O/U 1.5", "Goals 1.5", "", "Discover 1.5"])
def test_an_ambiguous_label_is_skipped_rather_than_guessed(text):
    assert classify_over_under(text) is None


def test_a_json_node_without_both_teams_is_ignored():
    assert extract_fixtures_from_json({"events": [{"homeTeam": {"name": "Arsenal"}}]}) == []


def test_json_fixtures_are_deduplicated():
    node = {
        "home": "Arsenal",
        "away": "Chelsea",
        "startTime": "2026-09-08T15:00:00Z",
        "league": "Premier League",
    }
    assert len(extract_fixtures_from_json({"a": [node, dict(node)]})) == 1


# ==========================================================================
# Access policy (spec 0) - the part that must never regress
# ==========================================================================
def test_automated_collection_is_off_by_default(config):
    """Permission could not be established, so the default is not to collect."""
    assert config.allow_scraping is False
    with pytest.raises(CollectionNotPermitted, match="off by default"):
        GsbScraper(config).ensure_permitted()


def test_the_refusal_points_at_the_manual_fixture_list(config):
    with pytest.raises(CollectionNotPermitted) as excinfo:
        GsbScraper(config).ensure_permitted()
    assert "--fixtures" in str(excinfo.value)


def test_an_explicit_disallow_is_never_overridden(config, monkeypatch):
    import gsb_scraper
    from gsb_scraper import RobotsDecision

    config.allow_scraping = True  # even when the operator has opted in
    monkeypatch.setattr(
        gsb_scraper,
        "check_robots",
        lambda *a, **k: RobotsDecision(False, "robots.txt disallows it"),
    )
    with pytest.raises(CollectionNotPermitted, match="not overridable"):
        GsbScraper(config).ensure_permitted()


def test_an_unreadable_robots_file_is_unknown_not_permission(monkeypatch):
    """A 403 must not be read as a yes."""
    import urllib.error
    import urllib.request

    def forbidden(*_a, **_k):
        raise urllib.error.HTTPError("https://example.test", 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    decision = check_robots("https://example.test/page", "TestBot")
    assert decision.allowed is None
    assert decision.explicitly_disallowed is False


@pytest.mark.parametrize(
    "html",
    [
        "<html><head><title>Just a moment...</title></head></html>",
        "<html>Checking your browser before accessing</html>",
        "<html><body>Please complete the CAPTCHA</body></html>",
        "<html>Attention Required! Cloudflare</html>",
    ],
)
def test_a_challenge_page_is_recognised(html):
    assert looks_like_bot_protection(html) is True


def test_a_real_page_is_not_mistaken_for_a_challenge():
    assert (
        looks_like_bot_protection("<html><body>Arsenal v Chelsea 18:00 1.30</body></html>") is False
    )


def test_a_challenge_page_stops_collection_rather_than_being_worked_around(config):
    from gsb_scraper import parse_fixtures_html

    with pytest.raises(BotProtectionDetected) as excinfo:
        parse_fixtures_html("<html><title>Just a moment...</title></html>", config)
    assert "does not attempt to bypass" in str(excinfo.value)
    assert "--fixtures" in str(excinfo.value)
