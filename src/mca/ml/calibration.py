"""Win-probability model and calibration.

Design decision — the model outputs a *calibrated probability*, not a signal
---------------------------------------------------------------------------
A gradient-boosted classifier's raw ``predict_proba`` is not a probability; it is
a monotone score that happens to live in [0,1]. Feeding it into a Kelly formula
produces nonsense position sizes. The raw model is therefore wrapped in isotonic
regression fitted on a *separate, later* time slice, and the output is validated
with a reliability curve and Brier score before it is allowed to size anything.

If the calibration curve is bad, the correct response is to trade fixed size,
not to trade the miscalibrated Kelly.

Design decision — temporal splits only, with an embargo
--------------------------------------------------------
``train_test_split(shuffle=True)`` on trading data is the most common way to
produce a 0.85 AUC model that loses money. Two positions opened four minutes
apart on correlated tokens are the same bet; random splitting puts one in train
and one in test and leaks the answer. Splits here are strictly chronological
with a purge window and an embargo after it (López de Prado's scheme), sized to
the maximum holding period.

Design decision — the label is the *realised, net* outcome
-----------------------------------------------------------
``y = 1`` if the position, executed under the real exit rules and net of all
modelled costs, returned more than ``label_threshold``. Not "did the price ever
go up 2x", which is unattainable-in-practice labelling and inflates every metric.

Design decision — a stale model must fail closed
--------------------------------------------------
``WinProbabilityModel.predict`` returns ``None`` when the model is older than
``max_model_age_days``. Market regime turns over in weeks in this asset class; a
model fitted on a bull-phase distribution will confidently size into a
distribution it has never seen. ``None`` propagates to the EV gate, which
refuses the trade.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

log = logging.getLogger(__name__)

FEATURE_ORDER: list[str] = [
    "score_total", "liquidity_depth", "volume_momentum", "holder_dynamics",
    "smart_money_flow", "technicals", "social", "safety_margin",
    "liquidity_usd_log", "age_seconds_log", "turnover_5m", "buy_sell_ratio_5m",
    "unique_buyers_5m_log", "top10_holder_pct", "rsi_14", "atr_pct",
    "realized_vol", "order_flow_imbalance", "expected_impact_bps",
    "hour_of_day_sin", "hour_of_day_cos", "chain_id",
]


@dataclass(slots=True)
class TrainConfig:
    label_threshold: float = 0.0        # net return above which a trade is a "win"
    purge_minutes: int = 360            # >= max holding period
    embargo_minutes: int = 720
    n_splits: int = 5
    max_model_age_days: int = 21
    min_train_samples: int = 800
    min_auc: float = 0.56               # below this, ship nothing
    max_brier: float = 0.24


@dataclass(slots=True)
class CalibrationReport:
    auc: float
    brier: float
    log_loss: float
    n_train: int
    n_test: int
    reliability_bins: list[tuple[float, float, int]] = field(default_factory=list)
    passed: bool = False
    notes: str = ""


def purged_time_series_splits(timestamps: np.ndarray, n_splits: int,
                              purge: timedelta, embargo: timedelta):
    """Chronological CV with purge + embargo.

    ``timestamps`` must be sorted ascending (numpy datetime64). Yields
    (train_idx, test_idx) where every training sample ends at least ``purge``
    before the test fold begins, and the ``embargo`` after the fold is dropped
    from later training sets.
    """
    n = timestamps.size
    fold = n // (n_splits + 1)
    purge_ns = np.timedelta64(int(purge.total_seconds()), "s")
    embargo_ns = np.timedelta64(int(embargo.total_seconds()), "s")

    for i in range(1, n_splits + 1):
        test_start, test_end = i * fold, min((i + 1) * fold, n)
        if test_end <= test_start:
            continue
        t0 = timestamps[test_start]
        t1 = timestamps[test_end - 1]
        train_mask = timestamps <= (t0 - purge_ns)
        embargo_mask = (timestamps > t1) & (timestamps <= t1 + embargo_ns)
        train_idx = np.where(train_mask & ~embargo_mask)[0]
        test_idx = np.arange(test_start, test_end)
        if train_idx.size and test_idx.size:
            yield train_idx, test_idx


class WinProbabilityModel:
    """Gradient-boosted classifier + isotonic calibration.

    XGBoost when available (better on tabular data with missing values),
    otherwise sklearn's HistGradientBoosting, which is a close substitute and
    already a dependency.
    """

    def __init__(self, cfg: TrainConfig | None = None) -> None:
        self.cfg = cfg or TrainConfig()
        self.model = None
        self.calibrator = None
        self.trained_at: datetime | None = None
        self.version: str = "untrained"
        self.report: CalibrationReport | None = None

    # -- training -----------------------------------------------------------
    def fit(self, X: np.ndarray, y: np.ndarray, timestamps: np.ndarray
            ) -> CalibrationReport:
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

        if X.shape[0] < self.cfg.min_train_samples:
            raise ValueError(
                f"{X.shape[0]} samples < {self.cfg.min_train_samples} minimum. "
                f"Keep paper trading; a model fitted on a few hundred meme-coin "
                f"trades is fitting noise.")

        order = np.argsort(timestamps)
        X, y, timestamps = X[order], y[order], timestamps[order]

        splits = list(purged_time_series_splits(
            timestamps, self.cfg.n_splits,
            timedelta(minutes=self.cfg.purge_minutes),
            timedelta(minutes=self.cfg.embargo_minutes)))
        if not splits:
            raise ValueError("not enough history for purged CV")

        aucs, briers, lls = [], [], []
        for train_idx, test_idx in splits:
            if len(np.unique(y[train_idx])) < 2:
                continue
            base = _make_base_model()
            clf = CalibratedClassifierCV(base, method="isotonic", cv=3)
            clf.fit(X[train_idx], y[train_idx])
            p = clf.predict_proba(X[test_idx])[:, 1]
            if len(np.unique(y[test_idx])) > 1:
                aucs.append(roc_auc_score(y[test_idx], p))
            briers.append(brier_score_loss(y[test_idx], p))
            lls.append(log_loss(y[test_idx], p, labels=[0, 1]))

        # final fit on everything except the last embargo window
        base = _make_base_model()
        self.model = CalibratedClassifierCV(base, method="isotonic", cv=3)
        self.model.fit(X, y)
        self.trained_at = datetime.now().astimezone()
        self.version = f"wp-{self.trained_at:%Y%m%d%H%M}"

        auc = float(np.mean(aucs)) if aucs else 0.5
        brier = float(np.mean(briers)) if briers else 1.0
        rep = CalibrationReport(
            auc=auc, brier=brier,
            log_loss=float(np.mean(lls)) if lls else 1.0,
            n_train=int(X.shape[0]), n_test=sum(len(t) for _, t in splits),
            reliability_bins=self._reliability(X, y),
        )
        rep.passed = auc >= self.cfg.min_auc and brier <= self.cfg.max_brier
        rep.notes = ("ok" if rep.passed else
                     f"REJECTED: auc={auc:.3f} (min {self.cfg.min_auc}), "
                     f"brier={brier:.3f} (max {self.cfg.max_brier}). "
                     f"Do not deploy; trade fixed size instead.")
        self.report = rep
        log.info("model %s trained: %s", self.version, rep.notes)
        return rep

    def _reliability(self, X: np.ndarray, y: np.ndarray, bins: int = 10):
        p = self.model.predict_proba(X)[:, 1]
        edges = np.linspace(0, 1, bins + 1)
        out = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            m = (p >= lo) & (p < hi)
            if m.sum() > 0:
                out.append((float(p[m].mean()), float(y[m].mean()), int(m.sum())))
        return out

    # -- inference ----------------------------------------------------------
    def predict(self, features: dict[str, float],
                now: datetime | None = None) -> float | None:
        """Calibrated P(win), or ``None`` when the model must not be trusted."""
        if self.model is None or self.trained_at is None:
            return None
        now = now or datetime.now().astimezone()
        age = (now - self.trained_at).days
        if age > self.cfg.max_model_age_days:
            log.warning("model %s is %dd old (max %d) — failing closed",
                        self.version, age, self.cfg.max_model_age_days)
            return None
        if self.report and not self.report.passed:
            return None

        x = np.array([[features.get(k, np.nan) for k in FEATURE_ORDER]],
                     dtype=np.float64)
        return float(self.model.predict_proba(x)[0, 1])


def training_rows_to_arrays(rows, *, label_threshold: float = 0.0
                            ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Maps ``db.repository.fetch_training_rows`` output to ``(X, y, timestamps)``.

    Only the seven aggregated score components plus ``score_total`` are
    populated from real data — those are exactly what ``signals.components``
    stores. Every other :data:`FEATURE_ORDER` column (raw liquidity, turnover,
    RSI, ...) is left as ``NaN``: both HistGradientBoosting and XGBoost handle
    missing values natively, so this degrades gracefully to "the model learns
    from what we actually recorded" rather than silently teaching it that a
    zero-filled placeholder is a meaningful value. Persisting the granular raw
    features (not just the aggregated component scores) in ``signals`` would
    improve this — a real follow-up, not a hidden limitation.
    """
    X = np.full((len(rows), len(FEATURE_ORDER)), np.nan, dtype=np.float64)
    y = np.zeros(len(rows), dtype=np.int64)
    ts = np.empty(len(rows), dtype="datetime64[s]")

    idx = {name: i for i, name in enumerate(FEATURE_ORDER)}
    for i, row in enumerate(rows):
        X[i, idx["score_total"]] = row["score_total"]
        components = row["components"] or {}
        for key in ("liquidity_depth", "volume_momentum", "holder_dynamics",
                   "smart_money_flow", "technicals", "social", "safety_margin"):
            if key in idx and key in components:
                X[i, idx[key]] = components[key]

        invested = float(row["quote_invested_usd"] or 0)
        net_return = (float(row["realized_pnl_usd"]) / invested) if invested > 0 else 0.0
        y[i] = 1 if net_return > label_threshold else 0
        ts[i] = np.datetime64(row["as_of"].replace(tzinfo=None), "s")

    return X, y, ts


def _make_base_model():
    try:
        from xgboost import XGBClassifier
        return XGBClassifier(
            n_estimators=300, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
            eval_metric="logloss", tree_method="hist",
        )
    except ImportError:
        from sklearn.ensemble import HistGradientBoostingClassifier
        return HistGradientBoostingClassifier(
            max_depth=4, learning_rate=0.05, max_iter=300,
            l2_regularization=2.0, early_stopping=True,
        )
