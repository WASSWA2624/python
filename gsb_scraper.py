"""Fixture and odds collection (spec 3).

Access policy
-------------
Section 0 requires reviewing the sportsbook's terms and ``robots.txt`` before
implementing automated collection, and requires falling back to a manually
supplied fixture list if automated collection is not permitted.

At the time of writing, ``https://gsb.ug/robots.txt``, the homepage and the
fixtures page all return **HTTP 403** to non-browser clients: the site sits
behind edge bot protection and its crawling policy cannot be read. Because
permission could not be established, this module treats collection as follows:

* **the manual CSV path (section 3.4) is the default and supported route**;
* the Playwright collector is fully implemented but **opt-in**, gated behind
  ``--allow-scraping`` (or ``OVER15_ALLOW_SCRAPING=true``), which the operator
  sets only after satisfying themselves that the site's terms permit it;
* an explicit ``Disallow`` in ``robots.txt`` is **never** overridden;
* if a bot-protection challenge is served, the collector stops with a clear
  message. It does not attempt to solve, evade, or work around the challenge,
  and it performs no authenticated action of any kind (section 0).

Nothing here places, stages, or authorises a bet.
"""

from __future__ import annotations

import csv
import json
import re
import time as time_module
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from config import Config
from models import EAT, UTC, Fixture, eat_day_bounds, now_utc, to_utc
from utils.logging import get_logger

log = get_logger("scraper")

#: Markers that identify a bot-protection interstitial rather than content.
BOT_PROTECTION_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "cf_chl_",
    "checking your browser",
    "attention required",
    "access denied",
    "enable javascript and cookies to continue",
    "captcha",
    "ddos protection",
)

#: Column aliases accepted in a manual fixtures CSV (spec 3.4).
CSV_ALIASES: dict[str, tuple[str, ...]] = {
    "league": ("league", "competition", "tournament", "country_league"),
    "home_team": ("home_team", "home", "hometeam", "home team", "team_home"),
    "away_team": ("away_team", "away", "awayteam", "away team", "team_away"),
    "kickoff_utc": ("kickoff_utc", "kick_off_utc", "kickoff utc", "start_utc"),
    "kickoff_eat": (
        "kickoff_eat",
        "kick_off_eat",
        "kickoff eat",
        "kickoff",
        "start_time",
        "kick_off",
    ),
    "date": ("date", "match_date", "match date"),
    "time": ("time", "kickoff_time", "start"),
    "odds_over_15": ("odds_over_15", "over_15", "over1.5", "o1.5", "over 1.5", "over15"),
    "odds_under_15": ("odds_under_15", "under_15", "under1.5", "u1.5", "under 1.5", "under15"),
    "odds_over_25": ("odds_over_25", "over_25", "over2.5", "o2.5", "over 2.5"),
    "odds_under_25": ("odds_under_25", "under_25", "under2.5", "u2.5", "under 2.5"),
    "odds_home": ("odds_home", "home_odds", "1", "odds_1"),
    "odds_draw": ("odds_draw", "draw_odds", "x", "odds_x"),
    "odds_away": ("odds_away", "away_odds", "2", "odds_2"),
    "odds_btts_yes": ("odds_btts_yes", "btts_yes", "btts"),
    "odds_btts_no": ("odds_btts_no", "btts_no"),
}

#: Key aliases used when reading a structured JSON payload.
JSON_HOME_KEYS = ("home", "hometeam", "home_team", "homename", "competitor1", "team1", "localteam")
JSON_AWAY_KEYS = (
    "away",
    "awayteam",
    "away_team",
    "awayname",
    "competitor2",
    "team2",
    "visitorteam",
)
JSON_TIME_KEYS = (
    "starttime",
    "start_time",
    "kickoff",
    "kickofftime",
    "startdate",
    "date",
    "eventdate",
    "scheduled",
)
JSON_LEAGUE_KEYS = (
    "league",
    "competition",
    "tournament",
    "category",
    "leaguename",
    "competitionname",
)

