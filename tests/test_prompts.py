"""Interactive setup: precedence, non-interactive fallback, validation.

Covers testing bullets 5, 6 and 7 of specification 13:

* precedence resolution - flag beats prompt beats environment beats default
* non-interactive fallback - stdin not a TTY uses defaults and never blocks
* prompt validation - bad input re-prompts rather than crashing
"""

from __future__ import annotations

import io
from argparse import Namespace
from datetime import date, datetime, time

import pytest
from conftest import MATCH_DAY

from config import (
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_FLAG,
    SOURCE_PROMPT,
    Config,
)
from models import EAT, AnalysisWindow, eat_day_bounds
from prompts import (
    MAX_ATTEMPTS,
    PromptAborted,
    PromptIO,
    ValidationError,
    check_window,
    is_interactive,
    parse_hhmm,
    parse_window_text,
    resolve_configuration,
    validate_hours,
    validate_odds,
    validate_probability,
)

NOW = datetime(2026, 9, 8, 14, 30, tzinfo=EAT)


class ScriptedIO(PromptIO):
    """Feeds a fixed list of answers and captures everything printed."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.output: list[str] = []
        super().__init__(ask=self._ask, say=self.output.append)

    def _ask(self, prompt: str) -> str:
        self.output.append(prompt)
        if not self.answers:
            raise AssertionError(f"the prompt asked for more input than expected: {prompt!r}")
        return self.answers.pop(0)

    @property
    def text(self) -> str:
        return "\n".join(self.output)


def args(**overrides) -> Namespace:
    base = dict(window=None, next_hours=None, min_probability=None, min_odds=None, no_prompt=False)
    base.update(overrides)
    return Namespace(**base)


def tty(is_a_tty: bool = True):
    stream = io.StringIO()
    stream.isatty = lambda: is_a_tty  # type: ignore[method-assign]
    return stream


# ==========================================================================
# Precedence (spec 2.2)
# ==========================================================================
def test_default_is_used_when_nothing_else_supplies_a_value():
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=ScriptedIO([]), env={}, now=NOW, stdin=tty(False)
    )
    assert setup.config.min_probability == 70.0
    assert setup.config.min_odds == 1.20
    assert setup.config.source_of("min_probability") == SOURCE_DEFAULT


def test_environment_beats_default():
    config = Config.from_env({"OVER15_MIN_PROBABILITY": "72.5", "OVER15_MIN_ODDS": "1.45"})
    setup = resolve_configuration(
        config, args(), MATCH_DAY, io=ScriptedIO([]), env={}, now=NOW, stdin=tty(False)
    )
    assert setup.config.min_probability == 72.5
    assert setup.config.min_odds == 1.45
    assert setup.config.source_of("min_probability") == SOURCE_ENV


def test_the_bare_specification_name_is_read_too():
    config = Config.from_env({"MIN_ODDS": "1.60", "OUTPUT_DIR": "elsewhere"})
    assert config.min_odds == 1.60
    assert config.output_dir == "elsewhere"


def test_the_prefixed_name_wins_over_the_bare_one():
    config = Config.from_env({"MIN_ODDS": "1.60", "OVER15_MIN_ODDS": "1.75"})
    assert config.min_odds == 1.75


def test_prompt_beats_environment():
    config = Config.from_env({"OVER15_MIN_PROBABILITY": "72.5"})
    io_ = ScriptedIO(["1", "80", "", "y"])
    setup = resolve_configuration(
        config, args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.config.min_probability == 80.0
    assert setup.config.source_of("min_probability") == SOURCE_PROMPT


def test_accepting_a_prompt_default_keeps_the_value_and_its_real_source():
    """Enter accepts what was shown; it does not relabel where it came from."""
    config = Config.from_env({"OVER15_MIN_PROBABILITY": "72.5"})
    io_ = ScriptedIO(["1", "", "", "y"])
    setup = resolve_configuration(
        config, args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.config.min_probability == 72.5
    assert setup.config.source_of("min_probability") == SOURCE_ENV
    assert setup.config.source_of("min_odds") == SOURCE_DEFAULT


def test_flag_beats_prompt():
    """The headline rule: a flagged setting is not prompted for at all."""
    io_ = ScriptedIO(["1", "", "y"])  # window and odds only - no probability prompt
    setup = resolve_configuration(
        Config(), args(min_probability=88.0), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.config.min_probability == 88.0
    assert setup.config.source_of("min_probability") == SOURCE_FLAG
    assert "Minimum probability" not in io_.text


def test_flag_beats_environment():
    config = Config.from_env({"OVER15_MIN_ODDS": "1.45"})
    setup = resolve_configuration(
        config, args(min_odds=1.90), MATCH_DAY, io=ScriptedIO([]), env={}, now=NOW, stdin=tty(False)
    )
    assert setup.config.min_odds == 1.90
    assert setup.config.source_of("min_odds") == SOURCE_FLAG


def test_the_full_precedence_chain_in_one_run():
    """Flag > prompt > environment > default, all four at once."""
    config = Config.from_env({"OVER15_MIN_ODDS": "1.45"})  # environment
    io_ = ScriptedIO(["77", "", "y"])  # probability typed, odds left at the default
    setup = resolve_configuration(
        config, args(window="16:00-20:00"), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.config.window_start == "16:00"  # flag
    assert setup.config.source_of("window") == SOURCE_FLAG
    assert setup.config.min_probability == 77.0  # prompt
    assert setup.config.source_of("min_probability") == SOURCE_PROMPT
    assert setup.config.min_odds == 1.45  # environment
    assert setup.config.source_of("min_odds") == SOURCE_ENV
    assert setup.config.max_odds == 5.00  # default
    assert setup.config.source_of("max_odds") == SOURCE_DEFAULT


def test_all_three_flags_make_the_run_non_interactive_without_no_prompt():
    """Spec 2.2 - flags alone are enough; --no-prompt is not required."""
    io_ = ScriptedIO([])  # any prompt at all would raise
    setup = resolve_configuration(
        Config(),
        args(window="10:00-14:00", min_probability=75.0, min_odds=1.35),
        MATCH_DAY,
        io=io_,
        env={},
        now=NOW,
        stdin=tty(True),
    )
    assert setup.prompted is False
    assert io_.text == ""


# ==========================================================================
# Non-interactive execution (spec 2.5)
# ==========================================================================
@pytest.mark.parametrize(
    ("no_prompt", "env", "isatty"),
    [
        (True, {}, True),  # --no-prompt
        (False, {}, False),  # stdin is not a TTY
        (False, {"CI": "1"}, True),  # running under CI
    ],
)
def test_prompting_is_skipped_in_an_unattended_run(no_prompt, env, isatty):
    assert is_interactive(no_prompt, env=env, stdin=tty(isatty)) is False


def test_an_interactive_terminal_does_prompt():
    assert is_interactive(False, env={}, stdin=tty(True)) is True


def test_an_unattended_run_never_blocks_and_uses_the_defaults():
    """The single most important property of section 2.5."""
    io_ = ScriptedIO([])  # asking anything would raise AssertionError
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(False)
    )
    assert setup.interactive is False
    assert setup.prompted is False
    assert setup.config.min_probability == 70.0
    assert setup.config.min_odds == 1.20
    assert setup.window.is_full_day
    assert io_.text == ""


def test_a_missing_stdin_is_treated_as_unattended():
    assert is_interactive(False, env={}, stdin=None) in {True, False}  # never raises
    assert is_interactive(False, env={}, stdin=object()) is False


def test_end_of_input_mid_prompt_falls_back_to_the_default():
    """stdin can vanish; that must accept the default, not crash."""

    def raise_eof(prompt: str) -> str:
        raise EOFError

    io_ = PromptIO(ask=raise_eof, say=lambda _: None)
    assert io_.read("anything: ") == ""


def test_ctrl_c_at_a_prompt_aborts_cleanly():
    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    io_ = PromptIO(ask=interrupt, say=lambda _: None)
    with pytest.raises(PromptAborted):
        io_.read("anything: ")


def test_declining_the_confirmation_aborts():
    io_ = ScriptedIO(["1", "", "", "n"])
    with pytest.raises(PromptAborted):
        resolve_configuration(Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True))


# ==========================================================================
# Validation (spec 2.4)
# ==========================================================================
@pytest.mark.parametrize("raw", ["70", "70.0", "70%", " 70 "])
def test_probability_accepts_the_obvious_spellings(raw):
    value, warning = validate_probability(raw, Config())
    assert value == 70.0 and warning is None


@pytest.mark.parametrize("raw", ["0", "-5", "101", "abc", ""])
def test_probability_rejects_out_of_range_and_nonsense(raw):
    with pytest.raises(ValidationError):
        validate_probability(raw, Config())


def test_a_very_high_probability_warns_rather_than_failing():
    """Spec 2.4 - warn above 95, where almost nothing will qualify."""
    value, warning = validate_probability("97", Config())
    assert value == 97.0
    assert warning and "very high" in warning


def test_odds_must_be_at_least_one_point_zero_one():
    with pytest.raises(ValidationError, match="1.01"):
        validate_odds("1.00", Config())


def test_odds_crossing_the_maximum_are_rejected_with_an_explanation():
    """Spec 2.4 - reject when the two would leave no admissible range."""
    with pytest.raises(ValidationError, match="no admissible range"):
        validate_odds("6.00", Config())


def test_hours_ahead_must_be_positive():
    assert validate_hours("8") == 8.0
    for bad in ("0", "-3", "abc", "500"):
        with pytest.raises(ValidationError):
            validate_hours(bad)


@pytest.mark.parametrize("raw", ["14:30", "1430", "14"])
def test_clock_times_are_parsed_forgivingly(raw):
    assert parse_hhmm(raw).hour == 14


@pytest.mark.parametrize("raw", ["25:00", "14:70", "abc", ""])
def test_bad_clock_times_are_rejected(raw):
    with pytest.raises(ValidationError):
        parse_hhmm(raw)


def test_an_inverted_window_is_rejected():
    with pytest.raises(ValidationError, match="must follow"):
        parse_window_text("22:00-14:00")


def test_an_equal_ended_window_is_rejected():
    with pytest.raises(ValidationError):
        parse_window_text("14:00-14:00")


def test_window_dashes_of_every_kind_are_accepted():
    assert parse_window_text("14:00-22:00") == (time(14, 0), time(22, 0))
    assert parse_window_text("14:00 to 22:00") == (time(14, 0), time(22, 0))
    assert parse_window_text("14:00–22:00") == (time(14, 0), time(22, 0))


def test_a_window_that_misses_the_match_day_is_rejected():
    other_day = date(2026, 9, 20)
    window = AnalysisWindow.from_times(other_day, time(10, 0), time(12, 0))
    with pytest.raises(ValidationError, match="does not intersect"):
        check_window(window, MATCH_DAY, NOW)


def test_a_window_that_has_already_passed_only_warns():
    window = AnalysisWindow.from_times(MATCH_DAY, time(8, 0), time(9, 0))
    warning = check_window(window, MATCH_DAY, NOW)
    assert warning and "already passed" in warning


def test_a_future_window_raises_no_warning():
    window = AnalysisWindow.from_times(MATCH_DAY, time(18, 0), time(22, 0))
    assert check_window(window, MATCH_DAY, NOW) is None


# ==========================================================================
# Re-prompting rather than crashing (spec 2.4)
# ==========================================================================
def test_invalid_probability_re_prompts_with_a_reason():
    io_ = ScriptedIO(["1", "150", "75", "", "y"])
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.config.min_probability == 75.0
    assert "at most 100" in io_.text
    assert "Please try again" in io_.text


def test_invalid_odds_re_prompt_with_a_reason():
    io_ = ScriptedIO(["1", "", "9.00", "1.35", "y"])
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.config.min_odds == 1.35
    assert "no admissible range" in io_.text


def test_an_inverted_window_re_prompts():
    io_ = ScriptedIO(["3", "22:00-14:00", "16:00-20:00", "", "", "y"])
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.config.window_start == "16:00"
    assert setup.config.window_end == "20:00"
    assert "must follow" in io_.text


def test_an_unrecognised_menu_choice_re_prompts():
    io_ = ScriptedIO(["9", "1", "", "", "y"])
    resolve_configuration(Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True))
    assert "choose 1, 2 or 3" in io_.text


def test_three_bad_answers_fall_back_to_the_default_with_a_warning():
    """Spec 2.4 - after three attempts, use the default and carry on."""
    io_ = ScriptedIO(["1"] + ["nonsense"] * MAX_ATTEMPTS + ["", "y"])
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.config.min_probability == 70.0
    assert any("falling back to the default" in w for w in setup.warnings)


# ==========================================================================
# The window itself (spec 2.1, 2.3)
# ==========================================================================
def test_pressing_enter_three_times_gives_the_documented_defaults():
    """Spec 14.1 - accepting every default produces a full-day analysis."""
    io_ = ScriptedIO(["", "", "", "y"])
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.window.is_full_day
    assert setup.window.label == "00:00 - 23:59"
    assert setup.config.min_probability == 70.0
    assert setup.config.min_odds == 1.20


def test_option_two_measures_from_now_and_clamps_to_the_match_day():
    """Spec 2.3 - the worked example: 8 hours from 14:30 gives 14:30-22:30."""
    io_ = ScriptedIO(["2", "8", "", "", "y"])
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.window.label == "14:30 - 22:30"
    assert "-> Window: 14:30 - 22:30 EAT" in io_.text


def test_next_hours_is_clamped_to_the_end_of_the_match_day():
    window = AnalysisWindow.next_hours(MATCH_DAY, 48, reference=NOW)
    _, day_end = eat_day_bounds(MATCH_DAY)
    assert window.end <= day_end


def test_next_hours_for_a_future_day_starts_at_that_day():
    later = date(2026, 9, 20)
    window = AnalysisWindow.next_hours(later, 6, reference=NOW)
    day_start, _ = eat_day_bounds(later)
    assert window.start == day_start


def test_option_three_takes_a_custom_window():
    io_ = ScriptedIO(["3", "16:00-20:00", "", "", "y"])
    setup = resolve_configuration(
        Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True)
    )
    assert setup.window.label == "16:00 - 20:00"
    assert setup.window.kind == "custom"


def test_the_window_flag_and_next_hours_flag_are_mutually_exclusive():
    """Spec 13 - supplying both is an argument error."""
    from main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--window", "10:00-12:00", "--next-hours", "6"])


def test_the_confirmation_line_restates_the_resolved_settings():
    io_ = ScriptedIO(["2", "8", "75", "1.35", "y"])
    resolve_configuration(Config(), args(), MATCH_DAY, io=io_, env={}, now=NOW, stdin=tty(True))
    assert "window 14:30 - 22:30 EAT" in io_.text
    assert "min probability 75.0%" in io_.text
    assert "min odds 1.35" in io_.text
    assert "Proceed? [Y/n]: " in io_.text
