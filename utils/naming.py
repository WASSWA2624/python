"""Team- and league-name normalisation, fuzzy linking, and match keys.

Sources disagree about club names ("Man Utd" / "Manchester United" /
"Manchester Utd FC"). Spec 12 requires an alias map plus fuzzy matching, and
requires that a link below the similarity threshold be flagged for review
rather than assumed.

``rapidfuzz`` is used when installed; ``difflib`` from the standard library is
a drop-in fallback so the tool still runs without it.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

try:  # pragma: no cover - whichever branch is installed gets exercised
    from rapidfuzz import fuzz as _fuzz

    def _similarity(a: str, b: str) -> float:
        """Similarity in 0..100.

        Deliberately does NOT use ``partial_ratio``. That scorer finds the best
        matching window, so a name wholly contained in a longer one scores near
        100: "Real Madrid" against "Real Madrid Castilla", "Arsenal" against
        "Arsenal Ladies", "Liverpool" against "Liverpool Montevideo". Those are
        different teams, and linking them would feed the wrong side's results
        into the model. Whole-string scorers keep such pairs below the
        threshold, where section 12 says they belong: flagged for review rather
        than assumed. Known abbreviations are the alias map's job, not the
        fuzzy matcher's.
        """
        return max(_fuzz.token_sort_ratio(a, b), _fuzz.ratio(a, b))

    FUZZY_BACKEND = "rapidfuzz"
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def _similarity(a: str, b: str) -> float:
        base = SequenceMatcher(None, a, b).ratio() * 100
        # Token-sort equivalent: compare order-insensitively as well.
        sorted_a = " ".join(sorted(a.split()))
        sorted_b = " ".join(sorted(b.split()))
        tokens = SequenceMatcher(None, sorted_a, sorted_b).ratio() * 100
        return max(base, tokens)

    FUZZY_BACKEND = "difflib"


#: Affixes carrying no identifying information that differ between sources.
_NOISE_TOKENS = frozenset(
    {
        "fc",
        "afc",
        "cf",
        "sc",
        "ac",
        "as",
        "ss",
        "sv",
        "fk",
        "sk",
        "bk",
        "if",
        "ik",
        "cd",
        "ca",
        "cs",
        "ud",
        "sd",
        "rc",
        "rcd",
        "cfr",
        "kv",
        "kaa",
        "rsc",
        "us",
        "nk",
        "hk",
        "mfk",
        "club",
        "clube",
        "calcio",
        "futbol",
        "football",
        "futebol",
        "soccer",
        "the",
        "de",
        "of",
    }
)

#: Tokens that must never be dropped even when they resemble noise, because
#: they distinguish two clubs from the same city.
_PROTECTED = frozenset({"united", "city", "town", "rovers", "wanderers", "athletic", "county"})

#: Tokens that mark a *different* team sharing a club's name - the reserve,
#: youth, or women's side. Two names whose qualifiers differ are never linked,
#: however similar they read: "Barcelona" and "Barcelona B" play in different
#: competitions, and blending their results would corrupt the estimate.
_SQUAD_QUALIFIERS = frozenset(
    {
        "b",
        "c",
        "ii",
        "iii",
        "2",
        "3",
        "u15",
        "u16",
        "u17",
        "u18",
        "u19",
        "u20",
        "u21",
        "u23",
        "youth",
        "juvenil",
        "junior",
        "juniors",
        "academy",
        "reserve",
        "reserves",
        "reserv",
        "amateure",
        "amateur",
        "women",
        "womens",
        "ladies",
        "feminine",
        "femenino",
        "feminin",
        "fem",
        "w",
        "castilla",
        "atletic",
    }
)

#: Conservative token-level abbreviation folding, applied before noise removal
#: so that "Manchester Utd FC" and "Man Utd" converge on the same canonical form.
_TOKEN_SUBSTITUTIONS = {
    "utd": "united",
    "st": "saint",
    "cty": "city",
    "intl": "international",
    "wdrs": "wanderers",
}

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

#: Built-in aliases, extended at runtime from ``data/team_aliases.json``.
BUILTIN_ALIASES: dict[str, str] = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city",
    "spurs": "tottenham hotspur",
    "tottenham": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "brighton": "brighton hove albion",
    "brighton and hove albion": "brighton hove albion",
    "nottm forest": "nottingham forest",
    "sheff utd": "sheffield united",
    "sheff wed": "sheffield wednesday",
    "west brom": "west bromwich albion",
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "bayern": "bayern munich",
    "bayern munchen": "bayern munich",
    "borussia mgladbach": "borussia monchengladbach",
    "mgladbach": "borussia monchengladbach",
    "b monchengladbach": "borussia monchengladbach",
    "dortmund": "borussia dortmund",
    "inter": "inter milan",
    "internazionale": "inter milan",
    "milan": "ac milan",
    "juve": "juventus",
    "atletico": "atletico madrid",
    "atl madrid": "atletico madrid",
    "ath madrid": "atletico madrid",
    "real": "real madrid",
    "barca": "barcelona",
    "ajax amsterdam": "ajax",
    "psv eindhoven": "psv",
    "sporting cp": "sporting lisbon",
    "sporting": "sporting lisbon",
}

_alias_overrides: dict[str, str] = {}


def load_aliases(path: str | Path | None) -> int:
    """Merge a JSON ``{"alias": "canonical"}`` file into the alias map.

    Returns the number of aliases loaded. A missing file is not an error, and
    keys beginning with ``_`` are treated as comments rather than aliases.
    """
    global _alias_overrides
    if path is None:
        return 0
    file = Path(path)
    if not file.is_file():
        return 0
    data = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{file} must contain a JSON object of alias -> canonical name")
    # Keys beginning with an underscore are file-level notes, not aliases.
    added = {
        str(k).strip().lower(): str(v).strip().lower()
        for k, v in data.items()
        if not str(k).startswith("_")
    }
    _alias_overrides = {**_alias_overrides, **added}
    _canonical_cached.cache_clear()

    cycles = find_alias_cycles()
    if cycles:
        raise ValueError(
            f"{file} introduces alias cycles: {', '.join(sorted(cycles))}. "
            "Every alias must point at a canonical name that is not itself an "
            "alias - check the direction of these entries against the built-in "
            "map in utils/naming.py."
        )
    return len(added)


def find_alias_cycles() -> set[str]:
    """Aliases that resolve back onto themselves through the merged map.

    Two maps pointing in opposite directions - "ath madrid" to "atletico
    madrid" in one and the reverse in the other - would otherwise resolve to
    whichever name happened to be asked for, silently splitting one club into
    two. ``_apply_alias`` breaks the loop rather than hanging, so without this
    check the damage would be invisible.
    """
    aliases = {**BUILTIN_ALIASES, **_alias_overrides}
    cycles: set[str] = set()
    for start in aliases:
        seen: set[str] = set()
        current = start
        while current in aliases and current not in seen:
            seen.add(current)
            current = aliases[current]
        if current in seen:
            cycles.add(start)
    return cycles


def reset_aliases() -> None:
    """Drop runtime alias overrides (used by tests)."""
    global _alias_overrides
    _alias_overrides = {}
    _canonical_cached.cache_clear()


def strip_accents(text: str) -> str:
    """Fold accented characters onto ASCII, so Malaga matches Malaga."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise_team(name: str | None) -> str:
    """Return a canonical, comparable form of a team name.

    Lower-cased, accent-folded, punctuation-stripped, with non-identifying
    affixes (FC, SC, Club...) removed and aliases applied. Never returns the
    empty string for non-empty input: if every token looks like noise, the
    stripped original is kept instead.
    """
    if not name:
        return ""
    return _canonical_cached(str(name))


@lru_cache(maxsize=4096)
def _canonical_cached(name: str) -> str:
    text = strip_accents(name).lower()
    text = text.replace("&", " and ")
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""

    text = _join_initials(text)
    text = _apply_alias(text)

    tokens = [_TOKEN_SUBSTITUTIONS.get(t, t) for t in text.split() if t]
    kept = [t for t in tokens if t not in _NOISE_TOKENS or t in _PROTECTED]
    if not kept:  # a club literally named "FC" - keep what we had
        kept = tokens
    cleaned = " ".join(kept).strip()

    return _apply_alias(cleaned)


def _join_initials(text: str) -> str:
    """Rejoin dotted abbreviations that punctuation stripping has split apart.

    "A.C. Milan" arrives here as "a c milan", which shares no token with the
    "ac milan" that "AC Milan" produces - so the two spellings of one club
    would never link. Collapsing runs of single letters back into one token
    fixes that, and does the same for "R.C.D. Espanyol" and "W.B.A.".
    """
    tokens = text.split()
    joined: list[str] = []
    run: list[str] = []
    for token in tokens:
        if len(token) == 1 and token.isalpha():
            run.append(token)
            continue
        if len(run) > 1:
            joined.append("".join(run))
        elif run:
            joined.append(run[0])
        run = []
        joined.append(token)
    if len(run) > 1:
        joined.append("".join(run))
    elif run:
        joined.append(run[0])
    return " ".join(joined)


