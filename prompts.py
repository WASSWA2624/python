"""Interactive setup and precedence resolution (spec 2).

Three settings are promptable: the analysis window, the minimum probability,
and the minimum odds. Each resolves from the first source that supplies it::

    1. an explicit command-line flag   - highest priority
    2. the interactive answer
    3. an environment variable or .env entry
    4. the built-in default in config.py

A setting supplied by flag is **not prompted for**, so running with all three
flags is fully non-interactive without needing ``--no-prompt``.

The program never blocks for input in an unattended run (spec 2.5): prompting
is skipped when ``--no-prompt`` is passed, when stdin is not a TTY, or when
``CI`` is set. Every prompt function takes its input and output callables as
arguments, which is what lets the tests drive them without a terminal.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from config import (
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_FLAG,
    SOURCE_PROMPT,
    Config,
)
from models import AnalysisWindow, eat_day_bounds, now_utc, to_eat

#: Attempts allowed per setting before falling back to its default (spec 2.4).
MAX_ATTEMPTS = 3

#: A minimum probability above this leaves almost nothing qualifying (spec 2.4).
PROBABILITY_WARN_ABOVE = 95.0

TITLE = "Over 1.5 Goals — Match Analysis Tool"


class PromptAborted(Exception):
    """The user pressed Ctrl+C, or declined at the confirmation prompt.

    Handled in ``main`` as a clean exit with code 0, writing nothing (spec 2.4).
    """


class ValidationError(ValueError):
    """An answer was rejected; the message explains why, and we re-prompt."""


# --------------------------------------------------------------------------
# Terminal plumbing
# --------------------------------------------------------------------------
def safe_write(text: str = "") -> None:
    """Print, tolerating consoles that cannot encode the character set.

    Windows terminals still default to a legacy code page in places, and the
    spec's prompt text contains an em dash. Falling back to a replaced
    encoding is better than dying on a decoration.
    """
    try:
        print(text)
    except UnicodeEncodeError:  # pragma: no cover - platform dependent
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


@dataclass
class PromptIO:
    """Input and output for the prompt flow, injectable for testing."""

    ask: Callable[[str], str] = input
    say: Callable[[str], None] = safe_write

    def read(self, prompt: str) -> str:
        """Read one answer. Ctrl+C aborts; EOF falls back to the default."""
        try:
            return self.ask(prompt)
        except KeyboardInterrupt as exc:
            raise PromptAborted("interrupted at the prompt") from exc
        except EOFError:
            # stdin vanished mid-prompt; treat as "accept the default".
            return ""


def is_interactive(
    no_prompt: bool,
    env: Mapping[str, str] | None = None,
    stdin: Any = None,
) -> bool:
    """Should the program prompt at all? (spec 2.5)"""
    if no_prompt:
        return False
    env = os.environ if env is None else env
    if env.get("CI"):
        return False
    stream = sys.stdin if stdin is None else stdin
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError):
        return False


# --------------------------------------------------------------------------
# Parsing and validation (spec 2.4)
# --------------------------------------------------------------------------
def parse_hhmm(raw: str) -> time:
    """Parse a 24-hour ``HH:MM`` clock time."""
    text = raw.strip()
    if not text:
        raise ValidationError("a time is required, in 24-hour HH:MM form")
    for fmt in ("%H:%M", "%H%M", "%H"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValidationError(f"{raw!r} is not a 24-hour HH:MM time (for example 14:30)")


def parse_window_text(raw: str) -> tuple[time, time]:
    """Parse ``HH:MM-HH:MM`` into a start and end time."""
    text = raw.strip().replace("–", "-").replace("—", "-").replace(" to ", "-")
    if "-" not in text:
        raise ValidationError("a window looks like 14:00-22:00")
    start_text, _, end_text = text.partition("-")
    start, end = parse_hhmm(start_text), parse_hhmm(end_text)
    if end <= start:
        raise ValidationError(f"the end ({end:%H:%M}) must follow the start ({start:%H:%M})")
    return start, end


def validate_probability(raw: str | float, config: Config) -> tuple[float, str | None]:
    """Validate a minimum probability. Returns ``(value, warning)``."""
    try:
        value = float(str(raw).strip().rstrip("%"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{raw!r} is not a number") from exc
    if value <= 0:
        raise ValidationError("the minimum probability must be greater than 0")
    if value > 100:
        raise ValidationError("the minimum probability must be at most 100")
    warning = None
    if value > PROBABILITY_WARN_ABOVE:
        warning = f"a minimum of {value:.1f}% is very high - almost nothing will qualify"
    return value, warning


def validate_odds(raw: str | float, config: Config) -> tuple[float, str | None]:
    """Validate a minimum odds figure against ``MAX_ODDS`` (spec 2.4)."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{raw!r} is not a number") from exc
    if value < 1.01:
        raise ValidationError("the minimum odds must be at least 1.01")
    if value >= config.max_odds:
        raise ValidationError(
            f"a minimum of {value:.2f} crosses the maximum of {config.max_odds:.2f}, "
            "leaving no admissible range"
        )
    return value, None


