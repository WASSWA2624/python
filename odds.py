"""Bookmaker odds: margin removal, value, and expected value (spec 6).

The important point is section 6.1: ``1 / decimal_odds`` includes the
bookmaker's margin and so overstates the true implied probability. Comparing a
model estimate against it biases every fixture toward "no value". Where both
sides of the Over/Under 1.5 pair are priced, the margin is removed first and
the fair figure is what every value calculation uses.
"""

from __future__ import annotations

from dataclasses import dataclass

#: An overround below this is arithmetically impossible for a real two-way
#: market and means the pair of prices is inconsistent (spec 9, 12).
MIN_PLAUSIBLE_OVERROUND = 0.995
#: Above this the pair is almost certainly mismatched rather than merely
#: expensive - a 40% book on a two-way market does not occur in practice.
MAX_PLAUSIBLE_OVERROUND = 1.40


class OddsError(ValueError):
    """Raised when a price cannot be interpreted as decimal odds."""


def validate_decimal_odds(odds: float | None) -> float | None:
    """Return *odds* if it is usable decimal odds, else ``None``.

    Decimal odds of 1.0 imply certainty and a payout of the stake alone;
    anything at or below that is a data error rather than a price.
    """
    if odds is None:
        return None
    try:
        value = float(odds)
    except (TypeError, ValueError):
        return None
    if value <= 1.0 or value != value:  # NaN-safe
        return None
    return value


def implied_probability(odds: float | None) -> float | None:
    """Raw implied probability, margin included: ``1 / decimal_odds``."""
    value = validate_decimal_odds(odds)
    return None if value is None else 1.0 / value


def overround(odds_over: float | None, odds_under: float | None) -> float | None:
    """Book total for the Over/Under pair: ``1/over + 1/under``."""
    over = implied_probability(odds_over)
    under = implied_probability(odds_under)
    if over is None or under is None:
        return None
    return over + under


def fair_probability(
    odds_over: float | None, odds_under: float | None
) -> tuple[float | None, float | None, bool]:
    """Margin-free implied probability of the Over side.

    Returns ``(implied_fair, overround, margin_adjusted)``.

    Uses proportional (multiplicative) margin removal::

        implied_fair = (1 / odds_over) / overround

    Where the Under price is missing, falls back to the raw figure and reports
    ``margin_adjusted = False`` so the row can be flagged (spec 6.1).
    """
    raw = implied_probability(odds_over)
    if raw is None:
        return None, None, False
    book = overround(odds_over, odds_under)
    if book is None or book <= 0:
        return raw, None, False
    return raw / book, book, True


def value_edge(model_probability: float | None, implied_fair: float | None) -> float | None:
    """``Value = model_probability - implied_fair`` (spec 6.2), in points."""
    if model_probability is None or implied_fair is None:
        return None
    return model_probability - implied_fair


def expected_value(model_probability: float | None, odds: float | None) -> float | None:
    """``EV = (model_probability * decimal_odds) - 1`` per unit staked."""
    price = validate_decimal_odds(odds)
    if model_probability is None or price is None:
        return None
    return model_probability * price - 1.0


def kelly_fraction(model_probability: float | None, odds: float | None) -> float | None:
    """Full-Kelly stake fraction, shown for information only.

    The program recommends no stake sizes (spec 6.2); this exists so the figure
    can be reported next to EV. Negative edges return 0.0 rather than a
    negative stake.
    """
    price = validate_decimal_odds(odds)
    if model_probability is None or price is None:
        return None
    b = price - 1.0
    if b <= 0:
        return 0.0
    edge = (b * model_probability) - (1.0 - model_probability)
    return max(0.0, edge / b)


@dataclass
class OddsAssessment:
    """Everything section 6 asks to be reported for one fixture."""

    implied_raw: float | None
    implied_fair: float | None
    overround: float | None
    margin_adjusted: bool
    value: float | None
    ev: float | None
    kelly: float | None
    warnings: list[str]

    @property
    def has_positive_edge(self) -> bool:
        return self.value is not None and self.value > 0


def assess(
    model_probability: float | None,
    odds_over: float | None,
    odds_under: float | None = None,
) -> OddsAssessment:
    """Full section 6 assessment for one fixture."""
    warnings: list[str] = []
    implied_fair, book, adjusted = fair_probability(odds_over, odds_under)

    if implied_fair is None:
        warnings.append("Over 1.5 price unavailable or invalid")
    elif not adjusted:
        warnings.append("margin-unadjusted (Under 1.5 price unavailable)")

    if book is not None:
        if book < MIN_PLAUSIBLE_OVERROUND:
            warnings.append(f"implausible overround {book:.3f} (below 1.0)")
        elif book > MAX_PLAUSIBLE_OVERROUND:
            warnings.append(f"implausible overround {book:.3f} - prices may be mismatched")

    return OddsAssessment(
        implied_raw=implied_probability(odds_over),
        implied_fair=implied_fair,
        overround=book,
        margin_adjusted=adjusted,
        value=value_edge(model_probability, implied_fair),
        ev=expected_value(model_probability, odds_over),
        kelly=kelly_fraction(model_probability, odds_over),
        warnings=warnings,
    )
