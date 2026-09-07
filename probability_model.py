"""The Over 1.5 probability estimate (spec 5).

Method
------
A Poisson goals model, the standard approach for total-goals markets. Expected
goals are estimated for each side, summed, and the tail is read off the Poisson
distribution::

    P(0 goals)  = exp(-lambda)
    P(1 goal)   = lambda * exp(-lambda)
    P(Over 1.5) = 1 - exp(-lambda) * (1 + lambda)

Sanity checks from the spec: lambda = 2.7 gives about 75%, lambda = 3.0 about
80%.

A Dixon-Coles low-score correction is implemented and configurable
(``use_dixon_coles``). It is **off by default** so the documented closed form
above holds exactly; when enabled, the three scorelines that decide the market
(0-0, 1-0, 0-1) are reweighted by the Dixon-Coles tau function, which is where
the correction does its work.

Blending (spec 5.2)
-------------------
Neither lambda comes from a single statistic. Five components each propose an
expected-goals pair, and they are combined with the section 5.2 weights.
**A component with no data drops out and its weight is redistributed over the
components that do have data**, so a missing input never behaves like a zero.
Supporting context is not a component but a multiplier applied at its own
weight, which keeps speculative information from materially moving the estimate
(spec 4.5).

Shrinkage (spec 5.3)
--------------------
The blended estimate is pulled toward the league baseline by
``n / (n + k)``, where ``n`` is the effective sample size and ``k`` is
``shrinkage_strength``. Three recorded matches therefore carry far less weight
than twenty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from config import Config
from models import LeagueStats, MatchStatistics

#: Modelling prior used only when no league baseline is available. This is a
#: documented prior, not a measured statistic: it is never written to the
#: league columns and its use is recorded in ``model_notes``.
GLOBAL_BASELINE_GOALS = 2.60

#: Confidence bands (spec 5.4). Driven by sample size, how many independent
#: components agreed, how far apart they were, and data completeness.
CONFIDENCE_HIGH_MIN_SAMPLES = 10
CONFIDENCE_HIGH_MIN_COMPONENTS = 4
CONFIDENCE_HIGH_MAX_SPREAD = 0.80
CONFIDENCE_HIGH_MIN_COMPLETENESS = 0.70
CONFIDENCE_MEDIUM_MIN_SAMPLES = 5
CONFIDENCE_MEDIUM_MIN_COMPONENTS = 3
CONFIDENCE_MEDIUM_MAX_SPREAD = 1.50
CONFIDENCE_MEDIUM_MIN_COMPLETENESS = 0.40


# --------------------------------------------------------------------------
# Poisson core
# --------------------------------------------------------------------------
def poisson_pmf(k: int, lam: float) -> float:
    """P(X = k) for a Poisson variable with mean *lam*."""
    if lam < 0:
        raise ValueError("lambda must not be negative")
    if k < 0:
        return 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def over_15_probability(lambda_total: float) -> float:
    """``P(Over 1.5) = 1 - exp(-lambda) * (1 + lambda)`` (spec 5.1)."""
    if lambda_total < 0:
        raise ValueError("lambda must not be negative")
    return 1.0 - math.exp(-lambda_total) * (1.0 + lambda_total)


def dixon_coles_over_15(lambda_home: float, lambda_away: float, rho: float) -> float:
    """Over 1.5 under a Dixon-Coles corrected bivariate Poisson.

    Only the 0-0, 1-0 and 0-1 scorelines matter for this market, and those are
    exactly the cells the tau function adjusts::

        tau(0,0) = 1 - lambda_home * lambda_away * rho
        tau(1,0) = 1 + lambda_away * rho
        tau(0,1) = 1 + lambda_home * rho

    The result is clamped to [0, 1]: for large rho the correction is not
    guaranteed to keep every cell a valid probability.
    """
    if not 0 <= rho < 1:
        raise ValueError("rho must be in [0, 1)")
    p_home_0 = math.exp(-lambda_home)
    p_away_0 = math.exp(-lambda_away)
    p_home_1 = lambda_home * p_home_0
    p_away_1 = lambda_away * p_away_0

    p00 = p_home_0 * p_away_0 * (1.0 - lambda_home * lambda_away * rho)
    p10 = p_home_1 * p_away_0 * (1.0 + lambda_away * rho)
    p01 = p_home_0 * p_away_1 * (1.0 + lambda_home * rho)

    return min(1.0, max(0.0, 1.0 - (p00 + p10 + p01)))


def probability_from_lambdas(lambda_home: float, lambda_away: float, config: Config) -> float:
    """Dispatch to the plain or Dixon-Coles form according to configuration."""
    if config.use_dixon_coles and config.dixon_coles_rho > 0:
        return dixon_coles_over_15(lambda_home, lambda_away, config.dixon_coles_rho)
    return over_15_probability(lambda_home + lambda_away)


def lambda_from_over_15_rate(rate: float, tolerance: float = 1e-10) -> float:
    """Invert the Over 1.5 formula: the lambda implying an observed *rate*.

    ``P(Over 1.5)`` is strictly increasing in lambda, so a bisection is exact
    and cheap. This is how an observed Over 1.5 percentage becomes an
    expected-goals figure that can be blended with the others.
    """
    if not 0.0 <= rate < 1.0:
        if rate >= 1.0:  # an all-Over sample: cap rather than diverge
            return 12.0
        raise ValueError("rate must be in [0, 1)")
    if rate <= 0.0:
        return 0.0
    low, high = 0.0, 12.0
    while high - low > tolerance:
        mid = (low + high) / 2.0
        if over_15_probability(mid) < rate:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


# --------------------------------------------------------------------------
# Result container
# --------------------------------------------------------------------------
@dataclass
class ModelResult:
    """Section 5.4 output plus the working needed to explain it."""

    lambda_home: float | None = None
    lambda_away: float | None = None
    lambda_total: float | None = None
    probability: float | None = None
    confidence: str = "Low"
    data_completeness: float = 0.0
    n_effective: float = 0.0
    spread: float | None = None
    components: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    usable: bool = False
    reason: str = ""

    @property
    def note_text(self) -> str:
        return "; ".join(self.notes)


# --------------------------------------------------------------------------
# Component estimators
# --------------------------------------------------------------------------
def _split_total(total: float, home_share: float) -> tuple[float, float]:
    """Divide an expected total between the sides by *home_share*."""
    home_share = min(0.95, max(0.05, home_share))
    return total * home_share, total * (1.0 - home_share)


def _baseline_league(stats: MatchStatistics, config: Config) -> tuple[LeagueStats, float, bool]:
    """League baseline goals and home share, falling back to the global prior."""
    league = stats.league
    if league.available and league.avg_goals is not None:
        share = league.home_goal_share
        if share is None:
            share = config.league_home_goal_share
        return league, float(league.avg_goals), False
    return league, GLOBAL_BASELINE_GOALS, True


def _component_recent_form(stats: MatchStatistics) -> tuple[float, float] | None:
    """Spec 4.2 - attack of one side against the defence of the other."""
    home, away = stats.home_form, stats.away_form
    if not (home.available and away.available):
        return None
    if None in (home.avg_scored, home.avg_conceded, away.avg_scored, away.avg_conceded):
        return None
    lam_home = 0.5 * (home.avg_scored + away.avg_conceded)
    lam_away = 0.5 * (away.avg_scored + home.avg_conceded)
    return lam_home, lam_away


def _component_home_away_split(stats: MatchStatistics, home_share: float) -> tuple[float, float] | None:
    """Spec 4.3 - the home side at home, the away side away."""
    home, away = stats.home_home_split, stats.away_away_split
    if not (home.available and away.available):
        return None
    if None not in (home.avg_scored, home.avg_conceded, away.avg_scored, away.avg_conceded):
        return (
            0.5 * (home.avg_scored + away.avg_conceded),
            0.5 * (away.avg_scored + home.avg_conceded),
        )
    if home.avg_total is not None and away.avg_total is not None:
        return _split_total(0.5 * (home.avg_total + away.avg_total), home_share)
    return None


def _component_over15_rate(stats: MatchStatistics, home_share: float) -> tuple[float, float] | None:
    """Spec 5.2 - the observed Over 1.5 rate, inverted into expected goals."""
    rates = [
        form.over15_rate
        for form in (stats.home_form, stats.away_form)
        if form.available and form.over15_rate is not None
    ]
    if not rates:
        return None
    mean_rate = sum(rates) / len(rates)
    return _split_total(lambda_from_over_15_rate(min(mean_rate, 0.999)), home_share)


def _component_h2h(stats: MatchStatistics, config: Config, home_share: float) -> tuple[float, float] | None:
    """Spec 4.1 - previous meetings, already age-discounted by the provider."""
    h2h = stats.h2h
    if not h2h.available or h2h.meetings < config.min_h2h_matches:
        return None
    share = h2h.home_goal_share if h2h.home_goal_share is not None else home_share
    return _split_total(float(h2h.avg_total_goals), share)


def _component_league(baseline_goals: float, home_share: float) -> tuple[float, float]:
    """Spec 4.4 - the league baseline, always available via the prior."""
    return _split_total(baseline_goals, home_share)


# --------------------------------------------------------------------------
# Estimation
# --------------------------------------------------------------------------
def estimate(stats: MatchStatistics, config: Config) -> ModelResult:
    """Estimate the Over 1.5 probability for one fixture.

    Returns a result with ``usable=False`` and a stated ``reason`` when there
    is not enough evidence; the caller records that as the exclusion reason
    rather than inventing a number (spec 12).
    """
    result = ModelResult(data_completeness=stats.completeness())

    league, baseline_goals, using_prior = _baseline_league(stats, config)
    home_share = league.home_goal_share if league.home_goal_share is not None else config.league_home_goal_share
    home_share = min(0.95, max(0.05, float(home_share)))
    if using_prior:
        result.notes.append(
            f"no league baseline available; using the documented global prior of "
            f"{GLOBAL_BASELINE_GOALS:.2f} goals per match"
        )

    weights = config.blend
    candidates: dict[str, tuple[tuple[float, float] | None, float]] = {
        "recent_form": (_component_recent_form(stats), weights.recent_form),
        "home_away_split": (_component_home_away_split(stats, home_share), weights.home_away_split),
        "over15_rate": (_component_over15_rate(stats, home_share), weights.over15_rate),
        "h2h": (_component_h2h(stats, config, home_share), weights.h2h),
        "league": (
            _component_league(baseline_goals, home_share) if not using_prior else None,
            weights.league,
        ),
    }

    available = {name: (pair, w) for name, (pair, w) in candidates.items() if pair is not None and w > 0}

    # The league prior alone is not evidence about *these* teams. Requiring at
    # least one team-specific component keeps the model from reporting a
    # league average dressed up as a match estimate.
    team_specific = [n for n in available if n != "league"]
    if not team_specific:
        result.reason = "insufficient historical data (no team, head-to-head, or league statistics)"
        result.notes.append(result.reason)
        return result

    total_weight = sum(w for _, w in available.values())
    lam_home = sum(pair[0] * w for pair, w in available.values()) / total_weight
    lam_away = sum(pair[1] * w for pair, w in available.values()) / total_weight
    result.components = {name: (pair[0], pair[1], w / total_weight) for name, (pair, w) in available.items()}

    if len(available) < len(candidates):
        dropped = sorted(set(candidates) - set(available))
        result.notes.append(f"components unavailable and reweighted: {', '.join(dropped)}")

    # --- home advantage ------------------------------------------------
    # Applied as a variance-preserving tilt: the total is left alone and only
    # the split between the sides moves, because the components above are
    # already measured on real home and away matches.
    tilt = math.sqrt(max(config.home_advantage, 0.01))
    lam_home, lam_away = lam_home * tilt, lam_away / tilt

    # --- shrinkage toward the league baseline (spec 5.3) ---------------
    n_eff = _effective_sample_size(stats)
    k = max(config.shrinkage_strength, 0.0)
    shrink = n_eff / (n_eff + k) if (n_eff + k) > 0 else 1.0
    base_home, base_away = _split_total(baseline_goals, home_share)
    lam_home = shrink * lam_home + (1 - shrink) * base_home
    lam_away = shrink * lam_away + (1 - shrink) * base_away
    if shrink < 0.75:
        result.notes.append(
            f"small sample (n={n_eff:.1f}); estimate pulled {100 * (1 - shrink):.0f}% "
            f"toward the baseline"
        )

    # --- supporting context (spec 4.5), at its own weight ---------------
    context = stats.context
    if context.available:
        w_ctx = max(0.0, min(1.0, weights.context))
        lam_home *= (1 - w_ctx) + w_ctx * context.home_multiplier
        lam_away *= (1 - w_ctx) + w_ctx * context.away_multiplier
        result.notes.append(f"context applied at weight {w_ctx:.2f}: {context.notes}")

    lam_home = _clamp(lam_home, config.lambda_floor / 2, config.lambda_ceiling)
    lam_away = _clamp(lam_away, config.lambda_floor / 2, config.lambda_ceiling)
    lam_total = _clamp(lam_home + lam_away, config.lambda_floor, config.lambda_ceiling)
    # Preserve the split after clamping the total.
    if lam_home + lam_away > 0:
        scale = lam_total / (lam_home + lam_away)
        lam_home, lam_away = lam_home * scale, lam_away * scale

    result.lambda_home = lam_home
    result.lambda_away = lam_away
    result.lambda_total = lam_total
    result.probability = probability_from_lambdas(lam_home, lam_away, config)
    result.n_effective = n_eff
    result.spread = _component_spread(available)
    result.confidence = _confidence(
        n_eff, len(team_specific), result.spread, result.data_completeness
    )
    if config.use_dixon_coles and config.dixon_coles_rho > 0:
        result.notes.append(f"Dixon-Coles correction applied (rho={config.dixon_coles_rho:.3f})")
    result.usable = True
    return result


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _effective_sample_size(stats: MatchStatistics) -> float:
    """How much evidence there really is about *this* pairing.

    The smaller of the two teams' form samples sets the ceiling - an estimate
    needs both sides - with partial credit for head-to-head meetings and for
    the home/away splits, which are subsets of the same matches.
    """
    form = min(stats.home_form.matches, stats.away_form.matches)
    splits = min(stats.home_home_split.matches, stats.away_away_split.matches)
    return float(form) + 0.5 * float(stats.h2h.meetings) + 0.25 * float(splits)


def _component_spread(available: dict[str, tuple[tuple[float, float], float]]) -> float | None:
    """Disagreement between components, as the range of their expected totals."""
    totals = [pair[0] + pair[1] for pair, _ in available.values()]
    if len(totals) < 2:
        return None
    return max(totals) - min(totals)


def _confidence(n_eff: float, components: int, spread: float | None, completeness: float) -> str:
    """High / Medium / Low from sample size and source agreement (spec 5.4)."""
    effective_spread = 0.0 if spread is None else spread
    if (
        n_eff >= CONFIDENCE_HIGH_MIN_SAMPLES
        and components >= CONFIDENCE_HIGH_MIN_COMPONENTS
        and effective_spread <= CONFIDENCE_HIGH_MAX_SPREAD
        and completeness >= CONFIDENCE_HIGH_MIN_COMPLETENESS
    ):
        return "High"
    if (
        n_eff >= CONFIDENCE_MEDIUM_MIN_SAMPLES
        and components >= CONFIDENCE_MEDIUM_MIN_COMPONENTS
        and effective_spread <= CONFIDENCE_MEDIUM_MAX_SPREAD
        and completeness >= CONFIDENCE_MEDIUM_MIN_COMPLETENESS
    ):
        return "Medium"
    return "Low"