_DECIMAL = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*$")
_TIME_IN_TEXT = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")


class CollectionError(RuntimeError):
    """Collection failed in a way the operator needs to see."""


class CollectionNotPermitted(CollectionError):
    """Automated collection is disallowed or has not been opted into."""


class BotProtectionDetected(CollectionError):
    """A challenge page was served. We stop rather than work around it."""


# ==========================================================================
# robots.txt
# ==========================================================================
@dataclass
class RobotsDecision:
    """What ``robots.txt`` says about fetching a URL."""

    allowed: bool | None  # None = could not be determined
    reason: str
    crawl_delay: float | None = None

    @property
    def explicitly_disallowed(self) -> bool:
        return self.allowed is False


def check_robots(url: str, user_agent: str, timeout: float = 15.0) -> RobotsDecision:
    """Fetch and interpret ``robots.txt`` for *url*.

    A 403 or a network failure yields ``allowed=None`` - unknown, not
    permission. Only a real ``Disallow`` yields ``allowed=False``.
    """
    parsed = urlparse(url)
    robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "/robots.txt")
    request = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return RobotsDecision(
                True, f"{robots_url} returned 404; no crawling restrictions published"
            )
        return RobotsDecision(
            None, f"{robots_url} returned HTTP {exc.code}; crawling policy unknown"
        )
    except Exception as exc:  # noqa: BLE001 - network errors are all equivalent here
        return RobotsDecision(
            None, f"{robots_url} could not be fetched ({exc}); crawling policy unknown"
        )

    parser = RobotFileParser()
    parser.parse(body.splitlines())
    allowed = parser.can_fetch(user_agent, url)
    delay = parser.crawl_delay(user_agent)
    return RobotsDecision(
        allowed=bool(allowed),
        reason=f"{robots_url} {'allows' if allowed else 'disallows'} {url} for this user agent",
        crawl_delay=float(delay) if delay else None,
    )


def looks_like_bot_protection(html: str) -> bool:
    """Is this a challenge page rather than the fixture list?"""
    head = html[:6000].lower()
    return any(marker in head for marker in BOT_PROTECTION_MARKERS)


# ==========================================================================
# Value parsing helpers
# ==========================================================================
def parse_decimal_odds(raw: Any) -> float | None:
    """Parse a decimal price, tolerating commas and stray markup."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value > 1.0 else None
    text = str(raw).strip().replace(" ", " ")
    if not text or text in {"-", "--", "n/a", "N/A", "SUS"}:
        return None
    match = _DECIMAL.match(text.replace(",", "."))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if value > 1.0 else None


def parse_kickoff(
    raw: Any,
    *,
    assume: str = "EAT",
    reference_day: date | None = None,
) -> datetime | None:
    """Parse a kick-off into an aware UTC datetime.

    Naive values are interpreted in the zone named by *assume* - EAT by
    default, since that is the sportsbook's local timezone (spec 1). Epoch
    seconds and milliseconds are recognised too, as sportsbook JSON commonly
    uses them.
    """
    if raw is None or raw == "":
        return None
    zone = EAT if assume.upper() == "EAT" else UTC

    if isinstance(raw, datetime):
        return to_utc(raw if raw.tzinfo else raw.replace(tzinfo=zone))

    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        number = float(raw)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        if 1e8 < number < 1e11:
            return datetime.fromtimestamp(number, tz=UTC)

    text = str(raw).strip()
    if not text:
        return None
    normalised = text.replace("Z", "+00:00").replace("/", "-")
    try:
        parsed = datetime.fromisoformat(normalised)
        return to_utc(parsed if parsed.tzinfo else parsed.replace(tzinfo=zone))
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%d-%m-%Y %H:%M",
        "%m-%d-%Y %H:%M",
        "%d-%m-%y %H:%M",
        "%Y-%m-%d",
        "%d-%m-%Y",
    ):
        try:
            parsed = datetime.strptime(normalised, fmt)
            return to_utc(parsed.replace(tzinfo=zone))
        except ValueError:
            continue

    # A bare clock time such as "18:30" only means something with a date.
    clock = _TIME_IN_TEXT.search(text)
    if clock and reference_day is not None:
        hour, minute = int(clock.group(1)), int(clock.group(2))
        return to_utc(datetime.combine(reference_day, time(hour, minute), tzinfo=zone))
    return None


# ==========================================================================
# Manual fixture list (spec 3.4)
# ==========================================================================
def _header_index(fieldnames: Sequence[str]) -> dict[str, str]:
    """Map canonical field names to the CSV's actual header spellings."""
    lookup: dict[str, str] = {}
    lowered = {name.strip().lower(): name for name in fieldnames if name}
    for canonical, aliases in CSV_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                lookup[canonical] = lowered[alias]
                break
    return lookup


