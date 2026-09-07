"""Team-name normalisation and match keys (spec 1, 12; testing bullet 4)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from conftest import eat

from models import UTC
from utils.naming import (
    best_match,
    find_alias_cycles,
    load_aliases,
    make_match_key,
    normalise_league,
    normalise_team,
    similarity,
)


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Arsenal FC", "arsenal"),
        ("FC Barcelona", "barcelona"),
        ("A.C. Milan", "ac milan"),
        ("Real Madrid CF", "real madrid"),
        ("  Chelsea  ", "chelsea"),
        ("CHELSEA", "chelsea"),
    ],
)
def test_affixes_and_case_are_stripped(raw, expected):
    assert normalise_team(raw) == expected


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Man Utd", "Manchester United"),
        ("Man United", "Manchester Utd"),
        ("Spurs", "Tottenham Hotspur"),
        ("PSG", "Paris Saint-Germain"),
        ("Wolves", "Wolverhampton Wanderers"),
        ("Inter", "Internazionale"),
        ("Atletico", "Atl. Madrid"),
    ],
)
def test_known_aliases_converge(a, b):
    assert normalise_team(a) == normalise_team(b)


def test_accents_are_folded():
    assert normalise_team("Málaga") == normalise_team("Malaga")
    assert normalise_team("Bayern München") == normalise_team("Bayern Munchen")


def test_ampersands_become_words():
    assert normalise_team("Brighton & Hove Albion") == normalise_team("Brighton and Hove Albion")


def test_distinguishing_tokens_are_never_dropped():
    """ "United" and "City" separate two clubs from one city."""
    assert normalise_team("Manchester United") != normalise_team("Manchester City")
    assert "united" in normalise_team("Leeds United")
    assert "city" in normalise_team("Norwich City")


def test_a_club_named_only_with_noise_keeps_its_name():
    assert normalise_team("FC") != ""
    assert normalise_team("SC Club") != ""


def test_empty_input_normalises_to_empty():
    assert normalise_team("") == ""
    assert normalise_team(None) == ""


@pytest.mark.parametrize(
    ("dotted", "plain"),
    [
        ("A.C. Milan", "AC Milan"),
        ("F.C. Porto", "FC Porto"),
        ("A.S. Roma", "AS Roma"),
        ("R.C.D. Espanyol", "RCD Espanyol"),
        ("W.B.A.", "WBA"),
    ],
)
def test_dotted_abbreviations_match_their_undotted_form(dotted, plain):
    """Stripping the dots must not split "A.C." into two unrecognisable tokens."""
    assert normalise_team(dotted) == normalise_team(plain)


def test_joining_initials_leaves_ordinary_names_alone():
    assert normalise_team("Manchester United") == "manchester united"
    assert normalise_team("Real Madrid") == "real madrid"


def test_league_normalisation_is_case_and_punctuation_insensitive():
    assert normalise_league("English Premier League") == normalise_league("english premier league")
    assert normalise_league("Serie A (Italy)") == "serie a italy"


# --------------------------------------------------------------------------
# Fuzzy matching (spec 12)
# --------------------------------------------------------------------------
def test_identical_names_score_full_marks():
    assert similarity("Arsenal", "Arsenal FC") == 100.0


def test_unrelated_names_score_poorly():
    assert similarity("Arsenal", "Real Madrid") < 60.0


def test_best_match_finds_the_right_candidate():
    candidates = ["Manchester United", "Manchester City", "Real Madrid"]
    name, score = best_match("Man Utd", candidates, threshold=85.0)
    assert name == "Manchester United"
    assert score >= 85.0


def test_below_the_threshold_no_link_is_assumed():
    """Spec 12 - flag for review rather than assuming a link."""
    name, score = best_match("Kampala City", ["Real Madrid", "Barcelona"], threshold=85.0)
    assert name is None
    assert score < 85.0


def test_an_empty_candidate_list_links_to_nothing():
    assert best_match("Anything", [], threshold=85.0) == (None, 0.0)


# --------------------------------------------------------------------------
# The alias file
# --------------------------------------------------------------------------
def test_aliases_load_from_a_file(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"kcca": "kcca fc kampala"}), encoding="utf-8")
    assert load_aliases(path) == 1
    assert normalise_team("KCCA") == normalise_team("KCCA FC Kampala")


def test_comment_keys_are_not_treated_as_aliases(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"_note": "a comment", "x": "y"}), encoding="utf-8")
    assert load_aliases(path) == 1


def test_a_missing_alias_file_is_not_an_error(tmp_path):
    assert load_aliases(tmp_path / "absent.json") == 0
    assert load_aliases(None) == 0


def test_a_non_object_alias_file_is_rejected(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps(["not", "a", "map"]), encoding="utf-8")
    with pytest.raises(ValueError, match="alias"):
        load_aliases(path)


def test_an_alias_cycle_is_refused_rather_than_silently_resolved(tmp_path):
    """Two maps pointing opposite ways would split one club into two."""
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"manchester united": "man utd"}), encoding="utf-8")
    with pytest.raises(ValueError, match="cycle"):
        load_aliases(path)


def test_the_shipped_alias_file_is_consistent():
    """The file the tool ships with must not fight the built-in map."""
    assert load_aliases("data/team_aliases.json") > 0
    assert find_alias_cycles() == set()


def test_the_shipped_alias_file_links_feed_spellings():
    load_aliases("data/team_aliases.json")
    assert normalise_team("Nott'm Forest") == normalise_team("Nottingham Forest")
    assert normalise_team("Ath Madrid") == normalise_team("Atletico Madrid")
    assert normalise_team("M'gladbach") == normalise_team("Borussia Monchengladbach")


# --------------------------------------------------------------------------
# Match keys (spec 1)
# --------------------------------------------------------------------------
def _key(league="Premier League", home="Arsenal", away="Chelsea", when=None):
    return make_match_key(league, home, away, when or eat(18, 0))


def test_the_key_is_stable_for_the_same_fixture():
    assert _key() == _key()


def test_the_key_is_insensitive_to_naming_differences():
    """The whole point: two sources spelling a club differently share a key."""
    assert _key(home="Man Utd") == _key(home="Manchester United")
    assert _key(league="premier league") == _key(league="Premier League")


def test_the_key_distinguishes_different_fixtures():
    assert _key(home="Arsenal") != _key(home="Aston Villa")
    assert _key(away="Chelsea") != _key(away="Everton")
    assert _key(league="Premier League") != _key(league="Championship")


def test_home_and_away_are_not_interchangeable():
    assert _key(home="Arsenal", away="Chelsea") != _key(home="Chelsea", away="Arsenal")


def test_seconds_of_drift_do_not_mint_a_new_key():
    """Collections a few seconds apart must not duplicate a fixture."""
    base = eat(18, 0)
    assert _key(when=base) == _key(when=base + timedelta(seconds=45))
    assert _key(when=base) != _key(when=base + timedelta(minutes=30))


def test_the_key_is_timezone_independent():
    """The same instant expressed in two zones is the same fixture."""
    utc_time = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    assert _key(when=utc_time) == _key(when=utc_time.astimezone(UTC))


def test_a_naive_kickoff_is_read_as_utc():
    naive = datetime(2026, 9, 8, 15, 0)
    aware = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    assert _key(when=naive) == _key(when=aware)


def test_the_key_is_a_sha1_hex_digest():
    key = _key()
    assert len(key) == 40
    assert all(character in "0123456789abcdef" for character in key)
