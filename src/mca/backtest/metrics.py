"""Performance metrics.

Design decision — report the deflated Sharpe ratio, not just the Sharpe
-----------------------------------------------------------------------
If you try 300 parameter combinations and keep the best, its Sharpe ratio is a
biased estimate of out-of-sample performance — the maximum of 300 noisy draws is
high by construction. The Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
corrects for the number of trials and for skew/kurtosis in the returns.

For meme-coin strategies this is not academic pedantry. The return distribution
is violently right-skewed with high excess kurtosis, which inflates the plain
Sharpe by a factor that routinely exceeds 2x. A strategy showing Sharpe 3.1 in
an optimiser sweep and DSR 0.3 has not been validated; it has been overfit.

Design decision — trade-level *and* equity-level metrics
--------------------------------------------------------
Win rate and profit factor describe the trade distribution. Sharpe, Sortino, and
max drawdown describe the equity path. Both are needed: a strategy with a 78%
win rate and a single -60% trade looks excellent on the first set and is
uninvestable on the second.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

try:                                    # optional: not installed in slim workers
    from scipy import stats as _scipy_stats
except ImportError:                     # pragma: no cover
    _scipy_stats = None


@dataclass(slots=True)
class PerformanceReport:
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    payoff_ratio: float
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown_pct: float
    max_drawdown_duration_bars: int
    ulcer_index: float
    deflated_sharpe: float
    skew: float
    kurtosis: float
    largest_win_pct: float
    largest_loss_pct: float
    max_consecutive_losses: int
    exposure_pct: float
    turnover: float

    def to_dict(self) -> dict:
        return asdict(self)


def _safe(x: float, default: float = 0.0) -> float:
    return default if (x is None or math.isnan(x) or math.isinf(x)) else float(x)


def max_drawdown(equity: np.ndarray) -> tuple[float, int]:
    """Returns (max drawdown as a positive fraction, duration in bars)."""
    equity = np.asarray(equity, dtype=np.float64)
    if equity.size < 2:
        return 0.0, 0
    peaks = np.maximum.accumulate(equity)
    dd = (peaks - equity) / np.where(peaks == 0, 1, peaks)
    mdd = float(dd.max())

    end = int(dd.argmax())
    start = int(np.argmax(equity[:end + 1])) if end > 0 else 0
    recovery = end
    for i in range(end, equity.size):
        if equity[i] >= equity[start]:
            recovery = i
            break
    else:
        recovery = equity.size - 1
    return mdd, recovery - start


def ulcer_index(equity: np.ndarray) -> float:
    """RMS of the drawdown series. Penalises long shallow drawdowns that max-DD
    ignores — relevant here because the psychological failure mode is a slow
    bleed that tempts the operator into disabling the risk limits."""
    equity = np.asarray(equity, dtype=np.float64)
    if equity.size < 2:
        return 0.0
    peaks = np.maximum.accumulate(equity)
    dd = (peaks - equity) / np.where(peaks == 0, 1, peaks)
    return float(np.sqrt((dd ** 2).mean()))


def sharpe_ratio(returns: np.ndarray, periods_per_year: int = 365,
                 rf: float = 0.0) -> float:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return 0.0
    excess = r - rf / periods_per_year
    sd = excess.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(excess.mean() / sd * np.sqrt(periods_per_year))


def sortino_ratio(returns: np.ndarray, periods_per_year: int = 365,
                  target: float = 0.0) -> float:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return 0.0
    downside = np.clip(r - target, None, 0.0)
    dd = np.sqrt((downside ** 2).mean())
    if dd == 0:
        return 0.0
    return float((r.mean() - target) / dd * np.sqrt(periods_per_year))


def deflated_sharpe_ratio(observed_sharpe: float, n_returns: int, n_trials: int,
                          skew: float, kurtosis: float) -> float:
    """Probability that the true Sharpe exceeds zero, given ``n_trials`` searched.

    Implements Bailey & López de Prado (2014). ``kurtosis`` is the *raw* fourth
    moment ratio (3.0 for a normal distribution), not excess kurtosis.
    """
    if n_returns < 3 or n_trials < 1:
        return 0.0

    # Expected maximum Sharpe from n_trials independent draws of pure noise.
    euler = 0.5772156649015329
    if n_trials > 1:
        z1 = _norm_ppf(1.0 - 1.0 / n_trials)
        z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        sr0 = (1 - euler) * z1 + euler * z2
    else:
        sr0 = 0.0

    denom = 1.0 - skew * observed_sharpe + ((kurtosis - 1.0) / 4.0) * observed_sharpe ** 2
    if denom <= 0:
        return 0.0
    numer = (observed_sharpe - sr0) * math.sqrt(n_returns - 1)
    return float(_norm_cdf(numer / math.sqrt(denom)))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF. Uses scipy when present, else Acklam's rational
    approximation (max abs error ~1.15e-9), so the module has no hard scipy
    dependency in slim worker images."""
    if _scipy_stats is not None:
        return float(_scipy_stats.norm.ppf(p))
    if p <= 0 or p >= 1:
        return 0.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q, r = p - 0.5, (p - 0.5) ** 2
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def max_consecutive_losses(trade_returns: np.ndarray) -> int:
    run = best = 0
    for r in trade_returns:
        if r < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return int(best)


