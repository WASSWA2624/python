"""External statistics with caching and provenance (spec 4).

Every figure the model consumes is derived from real recorded results. There is
no synthetic data anywhere in this module: when a source cannot supply a
statistic it is left as ``None``, which lowers ``data_completeness`` and
``confidence`` and can exclude the fixture outright (spec 12). Nothing is
invented to fill a gap.

Sources
-------
``footballdata_couk``
    football-data.co.uk publishes free season CSVs of full-time results for
    the major European divisions. No key, no authentication.
``local_csv``
    A results CSV supplied with ``--results``: ``date,league,home,away,
    home_goals,away_goals``. Useful for leagues the free feeds do not cover,
    and for offline runs.
``football_data_org``
    api.football-data.org v4, used only when ``FOOTBALL_DATA_API_KEY`` is set.

All of them feed one ``ResultsRepository``; the section 4 aggregates are then
computed from that single store, so the same maths applies whatever the origin.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from config import Config
from models import (
    ContextInfo,
    Fixture,
    FormStats,
    H2HStats,
    LeagueStats,
    MatchStatistics,
    SourceRecord,
    now_utc,
)
from utils.logging import get_logger
from utils.naming import best_match, normalise_league, normalise_team

log = get_logger("stats")

FOOTBALL_DATA_COUK_BASE = "https://www.football-data.co.uk/mmz4281"

#: Competition name fragments mapped to football-data.co.uk division codes.
FOOTBALL_DATA_COUK_DIVISIONS: dict[str, tuple[str, ...]] = {
    "E0": ("english premier league", "premier league", "england premier", "epl"),
    "E1": ("championship", "england championship"),
    "E2": ("league one", "england league one"),
    "E3": ("league two", "england league two"),
    "SC0": ("scottish premiership", "scotland premiership", "scottish premier"),
    "D1": ("bundesliga", "germany bundesliga"),
    "D2": ("2 bundesliga", "bundesliga 2", "zweite bundesliga"),
    "I1": ("serie a", "italy serie a"),
    "I2": ("serie b", "italy serie b"),
    "SP1": ("la liga", "laliga", "spain primera", "primera division"),
    "SP2": ("segunda division", "la liga 2", "spain segunda"),
    "F1": ("ligue 1", "france ligue 1"),
    "F2": ("ligue 2", "france ligue 2"),
    "N1": ("eredivisie", "netherlands eredivisie"),
    "B1": ("jupiler", "belgian pro league", "belgium first division"),
    "P1": ("primeira liga", "portugal primeira", "liga portugal"),
    "T1": ("super lig", "turkey super lig", "turkiye super lig"),
    "G1": ("super league greece", "greece super league"),
}


# ==========================================================================
# Result store
# ==========================================================================
@dataclass(frozen=True)
class MatchResult:
    """One completed match: the atom every section 4 statistic is built from."""

    played_on: date
    league: str
    home_team: str
    away_team: str
    home_goals: int
    away_goals: int
    source: str = ""

    @property
    def total_goals(self) -> int:
        return self.home_goals + self.away_goals

    @property
    def over_15(self) -> bool:
        return self.total_goals >= 2


class ResultsRepository:
    """Completed results, indexed for the lookups section 4 needs."""

    def __init__(self) -> None:
        self._results: list[MatchResult] = []
        self._by_team: dict[str, list[MatchResult]] = {}
        self._by_league: dict[str, list[MatchResult]] = {}
        self._team_names: set[str] = set()

    def __len__(self) -> int:
        return len(self._results)

    @property
    def team_names(self) -> list[str]:
        return sorted(self._team_names)

    def add(self, result: MatchResult) -> None:
        self._results.append(result)
        for team in (result.home_team, result.away_team):
            key = normalise_team(team)
            self._by_team.setdefault(key, []).append(result)
            self._team_names.add(team)
        self._by_league.setdefault(normalise_league(result.league), []).append(result)

    def extend(self, results: Iterable[MatchResult]) -> None:
        for result in results:
            self.add(result)

    def resolve_team(self, name: str, threshold: float) -> tuple[str | None, float]:
        """Link a sportsbook team name to a name known to the repository.

        Exact match on the normalised form first, then fuzzy matching. Below
        *threshold* the link is refused and the caller flags it for review
        rather than assuming it (spec 12).
        """
        key = normalise_team(name)
        if key in self._by_team:
            return key, 100.0
        candidate, score = best_match(name, self.team_names, threshold)
        if candidate is None:
            return None, score
        return normalise_team(candidate), score

    def matches_for(
        self,
        team_key: str,
        *,
        before: date,
        limit: int | None = None,
        venue: str | None = None,
    ) -> list[MatchResult]:
        """A team's most recent completed matches before *before*.

        *venue* of ``"home"`` or ``"away"`` restricts to that side, which is
        what the section 4.3 splits need.
        """
        results = self._by_team.get(team_key, [])
        selected = []
        for result in results:
            if result.played_on >= before:
                continue
            if venue == "home" and normalise_team(result.home_team) != team_key:
                continue
            if venue == "away" and normalise_team(result.away_team) != team_key:
                continue
            selected.append(result)
        selected.sort(key=lambda r: r.played_on, reverse=True)
        return selected[:limit] if limit else selected

    def head_to_head(self, home_key: str, away_key: str, *, before: date) -> list[MatchResult]:
        """Previous meetings between two teams, most recent first."""
        meetings = [
            result
            for result in self._by_team.get(home_key, [])
            if result.played_on < before
            and {normalise_team(result.home_team), normalise_team(result.away_team)}
            == {home_key, away_key}
        ]
        meetings.sort(key=lambda r: r.played_on, reverse=True)
        return meetings

    def league_matches(
        self, league: str, *, before: date, limit: int | None = None
    ) -> list[MatchResult]:
        results = [
            r for r in self._by_league.get(normalise_league(league), []) if r.played_on < before
        ]
        results.sort(key=lambda r: r.played_on, reverse=True)
        return results[:limit] if limit else results


# ==========================================================================
# Aggregation - the section 4 statistics
# ==========================================================================
def summarise_form(
    results: Sequence[MatchResult],
    team_key: str,
    source: str,
) -> FormStats:
    """Section 4.2/4.3 aggregates for one team over *results*."""
    if not results:
        return FormStats(source=source)
    scored = conceded = totals = 0
    over15 = two_plus = 0
    for result in results:
        is_home = normalise_team(result.home_team) == team_key
        team_goals = result.home_goals if is_home else result.away_goals
        opponent_goals = result.away_goals if is_home else result.home_goals
        scored += team_goals
        conceded += opponent_goals
        totals += result.total_goals
        if result.over_15:
            over15 += 1
            two_plus += 1
    count = len(results)
    return FormStats(
        matches=count,
        over15_rate=over15 / count,
        avg_scored=scored / count,
        avg_conceded=conceded / count,
        avg_total=totals / count,
        count_2plus=two_plus,
        source=source,
    )


def summarise_h2h(
    meetings: Sequence[MatchResult],
    home_key: str,
    reference_day: date,
    config: Config,
    fixture_league: str,
    source: str,
) -> H2HStats:
    """Section 4.1 aggregates, age- and context-discounted.

    Meetings older than ``h2h_max_age_years`` are dropped outright. The rest
    are weighted by an exponential half-life, and a meeting played in a
    different competition is halved again - materially different circumstances
    should not carry full weight (spec 4.1).
    """
    if not meetings:
        return H2HStats(source=source)

    fixture_league_key = normalise_league(fixture_league)
    kept: list[tuple[MatchResult, float]] = []
    for meeting in meetings:
        age_years = (reference_day - meeting.played_on).days / 365.25
        if age_years > config.h2h_max_age_years:
            continue
        weight = 0.5 ** (age_years / max(config.h2h_halflife_years, 0.1))
        if fixture_league_key and normalise_league(meeting.league) != fixture_league_key:
            weight *= 0.5
        kept.append((meeting, weight))

    if not kept:
        return H2HStats(source=source)

    total_weight = sum(w for _, w in kept)
    weighted_goals = sum(m.total_goals * w for m, w in kept) / total_weight
    weighted_over15 = sum((1.0 if m.over_15 else 0.0) * w for m, w in kept) / total_weight
    home_goals = sum(
        (m.home_goals if normalise_team(m.home_team) == home_key else m.away_goals) * w
        for m, w in kept
    )
    all_goals = sum(m.total_goals * w for m, w in kept)

    return H2HStats(
        meetings=len(kept),
        over15_rate=weighted_over15,
        avg_total_goals=weighted_goals,
        count_2plus=sum(1 for m, _ in kept if m.over_15),
        recent_results=[
            f"{m.played_on:%Y-%m-%d} {m.home_team} {m.home_goals}-{m.away_goals} {m.away_team}"
            for m, _ in kept[:5]
        ],
        home_goal_share=(home_goals / all_goals) if all_goals > 0 else None,
        source=source,
    )


def summarise_league(
    results: Sequence[MatchResult],
    league_name: str,
    source: str,
) -> LeagueStats:
    """Section 4.4 baselines plus a recent scoring trend."""
    if not results:
        return LeagueStats(name=league_name, source=source)
    count = len(results)
    total_goals = sum(r.total_goals for r in results)
    over15 = sum(1 for r in results if r.over_15)
    home_goals = sum(r.home_goals for r in results)

    trend = ""
    recent_window = max(20, count // 5)
    if count >= 2 * recent_window:
        ordered = sorted(results, key=lambda r: r.played_on, reverse=True)
        recent = ordered[:recent_window]
        earlier = ordered[recent_window:]
        recent_avg = sum(r.total_goals for r in recent) / len(recent)
        earlier_avg = sum(r.total_goals for r in earlier) / len(earlier)
        delta = recent_avg - earlier_avg
        direction = "up" if delta > 0.15 else "down" if delta < -0.15 else "flat"
        trend = f"{direction} ({recent_avg:.2f} in last {len(recent)} vs {earlier_avg:.2f} before)"

    return LeagueStats(
        name=league_name,
        matches=count,
        avg_goals=total_goals / count,
        over15_rate=over15 / count,
        home_goal_share=(home_goals / total_goals) if total_goals else None,
        trend=trend,
        source=source,
    )


# ==========================================================================
# Loaders
# ==========================================================================
class ResultsLoader:
    """Base class: fills a repository from one source."""

    name = "base"

    def load(
        self, repository: ResultsRepository, fixtures: Sequence[Fixture]
    ) -> list[SourceRecord]:
        raise NotImplementedError


def _http_get(url: str, timeout: float, user_agent: str) -> bytes:
    """Fetch a URL, preferring ``requests`` and falling back to urllib."""
    try:
        import requests

        response = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        response.raise_for_status()
        return response.content
    except ImportError:  # pragma: no cover - requests is declared
        import urllib.request

        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=timeout) as handle:
            return handle.read()


class CachedDownloader:
    """Disk-cached HTTP fetches, so re-runs cost nothing (spec 3.3)."""

    def __init__(self, config: Config, subdirectory: str) -> None:
        self.config = config
        self.directory = Path(config.cache_dir) / subdirectory
        self.ttl = timedelta(hours=12)

    def get(self, url: str, filename: str) -> bytes | None:
        path = self.directory / filename
        if self.config.use_cache and path.is_file():
            age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            if age < self.ttl:
                return path.read_bytes()
        try:
            payload = _http_get(url, self.config.stats_timeout_seconds, self.config.user_agent)
        except Exception as exc:  # noqa: BLE001 - any failure is non-fatal
            log.warning("statistics fetch failed", extra={"url": url, "error": str(exc)})
            if path.is_file():
                log.info("falling back to a stale cache entry", extra={"file": str(path)})
                return path.read_bytes()
            return None
        if self.config.use_cache:
            self.directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        return payload


def season_codes(reference: date, seasons_back: int) -> list[str]:
    """football-data.co.uk season codes, newest first: ``2526`` and so on."""
    start_year = reference.year if reference.month >= 7 else reference.year - 1
    codes = []
    for offset in range(max(1, seasons_back)):
        year = start_year - offset
        codes.append(f"{year % 100:02d}{(year + 1) % 100:02d}")
    return codes


#: The country each division belongs to, used to stop a name fragment from
#: matching across borders - "Uganda Premier League" must never resolve to the
#: English "Premier League" and be analysed on English results.
DIVISION_COUNTRY: dict[str, str] = {
    "E0": "england",
    "E1": "england",
    "E2": "england",
    "E3": "england",
    "SC0": "scotland",
    "D1": "germany",
    "D2": "germany",
    "I1": "italy",
    "I2": "italy",
    "SP1": "spain",
    "SP2": "spain",
    "F1": "france",
    "F2": "france",
    "N1": "netherlands",
    "B1": "belgium",
    "P1": "portugal",
    "T1": "turkey",
    "G1": "greece",
}

#: Country words that may appear in a competition name, mapped to a country.
COUNTRY_WORDS: dict[str, str] = {
    "england": "england",
    "english": "england",
    "britain": "england",
    "scotland": "scotland",
    "scottish": "scotland",
    "germany": "germany",
    "german": "germany",
    "deutschland": "germany",
    "italy": "italy",
    "italian": "italy",
    "italia": "italy",
    "spain": "spain",
    "spanish": "spain",
    "espana": "spain",
    "france": "france",
    "french": "france",
    "netherlands": "netherlands",
    "dutch": "netherlands",
    "holland": "netherlands",
    "belgium": "belgium",
    "belgian": "belgium",
    "portugal": "portugal",
    "portuguese": "portugal",
    "turkey": "turkey",
    "turkish": "turkey",
    "turkiye": "turkey",
    "greece": "greece",
    "greek": "greece",
    # Countries with no free division feed here: naming one must block a match.
    "uganda": "uganda",
    "ugandan": "uganda",
    "kenya": "kenya",
    "tanzania": "tanzania",
    "rwanda": "rwanda",
    "nigeria": "nigeria",
    "ghana": "ghana",
    "egypt": "egypt",
    "south africa": "south africa",
    "morocco": "morocco",
    "zambia": "zambia",
    "brazil": "brazil",
    "brasil": "brazil",
    "argentina": "argentina",
    "chile": "chile",
    "colombia": "colombia",
    "mexico": "mexico",
    "usa": "usa",
    "mls": "usa",
    "japan": "japan",
    "china": "china",
    "korea": "korea",
    "india": "india",
    "australia": "australia",
    "sweden": "sweden",
    "norway": "norway",
    "denmark": "denmark",
    "finland": "finland",
    "poland": "poland",
    "russia": "russia",
    "ukraine": "ukraine",
    "austria": "austria",
    "switzerland": "switzerland",
    "swiss": "switzerland",
    "czech": "czech",
    "croatia": "croatia",
    "serbia": "serbia",
    "romania": "romania",
    "bulgaria": "bulgaria",
    "ireland": "ireland",
    "wales": "wales",
    "israel": "israel",
    "saudi": "saudi arabia",
    "qatar": "qatar",
}


def country_in_league_name(league: str) -> str | None:
    """The country named in a competition title, if any."""
    key = f" {normalise_league(league)} "
    for word, country in COUNTRY_WORDS.items():
        if f" {word} " in key:
            return country
    return None


def division_for_league(league: str) -> str | None:
    """Map a competition name to a football-data.co.uk division code.

    Matching is longest-fragment-first and guarded by country: a name that
    explicitly names a country only maps to that country's divisions. An
    unqualified "Premier League" still resolves to England, which is the
    conventional reading.
    """
    key = normalise_league(league)
    if not key:
        return None
    named_country = country_in_league_name(key)

    # Fragments are normalised the same way the league name is, so that
    # punctuation in either ("2. Bundesliga") cannot cause a miss.
    ordered = sorted(
        (
            (code, normalise_league(fragment))
            for code, fragments in FOOTBALL_DATA_COUK_DIVISIONS.items()
            for fragment in fragments
        ),
        key=lambda pair: len(pair[1]),
        reverse=True,
    )
    for code, fragment in ordered:
        if fragment not in key:
            continue
        if named_country and DIVISION_COUNTRY.get(code) != named_country:
            continue
        return code
    return None


class FootballDataCoUkLoader(ResultsLoader):
    """Free season CSVs of full-time results for the major divisions."""

    name = "football-data.co.uk"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.downloader = CachedDownloader(config, "footballdata")

    def load(
        self, repository: ResultsRepository, fixtures: Sequence[Fixture]
    ) -> list[SourceRecord]:
        sources: list[SourceRecord] = []
        wanted: dict[str, str] = {}
        for fixture in fixtures:
            code = division_for_league(fixture.league)
            if code:
                wanted[code] = fixture.league

        if not wanted:
            log.info("no fixture league maps to a football-data.co.uk division")
            return sources

        reference = min((f.match_day for f in fixtures), default=date.today())
        for code, league_name in sorted(wanted.items()):
            for season in season_codes(reference, self.config.seasons_back):
                url = f"{FOOTBALL_DATA_COUK_BASE}/{season}/{code}.csv"
                payload = self.downloader.get(url, f"{season}-{code}.csv")
                if payload is None:
                    sources.append(
                        SourceRecord(
                            statistic=f"results: {league_name}",
                            source=self.name,
                            endpoint=url,
                            retrieved_at=now_utc(),
                            notes="unavailable - fetch failed",
                        )
                    )
                    continue
                added = self._parse(payload, repository, league_name)
                sources.append(
                    SourceRecord(
                        statistic=f"results: {league_name} {season}",
                        source=self.name,
                        endpoint=url,
                        retrieved_at=now_utc(),
                        notes=f"{added} completed matches",
                    )
                )
        return sources

    def _parse(self, payload: bytes, repository: ResultsRepository, league_name: str) -> int:
        """Load one season file into the repository, returning the row count.

        These files run to several hundred rows across twenty-odd columns, and
        a full run may pull dozens of them, so pandas does the parsing when it
        is installed. The ``csv`` fallback below produces identical records, so
        nothing about the analysis depends on which path ran.
        """
        text = payload.decode("utf-8-sig", errors="replace")
        rows = self._rows_via_pandas(text)
        if rows is None:
            rows = csv.DictReader(io.StringIO(text))
        added = 0
        for row in rows:
            try:
                played = _parse_uk_date(row.get("Date", ""))
                home = (row.get("HomeTeam") or "").strip()
                away = (row.get("AwayTeam") or "").strip()
                home_goals = row.get("FTHG")
                away_goals = row.get("FTAG")
                if (
                    not (played and home and away)
                    or home_goals in (None, "")
                    or away_goals in (None, "")
                ):
                    continue
                repository.add(
                    MatchResult(
                        played_on=played,
                        league=league_name,
                        home_team=home,
                        away_team=away,
                        home_goals=int(float(home_goals)),
                        away_goals=int(float(away_goals)),
                        source=self.name,
                    )
                )
                added += 1
            except (ValueError, TypeError):
                continue
        return added

    @staticmethod
    def _rows_via_pandas(text: str) -> list[dict[str, str]] | None:
        """Parse with pandas, or return ``None`` when it is unavailable.

        Only the six columns the repository needs are read. Everything is kept
        as text so the loop below applies exactly the same coercion and the
        same rejections whichever parser produced the rows.
        """
        try:
            import pandas as pd
        except ImportError:
            return None
        wanted = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]
        try:
            frame = pd.read_csv(
                io.StringIO(text),
                usecols=lambda name: name in wanted,
                dtype=str,
                on_bad_lines="skip",
            )
        except Exception as exc:  # noqa: BLE001 - fall back rather than fail
            log.debug("pandas could not read a season file", extra={"error": str(exc)})
            return None
        if not set(wanted).issubset(frame.columns):
            return None
        frame = frame.dropna(subset=wanted)
        return frame[wanted].to_dict("records")


def _parse_uk_date(raw: str) -> date | None:
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


class LocalResultsLoader(ResultsLoader):
    """Results from a local CSV: ``date,league,home,away,home_goals,away_goals``."""

    name = "local results CSV"

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(
        self, repository: ResultsRepository, fixtures: Sequence[Fixture]
    ) -> list[SourceRecord]:
        if not self.path.is_file():
            log.warning("results CSV not found", extra={"path": str(self.path)})
            return [
                SourceRecord(
                    statistic="results",
                    source=self.name,
                    endpoint=str(self.path),
                    retrieved_at=now_utc(),
                    notes="unavailable - file not found",
                )
            ]

        added = 0
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                played = _parse_any_date(lowered.get("date", ""))
                home = lowered.get("home") or lowered.get("home_team", "")
                away = lowered.get("away") or lowered.get("away_team", "")
                home_goals = lowered.get("home_goals") or lowered.get("fthg", "")
                away_goals = lowered.get("away_goals") or lowered.get("ftag", "")
                if not (played and home and away and home_goals and away_goals):
                    continue
                try:
                    repository.add(
                        MatchResult(
                            played_on=played,
                            league=lowered.get("league", "Unknown league"),
                            home_team=home,
                            away_team=away,
                            home_goals=int(float(home_goals)),
                            away_goals=int(float(away_goals)),
                            source=self.name,
                        )
                    )
                    added += 1
                except (ValueError, TypeError):
                    continue

        return [
            SourceRecord(
                statistic="results",
                source=self.name,
                endpoint=str(self.path),
                retrieved_at=now_utc(),
                notes=f"{added} completed matches",
            )
        ]


def _parse_any_date(raw: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


class FootballDataOrgLoader(ResultsLoader):
    """api.football-data.org v4. Used only when an API key is configured."""

    name = "api.football-data.org"
    BASE = "https://api.football-data.org/v4"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.downloader = CachedDownloader(config, "footballdataorg")

    def load(
        self, repository: ResultsRepository, fixtures: Sequence[Fixture]
    ) -> list[SourceRecord]:
        if not self.config.football_data_api_key:
            return []
        try:
            import requests
        except ImportError:  # pragma: no cover
            log.warning("requests is required for api.football-data.org")
            return []

        reference = min((f.match_day for f in fixtures), default=date.today())
        date_from = (reference - timedelta(days=365)).isoformat()
        url = (
            f"{self.BASE}/matches?dateFrom={date_from}"
            f"&dateTo={reference.isoformat()}&status=FINISHED"
        )
        try:
            response = requests.get(
                url,
                timeout=self.config.stats_timeout_seconds,
                headers={
                    "X-Auth-Token": self.config.football_data_api_key,
                    "User-Agent": self.config.user_agent,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("football-data.org fetch failed", extra={"error": str(exc)})
            return [
                SourceRecord(
                    statistic="results",
                    source=self.name,
                    endpoint=url,
                    retrieved_at=now_utc(),
                    notes=f"unavailable - {exc}",
                )
            ]

        added = 0
        for match in payload.get("matches", []):
            try:
                score = match["score"]["fullTime"]
                if score.get("home") is None or score.get("away") is None:
                    continue
                repository.add(
                    MatchResult(
                        played_on=datetime.fromisoformat(
                            match["utcDate"].replace("Z", "+00:00")
                        ).date(),
                        league=match.get("competition", {}).get("name", "Unknown league"),
                        home_team=match["homeTeam"]["name"],
                        away_team=match["awayTeam"]["name"],
                        home_goals=int(score["home"]),
                        away_goals=int(score["away"]),
                        source=self.name,
                    )
                )
                added += 1
            except (KeyError, TypeError, ValueError):
                continue

        return [
            SourceRecord(
                statistic="results",
                source=self.name,
                endpoint=url,
                retrieved_at=now_utc(),
                notes=f"{added} completed matches",
            )
        ]


# ==========================================================================
# Aggregator
# ==========================================================================
class StatisticsProvider:
    """Builds the section 4 statistics for each fixture, with provenance."""

    def __init__(self, config: Config, repository: ResultsRepository | None = None) -> None:
        self.config = config
        self.repository = repository if repository is not None else ResultsRepository()
        #: Loader-level provenance: one row per feed that was fetched.
        self.sources: list[SourceRecord] = []
        #: Per-fixture, per-statistic provenance for the ``Data Sources``
        #: sheet (spec 10.4), including the notes that say what was missing.
        self.fixture_sources: list[SourceRecord] = []
        self._loaded = False

    # -- loading -------------------------------------------------------
    def build_loaders(self) -> list[ResultsLoader]:
        loaders: list[ResultsLoader] = []
        requested = [n.strip().lower() for n in self.config.stats_providers.split(",") if n.strip()]
        for name in requested:
            if name in {"footballdata_couk", "football-data.co.uk", "couk"}:
                loaders.append(FootballDataCoUkLoader(self.config))
            elif name in {"football_data_org", "football-data.org", "org"}:
                loaders.append(FootballDataOrgLoader(self.config))
            elif name in {"none", "off"}:
                continue
            else:
                log.warning("unknown statistics provider", extra={"provider": name})
        if self.config.results_csv:
            loaders.append(LocalResultsLoader(self.config.results_csv))
        return loaders

    def load(self, fixtures: Sequence[Fixture]) -> None:
        """Populate the repository from every configured source."""
        if self._loaded:
            return
        for loader in self.build_loaders():
            try:
                self.sources.extend(loader.load(self.repository, fixtures))
            except Exception as exc:  # noqa: BLE001 - one bad source is survivable
                log.error(
                    "statistics loader failed",
                    extra={"loader": loader.name, "error": str(exc)},
                )
                self.sources.append(
                    SourceRecord(
                        statistic="results",
                        source=loader.name,
                        retrieved_at=now_utc(),
                        notes=f"loader failed: {exc}",
                    )
                )
        self._loaded = True
        log.info(
            "statistics loaded",
            extra={"results": len(self.repository), "teams": len(self.repository.team_names)},
        )

    # -- per-fixture ---------------------------------------------------
    def statistics_for(self, fixture: Fixture) -> MatchStatistics:
        """All of section 4 for one fixture. Missing figures stay missing."""
        stats = MatchStatistics()
        reference_day = fixture.match_day

        home_key, home_score = self.repository.resolve_team(
            fixture.home_team, self.config.fuzzy_threshold
        )
        away_key, away_score = self.repository.resolve_team(
            fixture.away_team, self.config.fuzzy_threshold
        )

        if home_key is None:
            stats.review_flags.append(
                f"home team {fixture.home_team!r} not linked to any known team "
                f"(best similarity {home_score:.0f} < {self.config.fuzzy_threshold:.0f})"
            )
        if away_key is None:
            stats.review_flags.append(
                f"away team {fixture.away_team!r} not linked to any known team "
                f"(best similarity {away_score:.0f} < {self.config.fuzzy_threshold:.0f})"
            )
        for label, score in (("home", home_score), ("away", away_score)):
            if 0 < score < 100 and score >= self.config.fuzzy_threshold:
                stats.review_flags.append(f"{label} team linked fuzzily at {score:.0f}")

        window = self.config.form_window
        source_note = ", ".join(sorted({s.source for s in self.sources})) or "no source"
        endpoint = ", ".join(sorted({s.endpoint for s in self.sources if s.endpoint}))

        if home_key:
            stats.home_form = summarise_form(
                self.repository.matches_for(home_key, before=reference_day, limit=window),
                home_key,
                source_note,
            )
            stats.home_home_split = summarise_form(
                self.repository.matches_for(
                    home_key, before=reference_day, limit=window, venue="home"
                ),
                home_key,
                source_note,
            )
        if away_key:
            stats.away_form = summarise_form(
                self.repository.matches_for(away_key, before=reference_day, limit=window),
                away_key,
                source_note,
            )
            stats.away_away_split = summarise_form(
                self.repository.matches_for(
                    away_key, before=reference_day, limit=window, venue="away"
                ),
                away_key,
                source_note,
            )
        if home_key and away_key:
            stats.h2h = summarise_h2h(
                self.repository.head_to_head(home_key, away_key, before=reference_day),
                home_key,
                reference_day,
                self.config,
                fixture.league,
                source_note,
            )

        stats.league = summarise_league(
            self.repository.league_matches(fixture.league, before=reference_day),
            fixture.league,
            source_note,
        )

        # Supporting context (spec 4.5) stays strictly neutral unless a
        # reliable source supplies it. Speculative news must not move the
        # estimate, so the default multipliers are exactly 1.0.
        stats.context = ContextInfo(notes="", source="")

        stats.sources = [
            SourceRecord(
                statistic=f"{fixture.label}: {name}",
                source=source_note,
                endpoint=endpoint,
                retrieved_at=now_utc(),
                notes=note,
            )
            for name, note in (
                (
                    "head-to-head",
                    f"{stats.h2h.meetings} meetings used" if stats.h2h.available else "unavailable",
                ),
                (
                    "home form",
                    f"{stats.home_form.matches} matches"
                    if stats.home_form.available
                    else "unavailable",
                ),
                (
                    "away form",
                    f"{stats.away_form.matches} matches"
                    if stats.away_form.available
                    else "unavailable",
                ),
                (
                    "home split",
                    f"{stats.home_home_split.matches} home matches"
                    if stats.home_home_split.available
                    else "unavailable",
                ),
                (
                    "away split",
                    f"{stats.away_away_split.matches} away matches"
                    if stats.away_away_split.available
                    else "unavailable",
                ),
                (
                    "league baseline",
                    f"{stats.league.matches} matches" if stats.league.available else "unavailable",
                ),
                ("supporting context", "no reliable source configured"),
            )
        ]
        self.fixture_sources.extend(stats.sources)
        return stats