def _field_reader(row: dict, index: dict[str, str]) -> Callable[[str], str]:
    """A reader bound to one CSV row, returning "" for absent columns."""

    def get(field: str) -> str:
        column = index.get(field)
        return (row.get(column) or "").strip() if column else ""

    return get


def parse_fixtures_csv(
    path: str | Path,
    *,
    reference_day: date | None = None,
    source: str | None = None,
) -> list[Fixture]:
    """Load fixtures from a CSV using the section 3.2 columns.

    Header names are matched case-insensitively against a set of aliases, so a
    file exported from a sportsbook or written by hand both work. Times without
    an explicit zone are read as EAT.
    """
    file = Path(path)
    if not file.is_file():
        raise CollectionError(f"fixtures file not found: {file}")

    fixtures: list[Fixture] = []
    with file.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise CollectionError(f"{file} has no header row")
        index = _header_index(reader.fieldnames)
        missing = [f for f in ("league", "home_team", "away_team") if f not in index]
        if missing:
            raise CollectionError(
                f"{file} is missing required column(s): {', '.join(missing)}. "
                f"Found: {', '.join(reader.fieldnames)}"
            )

        for line_number, row in enumerate(reader, start=2):
            get = _field_reader(row, index)
            home, away = get("home_team"), get("away_team")
            if not home or not away:
                log.warning("skipping CSV row with a missing team", extra={"line": line_number})
                continue

            kickoff = None
            if get("kickoff_utc"):
                kickoff = parse_kickoff(get("kickoff_utc"), assume="UTC")
            if kickoff is None and get("kickoff_eat"):
                kickoff = parse_kickoff(
                    get("kickoff_eat"), assume="EAT", reference_day=reference_day
                )
            if kickoff is None and get("date"):
                combined = f"{get('date')} {get('time')}".strip()
                kickoff = parse_kickoff(combined, assume="EAT", reference_day=reference_day)
            if kickoff is None and get("time"):
                kickoff = parse_kickoff(get("time"), assume="EAT", reference_day=reference_day)
            if kickoff is None:
                log.warning(
                    "skipping CSV row with an unreadable kick-off",
                    extra={"line": line_number, "match": f"{home} vs {away}"},
                )
                continue

            fixtures.append(
                Fixture(
                    league=get("league") or "Unknown league",
                    home_team=home,
                    away_team=away,
                    kickoff_utc=kickoff,
                    odds_over_15=parse_decimal_odds(get("odds_over_15")),
                    odds_under_15=parse_decimal_odds(get("odds_under_15")),
                    odds_over_25=parse_decimal_odds(get("odds_over_25")),
                    odds_under_25=parse_decimal_odds(get("odds_under_25")),
                    odds_home=parse_decimal_odds(get("odds_home")),
                    odds_draw=parse_decimal_odds(get("odds_draw")),
                    odds_away=parse_decimal_odds(get("odds_away")),
                    odds_btts_yes=parse_decimal_odds(get("odds_btts_yes")),
                    odds_btts_no=parse_decimal_odds(get("odds_btts_no")),
                    source=source or f"manual CSV: {file.name}",
                )
            )

    log.info("loaded fixtures from CSV", extra={"file": str(file), "count": len(fixtures)})
    return fixtures


# ==========================================================================
# Structured JSON parsing
# ==========================================================================
def _iter_dicts(node: Any, depth: int = 0) -> Iterator[dict]:
    """Walk a decoded JSON structure yielding every dict it contains."""
    if depth > 12:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item, depth + 1)


def _first_key(mapping: dict, keys: Sequence[str]) -> Any:
    """Fetch the first present key, comparing case- and separator-insensitively."""
    flattened = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in mapping.items()}
    for key in keys:
        candidate = flattened.get(re.sub(r"[^a-z0-9]", "", key))
        if candidate not in (None, ""):
            return candidate
    return None


def _team_name(value: Any) -> str:
    """Team names appear as bare strings or as nested objects."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "shortname", "title", "displayname"):
            found = _first_key(value, (key,))
            if isinstance(found, str) and found.strip():
                return found.strip()
    return ""


def _find_over_under_odds(node: dict) -> tuple[float | None, float | None]:
    """Locate Over/Under 1.5 prices anywhere beneath a fixture object.

    Sportsbook payloads nest markets differently everywhere, so this looks for
    any object mentioning a 1.5 total together with an over/under selection.
    """
    over = under = None
    for candidate in _iter_dicts(node):
        blob = " ".join(
            str(v).lower() for v in candidate.values() if isinstance(v, (str, int, float))
        )
        if "1.5" not in blob:
            continue
        price = _first_key(
            candidate, ("odds", "price", "value", "decimal", "oddsdecimal", "coefficient")
        )
        price = parse_decimal_odds(price)
        if price is None:
            continue
        if re.search(r"\bover\b|^o$|\bo1\.5\b|\bmore\b", blob):
            over = over or price
        elif re.search(r"\bunder\b|^u$|\bu1\.5\b|\bless\b", blob):
            under = under or price
    return over, under


def extract_fixtures_from_json(
    payload: Any,
    *,
    source: str = "GSB (JSON payload)",
    reference_day: date | None = None,
) -> list[Fixture]:
    """Pull fixtures out of a decoded JSON payload.

    Works against embedded page state (``__NEXT_DATA__`` and friends) and
    against XHR responses captured while the page loads. A dict qualifies as a
    fixture when it names both sides and carries a start time.
    """
    fixtures: list[Fixture] = []
    seen: set[str] = set()

    for node in _iter_dicts(payload):
        home = _team_name(_first_key(node, JSON_HOME_KEYS))
        away = _team_name(_first_key(node, JSON_AWAY_KEYS))
        if not home or not away:
            continue
        kickoff = parse_kickoff(
            _first_key(node, JSON_TIME_KEYS), assume="UTC", reference_day=reference_day
        )
        if kickoff is None:
            continue
        league = _team_name(_first_key(node, JSON_LEAGUE_KEYS)) or "Unknown league"
        over, under = _find_over_under_odds(node)
        fixture = Fixture(
            league=league,
            home_team=home,
            away_team=away,
            kickoff_utc=kickoff,
            odds_over_15=over,
            odds_under_15=under,
            source=source,
        )
        if fixture.match_key in seen:
            continue
        seen.add(fixture.match_key)
        fixtures.append(fixture)

    return fixtures


def extract_embedded_json(html: str) -> list[Any]:
    """Return decoded JSON blobs embedded in ``<script>`` tags."""
    blobs: list[Any] = []
    patterns = (
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>",
        r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;?\s*</script>",
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.DOTALL | re.IGNORECASE):
            text = match.group(1).strip()
            if not text or len(text) < 2:
                continue
            try:
                blobs.append(json.loads(text))
            except (json.JSONDecodeError, ValueError):
                continue
    return blobs


# ==========================================================================
# DOM parsing
# ==========================================================================
def parse_fixtures_html(
    html: str,
    config: Config,
    *,
    reference_day: date | None = None,
    source: str = "GSB (DOM)",
) -> list[Fixture]:
    """Parse the rendered fixture list.

    Tries structured JSON first, since it is unambiguous, then falls back to
    scanning the DOM. The CSS selectors are configurable
    (``selector_fixture_row`` and friends) because the live markup could not be
    inspected from this environment - see the module docstring.
    """
    if looks_like_bot_protection(html):
        raise BotProtectionDetected(
            "the response is a bot-protection challenge page, not the fixture list. "
            "Collection stops here: this tool does not attempt to bypass such "
            "controls. Supply fixtures with --fixtures <path.csv> instead."
        )

    if config.json_sniff:
        for blob in extract_embedded_json(html):
            fixtures = extract_fixtures_from_json(
                blob, source=f"{source} embedded JSON", reference_day=reference_day
            )
            if fixtures:
                log.info("parsed fixtures from embedded JSON", extra={"count": len(fixtures)})
                return fixtures

    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise CollectionError(
            "BeautifulSoup is required to parse the fixture DOM; run "
            "`pip install -r requirements.txt`"
        ) from exc

    soup = BeautifulSoup(html, "html.parser")
    rows = _candidate_rows(soup, config)
    fixtures: list[Fixture] = []
    for row in rows:
        fixture = _fixture_from_row(row, config, reference_day=reference_day, source=source)
        if fixture is not None:
            fixtures.append(fixture)
    log.info("parsed fixtures from DOM", extra={"count": len(fixtures), "rows": len(rows)})
    return fixtures


def _candidate_rows(soup: Any, config: Config) -> list[Any]:
    """Fixture-row elements, by configured selector or by heuristic."""
    if config.selector_fixture_row:
        return list(soup.select(config.selector_fixture_row))

    # Heuristic: the smallest elements that contain a clock time and at least
    # two plausible decimal prices are almost always one fixture each.
    candidates: list[Any] = []
    for element in soup.find_all(["tr", "li", "article", "div"]):
        text = element.get_text(" ", strip=True)
        if not text or len(text) > 400:
            continue
        if not _TIME_IN_TEXT.search(text):
            continue
        prices = [p for p in re.findall(r"\b\d+\.\d{1,2}\b", text) if 1.0 < float(p) < 100]
        if len(prices) < 2:
            continue
        if any(candidate in element.parents for candidate in candidates):
            continue
        candidates = [c for c in candidates if element not in c.parents]
        candidates.append(element)
    return candidates


def _select_text(row: Any, selector: str) -> str:
    if not selector:
        return ""
    found = row.select_one(selector)
    return found.get_text(" ", strip=True) if found else ""


def _fixture_from_row(
    row: Any,
    config: Config,
    *,
    reference_day: date | None,
    source: str,
) -> Fixture | None:
    """Build one fixture from a DOM row, returning ``None`` if unreadable."""
    text = row.get_text(" ", strip=True)

    home = _select_text(row, config.selector_home)
    away = _select_text(row, config.selector_away)
    if not (home and away):
        home, away = _split_teams(text)
    if not (home and away):
        return None

    kickoff_text = _select_text(row, config.selector_kickoff) or text
    kickoff = parse_kickoff(kickoff_text, assume="EAT", reference_day=reference_day)
    if kickoff is None:
        clock = _TIME_IN_TEXT.search(kickoff_text)
        if clock is None or reference_day is None:
            return None
        kickoff = parse_kickoff(clock.group(0), assume="EAT", reference_day=reference_day)
    if kickoff is None:
        return None

    league = _select_text(row, config.selector_league) or _infer_league(row)
    over = (
        parse_decimal_odds(_select_text(row, config.selector_over15))
        if config.selector_over15
        else None
    )
    under = (
        parse_decimal_odds(_select_text(row, config.selector_under15))
        if config.selector_under15
        else None
    )
    if over is None:
        over, under_guess = _odds_near_label(text, "1.5")
        under = under if under is not None else under_guess

    return Fixture(
        league=league or "Unknown league",
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        odds_over_15=over,
        odds_under_15=under,
        source=source,
    )


_VS_SPLIT = re.compile(r"\s+(?:vs?\.?|v|-|—|–|@)\s+", flags=re.IGNORECASE)


def _split_teams(text: str) -> tuple[str, str]:
    """Split "Home vs Away" out of a row's text."""
    stripped = _TIME_IN_TEXT.sub(" ", text)
    stripped = re.sub(r"\b\d+\.\d{1,2}\b", " ", stripped)
    stripped = re.sub(r"\s{2,}", " ", stripped).strip(" -|")
    parts = [p.strip(" -|") for p in _VS_SPLIT.split(stripped) if p.strip(" -|")]
    if len(parts) >= 2 and all(len(p) > 1 for p in parts[:2]):
        return parts[0], parts[1]
    return "", ""


def _infer_league(row: Any) -> str:
    """Walk up the DOM looking for a section heading naming the competition."""
    for parent in list(row.parents)[:4]:
        if parent is None or not hasattr(parent, "find"):
            continue
        heading = parent.find(["h1", "h2", "h3", "h4", "caption", "th"])
        if heading is not None:
            label = heading.get_text(" ", strip=True)
            if label and len(label) < 80:
                return label
    return ""


def _odds_near_label(text: str, label: str) -> tuple[float | None, float | None]:
    """Take the two prices that follow a "1.5" total in a row's text."""
    position = text.find(label)
    if position < 0:
        return None, None
    tail = text[position + len(label) :]
    prices = [parse_decimal_odds(p) for p in re.findall(r"\b\d+\.\d{1,2}\b", tail)[:2]]
    prices = [p for p in prices if p is not None]
    if not prices:
        return None, None
    return prices[0], (prices[1] if len(prices) > 1 else None)


# ==========================================================================
# Fixture set hygiene
# ==========================================================================
def dedupe_fixtures(fixtures: Iterable[Fixture]) -> list[Fixture]:
    """Collapse duplicate listings on ``match_key`` (spec 12).

    The later listing wins on odds, since it was collected more recently, but
    a missing price never overwrites one we already have.
    """
    merged: dict[str, Fixture] = {}
    for fixture in fixtures:
        key = fixture.match_key
        existing = merged.get(key)
        if existing is None:
            merged[key] = fixture
            continue
        for field_name in (
            "odds_over_15",
            "odds_under_15",
            "odds_over_25",
            "odds_under_25",
            "odds_home",
            "odds_draw",
            "odds_away",
            "odds_btts_yes",
            "odds_btts_no",
        ):
            new_value = getattr(fixture, field_name)
            if new_value is not None:
                setattr(existing, field_name, new_value)
        if fixture.collected_at > existing.collected_at:
            existing.collected_at = fixture.collected_at
    return list(merged.values())


def filter_upcoming(
    fixtures: Iterable[Fixture], reference: datetime | None = None
) -> list[Fixture]:
    """Keep only fixtures whose kick-off is strictly in the future (spec 1)."""
    moment = to_utc(reference or now_utc())
    return [f for f in fixtures if f.kickoff_utc > moment]


def restrict_to_day(fixtures: Iterable[Fixture], day: date) -> list[Fixture]:
    """Keep fixtures kicking off inside the given EAT match day (spec 1)."""
    start, end = eat_day_bounds(day)
    start_utc, end_utc = to_utc(start), to_utc(end)
    return [f for f in fixtures if start_utc <= f.kickoff_utc <= end_utc]


# ==========================================================================
# Live collection
# ==========================================================================
class GsbScraper:
    """Collects the day's fixtures from the sportsbook with Playwright.

    Opt-in only. See the module docstring for the access policy.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.cache_dir = Path(config.cache_dir) / "gsb"

    # -- caching -------------------------------------------------------
    def _cache_path(self, day: date) -> Path:
        return self.cache_dir / f"upcoming-{day.isoformat()}.html"

    def read_cache(self, day: date) -> str | None:
        """Return cached page HTML if it is present and fresh (spec 3.3)."""
        if not self.config.use_cache:
            return None
        path = self._cache_path(day)
        if not path.is_file():
            return None
        age = now_utc() - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if age > timedelta(minutes=self.config.cache_ttl_minutes):
            log.debug(
                "cache expired", extra={"file": str(path), "age_minutes": age.total_seconds() / 60}
            )
            return None
        log.info("using cached page", extra={"file": str(path)})
        return path.read_text(encoding="utf-8")

    def write_cache(self, day: date, html: str) -> None:
        if not self.config.use_cache:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(day).write_text(html, encoding="utf-8")

    # -- policy --------------------------------------------------------
    def ensure_permitted(self) -> RobotsDecision | None:
        """Refuse unless collection has been opted into and robots permits it."""
        if not self.config.allow_scraping:
            raise CollectionNotPermitted(
                "automated collection from the sportsbook is off by default.\n"
                "  At the time of writing gsb.ug returns HTTP 403 to non-browser\n"
                "  clients, so its robots.txt could not be read and permission could\n"
                "  not be established.\n"
                "  Either supply the fixture list yourself:\n"
                "      python main.py --fixtures fixtures.csv\n"
                "  or, once you have checked that the site's terms permit it, re-run\n"
                "  with --allow-scraping."
            )
        if not self.config.respect_robots:
            log.warning("robots.txt checking is disabled by configuration")
            return None

        decision = check_robots(self.config.gsb_url, self.config.user_agent)
        if decision.explicitly_disallowed:
            raise CollectionNotPermitted(
                f"robots.txt disallows automated collection of {self.config.gsb_url}. "
                "That is not overridable. Use --fixtures <path.csv> instead.\n"
                f"  ({decision.reason})"
            )
        if decision.allowed is None:
            log.warning("crawling policy unknown", extra={"detail": decision.reason})
        else:
            log.info("robots.txt permits collection", extra={"detail": decision.reason})
        return decision

    # -- collection ----------------------------------------------------
    def collect(self, day: date, *, reference: datetime | None = None) -> list[Fixture]:
        """Collect every upcoming fixture for *day*.

        The full day is collected here; the analysis window is applied later as
        a filter, so narrowing the window never erases what a wider run found
        (spec 3.1).
        """
        html = self.read_cache(day)
        if html is None:
            self.ensure_permitted()
            html = self._fetch_with_retries()
            self.write_cache(day, html)

        fixtures = parse_fixtures_html(html, self.config, reference_day=day, source="GSB Uganda")
        fixtures = dedupe_fixtures(fixtures)
        fixtures = filter_upcoming(fixtures, reference)
        fixtures = restrict_to_day(fixtures, day)
        log.info("collected fixtures", extra={"day": day.isoformat(), "count": len(fixtures)})
        return fixtures

    def _fetch_with_retries(self) -> str:
        """Fetch the rendered page, retrying with exponential backoff (spec 3.3)."""
        last_error: Exception | None = None
        for attempt in range(1, max(1, self.config.max_retries) + 1):
            try:
                return self._fetch_once()
            except BotProtectionDetected:
                raise  # retrying a challenge page is pointless and impolite
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                delay = self.config.backoff_base**attempt
                log.warning(
                    "fetch failed; backing off",
                    extra={"attempt": attempt, "delay_seconds": delay, "error": str(exc)},
                )
                time_module.sleep(delay)
        raise CollectionError(
            f"could not retrieve {self.config.gsb_url} after {self.config.max_retries} "
            f"attempts: {last_error}"
        )

    def _fetch_once(self) -> str:
        """Render the page with Playwright and return its HTML."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CollectionError(
                "Playwright is not installed. Run:\n"
                "    pip install -r requirements.txt\n"
                "    playwright install chromium\n"
                "or supply fixtures manually with --fixtures <path.csv>."
            ) from exc

        config = self.config
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=config.headless)
            try:
                context = browser.new_context(
                    user_agent=config.user_agent,
                    locale="en-GB",
                    timezone_id="Africa/Kampala",
                )
                page = context.new_page()
                captured: list[Any] = []

                def on_response(response: Any) -> None:
                    """Keep JSON payloads; they are cleaner than the DOM."""
                    try:
                        content_type = (response.headers or {}).get("content-type", "")
                        if "json" not in content_type.lower():
                            return
                        captured.append(response.json())
                    except Exception:  # noqa: BLE001 - a bad payload is not fatal
                        return

                if config.json_sniff:
                    page.on("response", on_response)

                page.goto(
                    config.gsb_url, wait_until="domcontentloaded", timeout=config.page_timeout_ms
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=config.page_timeout_ms)
                except Exception:  # noqa: BLE001 - a busy page never goes idle
                    log.debug("page did not reach network idle; continuing")

                html = page.content()
                if looks_like_bot_protection(html):
                    raise BotProtectionDetected(
                        "the sportsbook served a bot-protection challenge. This tool does "
                        "not attempt to bypass such controls; use --fixtures <path.csv>."
                    )

                self._expand_all(page)
                html = page.content()

                if captured:
                    html = self._append_captured_json(html, captured)
                return html
            finally:
                browser.close()

    def _expand_all(self, page: Any) -> None:
        """Scroll and click through lazy-loaded and paginated sections (spec 3.1)."""
        expand_labels = (
            "show more",
            "load more",
            "see more",
            "more matches",
            "view all",
            "show all",
            "next page",
        )
        previous_height = -1
        for pass_number in range(self.config.scroll_passes):
            try:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(400)
                clicked = False
                for label in expand_labels:
                    locator = page.get_by_text(re.compile(label, re.IGNORECASE))
                    count = min(locator.count(), 5)
                    for index in range(count):
                        element = locator.nth(index)
                        if element.is_visible():
                            element.click(timeout=2000)
                            page.wait_for_timeout(600)
                            clicked = True
                height = page.evaluate("document.body.scrollHeight")
                if height == previous_height and not clicked:
                    log.debug("page fully expanded", extra={"passes": pass_number + 1})
                    return
                previous_height = height
            except Exception as exc:  # noqa: BLE001 - expansion is best-effort
                log.debug("expansion pass failed", extra={"pass": pass_number, "error": str(exc)})
                return

    @staticmethod
    def _append_captured_json(html: str, captured: list[Any]) -> str:
        """Embed captured XHR payloads so the parser and the cache both see them."""
        try:
            blob = json.dumps(captured)
        except (TypeError, ValueError):
            return html
        return f'{html}\n<script type="application/json" id="captured-xhr">{blob}</script>'


# ==========================================================================
# Entry point used by main
# ==========================================================================
def collect_fixtures(
    config: Config,
    day: date,
    *,
    fixtures_path: str | None = None,
    reference: datetime | None = None,
) -> tuple[list[Fixture], str]:
    """Collect the day's fixtures and report which path was taken.

    Returns ``(fixtures, description)``. The manual CSV wins when supplied,
    which is what makes the pipeline usable when the site is unreachable, its
    markup changes, or automated collection is not permitted (spec 3.4).
    """
    if fixtures_path:
        fixtures = parse_fixtures_csv(fixtures_path, reference_day=day)
        fixtures = dedupe_fixtures(fixtures)
        before = len(fixtures)
        fixtures = filter_upcoming(fixtures, reference)
        skipped = before - len(fixtures)
        if skipped:
            log.info("excluded fixtures that have already started", extra={"count": skipped})
        fixtures = restrict_to_day(fixtures, day)
        return fixtures, f"manual fixture list ({fixtures_path})"

    scraper = GsbScraper(config)
    return scraper.collect(day, reference=reference), f"GSB Uganda ({config.gsb_url})"