def _empty_report() -> PerformanceReport:
    """Explicit zero report. Returned when there is nothing to measure, so
    callers never have to distinguish 'no trades' from 'failed run'."""
    return PerformanceReport(
        n_trades=0, win_rate=0.0, profit_factor=0.0, expectancy_pct=0.0,
        avg_win_pct=0.0, avg_loss_pct=0.0, payoff_ratio=0.0,
        total_return_pct=0.0, cagr_pct=0.0, sharpe=0.0, sortino=0.0, calmar=0.0,
        max_drawdown_pct=0.0, max_drawdown_duration_bars=0, ulcer_index=0.0,
        deflated_sharpe=0.0, skew=0.0, kurtosis=3.0, largest_win_pct=0.0,
        largest_loss_pct=0.0, max_consecutive_losses=0, exposure_pct=0.0,
        turnover=0.0,
    )


def analyze(trade_returns, equity_curve, *, periods_per_year: int = 365,
            n_trials: int = 1, days: float | None = None,
            exposure_pct: float = 0.0, turnover: float = 0.0) -> PerformanceReport:
    """Build the full report.

    ``trade_returns``: per-trade fractional PnL (0.35 == +35%), net of all costs.
    ``equity_curve``:  account equity sampled on a fixed grid (daily by default).
    ``n_trials``:      how many parameter sets were evaluated to arrive here.
                       Pass the real number. Passing 1 after a 500-cell grid
                       search makes the deflated Sharpe meaningless.
    """
    tr = np.asarray(list(trade_returns), dtype=np.float64)
    eq = np.asarray(list(equity_curve), dtype=np.float64)

    if tr.size == 0 or eq.size < 2:
        return _empty_report()

    wins, losses = tr[tr > 0], tr[tr <= 0]
    gross_win, gross_loss = float(wins.sum()), float(abs(losses.sum()))

    period_returns = np.diff(eq) / eq[:-1]
    total_return = float(eq[-1] / eq[0] - 1.0)
    days = days if days is not None else max(len(eq) - 1, 1)
    years = max(days / 365.0, 1e-9)
    cagr = (eq[-1] / eq[0]) ** (1 / years) - 1.0 if eq[0] > 0 else 0.0

    sh = sharpe_ratio(period_returns, periods_per_year)
    mdd, mdd_dur = max_drawdown(eq)
    sk = float(_skew(tr))
    ku = float(_kurtosis_raw(tr))

    return PerformanceReport(
        n_trades=int(tr.size),
        win_rate=float((tr > 0).mean()),
        profit_factor=_safe(gross_win / gross_loss if gross_loss > 0 else float("inf"), 999.0),
        expectancy_pct=float(tr.mean()),
        avg_win_pct=float(wins.mean()) if wins.size else 0.0,
        avg_loss_pct=float(losses.mean()) if losses.size else 0.0,
        payoff_ratio=_safe(abs(float(wins.mean()) / float(losses.mean()))
                           if wins.size and losses.size and losses.mean() != 0 else 0.0),
        total_return_pct=total_return,
        cagr_pct=_safe(cagr),
        sharpe=_safe(sh),
        sortino=_safe(sortino_ratio(period_returns, periods_per_year)),
        calmar=_safe(cagr / mdd if mdd > 0 else 0.0),
        max_drawdown_pct=mdd,
        max_drawdown_duration_bars=mdd_dur,
        ulcer_index=ulcer_index(eq),
        deflated_sharpe=deflated_sharpe_ratio(sh, period_returns.size, n_trials, sk, ku),
        skew=sk,
        kurtosis=ku,
        largest_win_pct=float(tr.max()),
        largest_loss_pct=float(tr.min()),
        max_consecutive_losses=max_consecutive_losses(tr),
        exposure_pct=exposure_pct,
        turnover=turnover,
    )


def _skew(x: np.ndarray) -> float:
    if x.size < 3:
        return 0.0
    m, s = x.mean(), x.std(ddof=0)
    return 0.0 if s == 0 else float((((x - m) / s) ** 3).mean())


def _kurtosis_raw(x: np.ndarray) -> float:
    if x.size < 4:
        return 3.0
    m, s = x.mean(), x.std(ddof=0)
    return 3.0 if s == 0 else float((((x - m) / s) ** 4).mean())