def _apply_alias(text: str) -> str:
    """Resolve *text* through the alias map, following chains without looping."""
    aliases = {**BUILTIN_ALIASES, **_alias_overrides}
    seen: set[str] = set()
    current = text
    while current in aliases and current not in seen:
        seen.add(current)
        current = aliases[current]
    return current


def normalise_league(name: str | None) -> str:
    """Canonical form of a competition name."""
    if not name:
        return ""
    text = strip_accents(str(name)).lower()
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def squad_qualifiers(name: str) -> frozenset[str]:
    """The squad-qualifier tokens in *name*, if any.

    ``"Barcelona B"`` yields ``{"b"}`` and ``"Barcelona"`` yields the empty
    set, which is what stops the two being treated as one club.
    """
    tokens = set(normalise_team(name).split())
    return frozenset(tokens & _SQUAD_QUALIFIERS)


def similarity(a: str, b: str) -> float:
    """Similarity of two team names in 0..100, after normalisation.

    Returns 0.0 when the two names carry different squad qualifiers - a first
    team and its B side are not the same team at any score.
    """
    na, nb = normalise_team(a), normalise_team(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    if squad_qualifiers(a) != squad_qualifiers(b):
        return 0.0
    return float(_similarity(na, nb))


def best_match(
    name: str,
    candidates: list[str],
    threshold: float = 85.0,
) -> tuple[str | None, float]:
    """Return the closest *candidate* to *name* and its score.

    Returns ``(None, best_score)`` when nothing clears *threshold* - the caller
    must then flag the fixture for review rather than assume a link (spec 12).
    """
    best_name: str | None = None
    best_score = 0.0
    for candidate in candidates:
        score = similarity(name, candidate)
        if score > best_score:
            best_score, best_name = score, candidate
        if best_score == 100.0:
            break
    if best_score < threshold:
        return None, best_score
    return best_name, best_score


def make_match_key(league: str, home: str, away: str, kickoff_utc: datetime) -> str:
    """Stable fixture identifier (spec 1).

    ``sha1(normalised_league | normalised_home | normalised_away | kickoff_iso)``

    The kick-off is normalised to UTC and truncated to whole minutes so a few
    seconds of drift between collections does not mint a new key.
    """
    if kickoff_utc.tzinfo is None:
        kickoff_utc = kickoff_utc.replace(tzinfo=UTC)
    stamp = kickoff_utc.astimezone(UTC).replace(second=0, microsecond=0)
    payload = "|".join(
        (
            normalise_league(league),
            normalise_team(home),
            normalise_team(away),
            stamp.isoformat().replace("+00:00", "Z"),
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
