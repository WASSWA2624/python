"""Configuration, defaults, and thresholds.

The values in the block below are the spec's section 9 thresholds, defined at
module level exactly as written there so they are greppable and quotable. The
``Config`` dataclass takes its defaults from them.

Resolution order for the three promptable settings is flag > prompt >
environment > default (spec 2.2). ``Config.sources`` records which of those
each value actually came from, because the workbook has to explain the
thresholds that produced it (spec 2.6).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# Section 9 - qualification thresholds
# --------------------------------------------------------------------------
MIN_PROBABILITY = 70.0  # percent          - promptable
MIN_ODDS = 1.20  # decimal          - promptable
MAX_ODDS = 5.00
MIN_VALUE = 0.00  # model probability minus fair implied
MIN_H2H_MATCHES = 3
MIN_FORM_MATCHES = 5

WINDOW_START = "00:00"  # EAT            - promptable
WINDOW_END = "23:59"  # EAT            - promptable
DEFAULT_NEXT_HOURS = 6  # used by prompt option [2]

# --------------------------------------------------------------------------
# Section 7 - ranking weights (must sum to 1.0)
# --------------------------------------------------------------------------
W_PROBABILITY = 0.60
W_VALUE = 0.40

#: Where a setting came from, in precedence order (spec 2.2).
SOURCE_FLAG = "flag"
SOURCE_PROMPT = "prompt"
SOURCE_ENV = "environment"
SOURCE_DEFAULT = "default"

ENV_PREFIX = "OVER15_"

#: Carried on every report and in the workbook (spec 0, 10.4, 11). The tool
#: estimates; it never predicts a certain outcome and never places a bet.
DISCLAIMER = (
    "These figures are statistical estimates, not predictions of certain outcomes. "
    "This tool performs analysis only: it places no bets and takes no authenticated "
    "action on any sportsbook. Bet responsibly, and only what you can afford to lose."
)

#: The settings that section 2.6 requires be reported with their source.
REPORTED_SETTINGS = ("window", "min_probability", "min_odds")


# --------------------------------------------------------------------------
# .env loading
# --------------------------------------------------------------------------
def load_dotenv_file(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    """Load ``.env`` into ``os.environ`` and return what was read.

    Uses ``python-dotenv`` when installed and a small built-in parser
    otherwise, so a missing optional dependency never stops a run. Existing
    environment variables win unless *override* is set.
    """
    file = Path(path)
    if not file.is_file():
        return {}
    try:  # pragma: no cover - depends on what is installed
        from dotenv import dotenv_values

        values = {k: v for k, v in dotenv_values(file).items() if v is not None}
    except ImportError:  # pragma: no cover
        values = _parse_dotenv(file.read_text(encoding="utf-8"))
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def _parse_dotenv(text: str) -> dict[str, str]:
    """Minimal ``KEY=value`` parser: comments, blank lines, optional quotes."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


# --------------------------------------------------------------------------
# Weight groups
# --------------------------------------------------------------------------
@dataclass
class BlendWeights:
    """Section 5.2 - how the evidence is combined into expected goals.

    A component with no data drops out and its weight is redistributed across
    the components that do have data, so a missing input never silently
    behaves like a zero.
    """

    recent_form: float = 0.30
    home_away_split: float = 0.20
    over15_rate: float = 0.20
    h2h: float = 0.15
    league: float = 0.10
    context: float = 0.05

    def as_dict(self) -> dict[str, float]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def total(self) -> float:
        return sum(self.as_dict().values())