def validate_hours(raw: str | float) -> float:
    """Validate a positive number of hours ahead."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{raw!r} is not a number") from exc
    if value <= 0:
        raise ValidationError("hours ahead must be greater than 0")
    if value > 24 * 7:
        raise ValidationError("hours ahead must be at most 168 (one week)")
    return value


def check_window(
    window: AnalysisWindow,
    match_day: date,
    now: datetime | None = None,
) -> str | None:
    """Reject a window that cannot match, warn about one that has passed.

    Raises ``ValidationError`` when the window does not intersect the match
    day at all; returns a warning string when it merely lies in the past.
    """
    day_start, day_end = eat_day_bounds(match_day)
    if window.end < day_start or window.start > day_end:
        raise ValidationError(
            f"the window {window.label} does not intersect the match day {match_day.isoformat()}"
        )
    moment = to_eat(now or now_utc())
    if window.end < moment:
        return (
            f"the window {window.label} EAT on {match_day.isoformat()} has already passed; "
            "no fixture can kick off inside it"
        )
    return None


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------
@dataclass
class ResolvedSetup:
    """The outcome of section 2: a config, a window, and how we got there."""

    config: Config
    window: AnalysisWindow
    interactive: bool
    prompted: bool = False
    warnings: list[str] = field(default_factory=list)


def _window_source(config: Config) -> str:
    for name in ("window_start", "window_end", "next_hours"):
        if config.sources.get(name) == SOURCE_ENV:
            return SOURCE_ENV
    return config.sources.get("window", SOURCE_DEFAULT)


def _build_window(config: Config, match_day: date, now: datetime | None) -> AnalysisWindow:
    """Build the window implied by the current config values."""
    if config.next_hours:
        return AnalysisWindow.next_hours(match_day, float(config.next_hours), reference=now)
    start = parse_hhmm(config.window_start)
    end = parse_hhmm(config.window_end)
    kind = "full-day" if (start == time(0, 0) and end >= time(23, 59)) else "custom"
    return AnalysisWindow.from_times(match_day, start, end, kind=kind)


def resolve_configuration(
    config: Config,
    args: Any,
    match_day: date,
    *,
    io: PromptIO | None = None,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    stdin: Any = None,
) -> ResolvedSetup:
    """Apply flags, prompt for what is left, and build the analysis window.

    *config* should already carry environment-resolved values
    (``Config.from_env``). Flags are applied first and are never prompted for;
    the remaining settings are prompted only on an interactive run.
    """
    io = io or PromptIO()
    warnings: list[str] = []

    # --- 1. flags (highest priority) ------------------------------------
    from_flag: set[str] = set()

    window_flag = getattr(args, "window", None)
    next_hours_flag = getattr(args, "next_hours", None)
    if window_flag:
        start, end = parse_window_text(window_flag)
        config.set("window_start", f"{start:%H:%M}", SOURCE_FLAG)
        config.set("window_end", f"{end:%H:%M}", SOURCE_FLAG)
        config.set("next_hours", None, SOURCE_FLAG)
        config.sources["window"] = SOURCE_FLAG
        from_flag.add("window")
    elif next_hours_flag is not None:
        config.set("next_hours", validate_hours(next_hours_flag), SOURCE_FLAG)
        config.sources["window"] = SOURCE_FLAG
        from_flag.add("window")

    probability_flag = getattr(args, "min_probability", None)
    if probability_flag is not None:
        value, warning = validate_probability(probability_flag, config)
        config.set("min_probability", value, SOURCE_FLAG)
        from_flag.add("min_probability")
        if warning:
            warnings.append(warning)

    odds_flag = getattr(args, "min_odds", None)
    if odds_flag is not None:
        value, _ = validate_odds(odds_flag, config)
        config.set("min_odds", value, SOURCE_FLAG)
        from_flag.add("min_odds")

    if "window" not in config.sources:
        config.sources["window"] = _window_source(config)

    # --- 2. decide whether to prompt ------------------------------------
    interactive = is_interactive(bool(getattr(args, "no_prompt", False)), env=env, stdin=stdin)
    unresolved = [
        name for name in ("window", "min_probability", "min_odds") if name not in from_flag
    ]

    if not interactive or not unresolved:
        window = _build_window(config, match_day, now)
        warning = _safe_check_window(window, match_day, now, warnings)
        return ResolvedSetup(
            config=config,
            window=window,
            interactive=interactive,
            prompted=False,
            warnings=warnings,
        )

    # --- 3. prompt for what is left -------------------------------------
    io.say(TITLE)
    io.say("Press Enter to accept the default shown in brackets.")
    io.say("")

    if "window" in unresolved:
        _prompt_window(config, match_day, io, now, warnings)
    if "min_probability" in unresolved:
        _prompt_probability(config, io, warnings)
    if "min_odds" in unresolved:
        _prompt_odds(config, io, warnings)

    window = _build_window(config, match_day, now)
    _safe_check_window(window, match_day, now, warnings)

    io.say("")
    io.say(
        f"Analysing {match_day.isoformat()} | window {window.label} EAT | "
        f"min probability {config.min_probability:.1f}% | min odds {config.min_odds:.2f}"
    )
    for warning in warnings:
        io.say(f"Warning: {warning}")
    answer = io.read("Proceed? [Y/n]: ").strip().lower()
    if answer in {"n", "no"}:
        raise PromptAborted("declined at the confirmation prompt")

    return ResolvedSetup(
        config=config,
        window=window,
        interactive=True,
        prompted=True,
        warnings=warnings,
    )


def _safe_check_window(
    window: AnalysisWindow,
    match_day: date,
    now: datetime | None,
    warnings: list[str],
) -> None:
    """Collect a window warning without letting it stop a resolved run."""
    try:
        warning = check_window(window, match_day, now)
    except ValidationError as exc:
        warnings.append(str(exc))
        return
    if warning:
        warnings.append(warning)


# --------------------------------------------------------------------------
# Individual prompts
# --------------------------------------------------------------------------
def _prompt_window(
    config: Config,
    match_day: date,
    io: PromptIO,
    now: datetime | None,
    warnings: list[str],
) -> None:
    """Prompt for the analysis window, exactly as laid out in spec 2.3."""
    default_is_full_day = config.window_start == "00:00" and config.window_end >= "23:59"
    default_label = (
        f"Full day, {config.window_start}-{config.window_end}"
        if default_is_full_day
        else f"Default window, {config.window_start}-{config.window_end}"
    )

    io.say("Analysis window (EAT)")
    io.say(f"  [1] {default_label}   (default)")
    io.say("  [2] Next N hours")
    io.say("  [3] Custom window, HH:MM-HH:MM")

    for _attempt in range(MAX_ATTEMPTS):
        choice = io.read("Choice [1]: ").strip()
        if choice == "":
            choice = "1"
        try:
            if choice == "1":
                config.sources["window"] = SOURCE_PROMPT
                return
            if choice == "2":
                _prompt_next_hours(config, match_day, io, now)
                return
            if choice == "3":
                _prompt_custom_window(config, match_day, io, now)
                return
            raise ValidationError("choose 1, 2 or 3")
        except ValidationError as exc:
            io.say(f"  {exc}. Please try again.")

    warnings.append(
        f"three invalid answers for the analysis window; "
        f"falling back to the default {config.window_start}-{config.window_end} EAT"
    )
    io.say(f"  Using the default window {config.window_start}-{config.window_end} EAT.")


def _prompt_next_hours(config: Config, match_day: date, io: PromptIO, now: datetime | None) -> None:
    default_hours = config.default_next_hours
    for _ in range(MAX_ATTEMPTS):
        raw = io.read(f"  Hours ahead [{default_hours:g}]: ").strip()
        hours = default_hours if raw == "" else None
        try:
            if hours is None:
                hours = validate_hours(raw)
            window = AnalysisWindow.next_hours(match_day, hours, reference=now)
            check_window(window, match_day, now)
            config.set("next_hours", hours, SOURCE_PROMPT)
            config.set("window_start", f"{to_eat(window.start):%H:%M}", SOURCE_PROMPT)
            config.set("window_end", f"{to_eat(window.end):%H:%M}", SOURCE_PROMPT)
            config.sources["window"] = SOURCE_PROMPT
            io.say(f"  -> Window: {window.label} EAT")
            return
        except ValidationError as exc:
            io.say(f"  {exc}. Please try again.")
    raise ValidationError("hours ahead could not be read")


def _prompt_custom_window(
    config: Config, match_day: date, io: PromptIO, now: datetime | None
) -> None:
    default_text = f"{config.window_start}-{config.window_end}"
    for _ in range(MAX_ATTEMPTS):
        raw = io.read(f"  Window HH:MM-HH:MM [{default_text}]: ").strip()
        if raw == "":
            raw = default_text
        try:
            start, end = parse_window_text(raw)
            window = AnalysisWindow.from_times(match_day, start, end)
            check_window(window, match_day, now)
            config.set("window_start", f"{start:%H:%M}", SOURCE_PROMPT)
            config.set("window_end", f"{end:%H:%M}", SOURCE_PROMPT)
            config.set("next_hours", None, SOURCE_PROMPT)
            config.sources["window"] = SOURCE_PROMPT
            io.say(f"  -> Window: {window.label} EAT")
            return
        except ValidationError as exc:
            io.say(f"  {exc}. Please try again.")
    raise ValidationError("the custom window could not be read")


def _prompt_probability(config: Config, io: PromptIO, warnings: list[str]) -> None:
    default_value = config.min_probability
    for _ in range(MAX_ATTEMPTS):
        raw = io.read(f"Minimum probability %  [{default_value:.1f}]: ").strip()
        if raw == "":
            config.sources["min_probability"] = SOURCE_PROMPT
            return
        try:
            value, warning = validate_probability(raw, config)
        except ValidationError as exc:
            io.say(f"  {exc}. Please try again.")
            continue
        config.set("min_probability", value, SOURCE_PROMPT)
        if warning:
            io.say(f"  Warning: {warning}")
            warnings.append(warning)
        return

    warnings.append(
        f"three invalid answers for the minimum probability; "
        f"falling back to the default {default_value:.1f}%"
    )
    io.say(f"  Using the default minimum probability {default_value:.1f}%.")


def _prompt_odds(config: Config, io: PromptIO, warnings: list[str]) -> None:
    default_value = config.min_odds
    for _ in range(MAX_ATTEMPTS):
        raw = io.read(f"Minimum odds           [{default_value:.2f}]: ").strip()
        if raw == "":
            config.sources["min_odds"] = SOURCE_PROMPT
            return
        try:
            value, _ = validate_odds(raw, config)
        except ValidationError as exc:
            io.say(f"  {exc}. Please try again.")
            continue
        config.set("min_odds", value, SOURCE_PROMPT)
        return

    warnings.append(
        f"three invalid answers for the minimum odds; "
        f"falling back to the default {default_value:.2f}"
    )
    io.say(f"  Using the default minimum odds {default_value:.2f}.")