@dataclass
class Config:
    """Every knob the program has. Defaults match the specification."""

    # --- promptable (spec 2.1) ------------------------------------------
    min_probability: float = MIN_PROBABILITY
    min_odds: float = MIN_ODDS
    window_start: str = WINDOW_START
    window_end: str = WINDOW_END
    next_hours: float | None = None
    default_next_hours: float = DEFAULT_NEXT_HOURS

    # --- remaining section 9 thresholds ---------------------------------
    max_odds: float = MAX_ODDS
    min_value: float = MIN_VALUE
    min_h2h_matches: int = MIN_H2H_MATCHES
    min_form_matches: int = MIN_FORM_MATCHES
    #: When False (the default) a thin head-to-head record only drops the
    #: h2h component from the blend; when True it also excludes the fixture.
    require_h2h_sample: bool = False
    #: How far the model may exceed the best observed Over 1.5 rate before
    #: the recent statistics count as materially contradicting it (spec 9).
    contradiction_margin: float = 0.25

    # --- ranking (spec 7) -----------------------------------------------
    w_probability: float = W_PROBABILITY
    w_value: float = W_VALUE

    # --- categories (spec 8) --------------------------------------------
    top_pick_min_probability: float = 80.0
    top_pick_min_value: float = 0.0
    top_pick_min_completeness: float = 0.60
    top_pick_min_confidence: str = "Medium"
    high_odds_min_odds: float = 2.00
    high_odds_min_value: float = 0.05

    # --- model (spec 5) --------------------------------------------------
    blend: BlendWeights = field(default_factory=BlendWeights)
    home_advantage: float = 1.10
    shrinkage_strength: float = 5.0
    dixon_coles_rho: float = 0.0
    use_dixon_coles: bool = False
    lambda_floor: float = 0.20
    lambda_ceiling: float = 6.00
    form_window: int = 10
    h2h_max_age_years: float = 3.0
    h2h_halflife_years: float = 2.0
    league_home_goal_share: float = 0.55

    # --- data quality (spec 12) -----------------------------------------
    fuzzy_threshold: float = 85.0
    alias_file: str = "data/team_aliases.json"

    # --- collection (spec 3) ---------------------------------------------
    gsb_url: str = "https://gsb.ug/sportsbook/upcoming"
    allow_scraping: bool = False
    respect_robots: bool = True
    user_agent: str = (
        "Over15GoalsAnalysisBot/1.0 (statistical research; contact via repository owner)"
    )
    page_timeout_ms: int = 45_000
    max_retries: int = 3
    backoff_base: float = 2.0
    scroll_passes: int = 25
    headless: bool = True
    cache_dir: str = ".cache"
    cache_ttl_minutes: float = 30.0
    use_cache: bool = True
    #: Try structured JSON (embedded state or XHR payloads) before the DOM.
    json_sniff: bool = True
    #: CSS selectors for the fixture list. Left empty the scraper falls
    #: back to heuristics; set them once the live DOM is confirmed.
    selector_fixture_row: str = ""
    selector_league: str = ""
    selector_home: str = ""
    selector_away: str = ""
    selector_kickoff: str = ""
    selector_over15: str = ""
    selector_under15: str = ""

    # --- statistics (spec 4) ---------------------------------------------
    stats_providers: str = "footballdata_couk"
    football_data_api_key: str = ""
    results_csv: str = ""
    seasons_back: int = 2
    stats_timeout_seconds: float = 30.0

    # --- output (spec 10) -------------------------------------------------
    output_dir: str = "results"
    excel_lock_retries: int = 5
    excel_lock_backoff: float = 1.5

    # --- logging (spec 12) ------------------------------------------------
    log_level: str = "INFO"
    log_file: str = "logs/over15.log"

    # --- bookkeeping ------------------------------------------------------
    sources: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def source_of(self, name: str) -> str:
        """Where the effective value of *name* came from."""
        return self.sources.get(name, SOURCE_DEFAULT)

    def set(self, name: str, value: Any, source: str) -> None:
        """Assign a setting and record where it came from."""
        setattr(self, name, value)
        self.sources[name] = source

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build a config from defaults overridden by the environment.

        Accepts both the ``OVER15_`` prefixed name and the bare name from the
        spec (``OUTPUT_DIR``); the prefixed form wins when both are present.
        """
        env = os.environ if env is None else env
        config = cls()

        def lookup(name: str) -> str | None:
            for key in (f"{ENV_PREFIX}{name.upper()}", name.upper()):
                value = env.get(key)
                if value is not None and str(value).strip() != "":
                    return str(value).strip()
            return None

        for spec_field in fields(cls):
            if spec_field.name in {"sources", "blend"}:
                continue
            raw = lookup(spec_field.name)
            if raw is None:
                continue
            try:
                config.set(
                    spec_field.name,
                    _coerce(raw, getattr(config, spec_field.name)),
                    SOURCE_ENV,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"environment value for {spec_field.name!r} is invalid: {raw!r} ({exc})"
                ) from exc

        for weight_field in fields(BlendWeights):
            raw = lookup(f"weight_{weight_field.name}")
            if raw is not None:
                setattr(config.blend, weight_field.name, float(raw))
                config.sources[f"blend.{weight_field.name}"] = SOURCE_ENV

        return config

    # ------------------------------------------------------------------
    def validate(self) -> list[str]:
        """Return a list of configuration problems; empty means valid."""
        problems: list[str] = []

        if not 0 < self.min_probability <= 100:
            problems.append(
                "min_probability must be greater than 0 and at most 100 "
                f"(got {self.min_probability})"
            )
        if self.min_odds < 1.01:
            problems.append(f"min_odds must be at least 1.01 (got {self.min_odds})")
        if self.max_odds <= 1.0:
            problems.append(f"max_odds must exceed 1.0 (got {self.max_odds})")
        if self.min_odds >= self.max_odds:
            problems.append(
                f"min_odds ({self.min_odds}) is not below max_odds ({self.max_odds}); "
                "no odds could ever qualify"
            )
        if abs((self.w_probability + self.w_value) - 1.0) > 1e-9:
            problems.append(
                f"w_probability + w_value must sum to 1.0 "
                f"(got {self.w_probability} + {self.w_value})"
            )
        if abs(self.blend.total() - 1.0) > 1e-6:
            problems.append(f"blend weights must sum to 1.0 (got {self.blend.total():.4f})")
        if any(v < 0 for v in self.blend.as_dict().values()):
            problems.append("blend weights must not be negative")
        if self.min_h2h_matches < 0 or self.min_form_matches < 0:
            problems.append("minimum sample sizes must not be negative")
        if not 0 <= self.dixon_coles_rho < 1:
            problems.append(f"dixon_coles_rho must be in [0, 1) (got {self.dixon_coles_rho})")
        if self.lambda_floor <= 0 or self.lambda_ceiling <= self.lambda_floor:
            problems.append("lambda_floor must be positive and below lambda_ceiling")
        if not 0 <= self.fuzzy_threshold <= 100:
            problems.append("fuzzy_threshold must be between 0 and 100")

        return problems

    def require_valid(self) -> None:
        """Raise ``ValueError`` listing every configuration problem at once."""
        problems = self.validate()
        if problems:
            raise ValueError("invalid configuration:\n  - " + "\n  - ".join(problems))

    # ------------------------------------------------------------------
    def effective_summary(self) -> list[tuple[str, str, str]]:
        """``(setting, value, source)`` triples for the Summary sheet."""
        window = f"{self.window_start}-{self.window_end} EAT"
        weights = f"probability {self.w_probability:.2f} / value {self.w_value:.2f}"
        rows = [
            ("Analysis window", window, "window"),
            ("Minimum probability", f"{self.min_probability:.1f}%", "min_probability"),
            ("Minimum odds", f"{self.min_odds:.2f}", "min_odds"),
            ("Maximum odds", f"{self.max_odds:.2f}", "max_odds"),
            ("Minimum value", f"{self.min_value:+.3f}", "min_value"),
            ("Minimum H2H matches", str(self.min_h2h_matches), "min_h2h_matches"),
            ("Minimum form matches", str(self.min_form_matches), "min_form_matches"),
            ("Score weights", weights, "w_probability"),
            ("Output directory", self.output_dir, "output_dir"),
            ("Statistics providers", self.stats_providers or "(none)", "stats_providers"),
            ("Dixon-Coles correction", "on" if self.use_dixon_coles else "off", "use_dixon_coles"),
        ]
        return [(label, value, self.source_of(name)) for label, value, name in rows]

    def one_line(self) -> str:
        """Compact restatement of the promptable settings, for the log."""
        return (
            f"window={self.window_start}-{self.window_end} EAT "
            f"({self.source_of('window')}) | "
            f"min_probability={self.min_probability:.1f}% ({self.source_of('min_probability')}) | "
            f"min_odds={self.min_odds:.2f} ({self.source_of('min_odds')})"
        )


def _coerce(raw: str, current: Any) -> Any:
    """Convert an environment string to the type of the current default."""
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError("expected a boolean")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    if current is None:
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw
