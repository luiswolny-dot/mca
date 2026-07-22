"""Configuration boundary.

This is the *only* place ``config/config.yaml`` is read. Everything downstream
— ``GateConfig``, ``ScorerConfig``, ``SizingConfig``, ``RiskLimits``,
``ExitConfig``, ``ExecutionConfig`` — is a plain dataclass that has never heard
of YAML, environment variables, or Pydantic, per the design decision at the top
of ``domain/models.py``. This module's whole job is to close that gap: parse
once, validate once, then hand every subsystem the dataclass it already expects.

Design decision — pure functions first, Pydantic second
----------------------------------------------------------
Loading YAML and substituting ``${VAR}`` references from the environment is
ordinary, dependency-light logic that has nothing to do with validation. It is
written and tested as plain functions operating on ``dict``. Pydantic is used
only for the last step (structural + range validation) so that a slim image
without ``pydantic`` installed can still exercise everything except the final
``AppConfig.model_validate`` call — which is exactly the boundary the rest of
this codebase already draws.

Design decision — one converter method per consumer, not one giant object
----------------------------------------------------------------------------
``AppConfig`` does not get handed wholesale to the scorer or the risk gate.
Each subsystem asks for exactly the dataclass it is typed to accept
(``to_gate_config()``, ``to_scorer_config()``, ...). This keeps every existing
module's public signature untouched — the integration point is entirely on the
config side, which is where it belongs.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from mca.domain.models import Chain

# --------------------------------------------------------------------------- #
# Pure functions — no optional dependency required, fully unit-tested
# --------------------------------------------------------------------------- #

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingEnvVars(Exception):
    """Raised once with *every* missing variable, not one at a time.

    Discovering required secrets one failed start-up at a time is a bad
    experience the second time it happens to the same operator. Collecting
    everything up front means a single restart surfaces the complete list.
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__(f"missing environment variables: {', '.join(sorted(set(names)))}")
        self.names = sorted(set(names))


def substitute_env(obj: Any, env: dict[str, str] | None = None, *,
                   strict: bool = True) -> Any:
    """Recursively replace ``${VAR}`` in strings with ``env[VAR]``.

    Walks dicts, lists, and tuples; non-string leaves pass through unchanged.
    With ``strict=True`` (the default, and the only sane default for a system
    that is about to load a private key) every missing variable is collected
    and raised together as :class:`MissingEnvVars` rather than substituting an
    empty string, which would silently turn ``rpc_http: ""`` into a listener
    that connects to nothing and never tells you why.
    """
    env = env if env is not None else os.environ
    missing: list[str] = []

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            def _sub(m: re.Match) -> str:
                name = m.group(1)
                if name not in env:
                    missing.append(name)
                    return ""
                return env[name]
            return _ENV_PATTERN.sub(_sub, node)
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, tuple):
            return tuple(_walk(v) for v in node)
        return node

    result = _walk(obj)
    if strict and missing:
        raise MissingEnvVars(missing)
    return result


def deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge; ``override`` wins on conflicting scalar keys.

    Used to layer an optional ``config.local.yaml`` (per-operator overrides,
    e.g. a lower ``max_notional_per_trade_usd`` while testing) on top of the
    checked-in ``config.yaml`` without duplicating the whole file.
    """
    out = copy.deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_yaml(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return data


def load_raw_config(path: str | Path, *, local_override: str | Path | None = None,
                    env: dict[str, str] | None = None) -> dict:
    """Load, optionally overlay a local file, substitute env vars. No validation."""
    data = load_yaml(path)
    if local_override is not None and Path(local_override).exists():
        data = deep_merge(data, load_yaml(local_override))
    return substitute_env(data, env)


# --------------------------------------------------------------------------- #
# Pydantic validation layer — optional dependency, guarded as a unit
# --------------------------------------------------------------------------- #

PYDANTIC_AVAILABLE = True
try:
    from pydantic import BaseModel, ConfigDict, Field, field_validator

    class _Loose(BaseModel):
        """Base for sections we validate loosely: known fields are typed,
        unknown fields (future config keys, chain-specific extras) pass
        through instead of raising, so a YAML edit never has to be paired
        with a code change just to add one more provider field."""

        model_config = ConfigDict(extra="allow")

    class SystemSection(_Loose):
        mode: str = "paper"
        timezone: str = "UTC"
        instance_name: str = "mca"

        @field_validator("mode")
        @classmethod
        def _mode_ok(cls, v: str) -> str:
            if v not in ("backtest", "paper", "live"):
                raise ValueError(f"system.mode must be backtest|paper|live, got {v!r}")
            return v

    class ChainSection(_Loose):
        enabled: bool = False
        rpc_http: str | None = None
        rpc_ws: str | None = None

    class DataSourceSection(_Loose):
        enabled: bool = False

    class DataSourcesSection(_Loose):
        min_confirming_sources: int = 2
        max_source_disagreement_pct: float = 0.08

    class WeightsSection(BaseModel):
        liquidity_depth: float
        volume_momentum: float
        holder_dynamics: float
        smart_money_flow: float
        technicals: float
        social: float
        safety_margin: float

    class GatesSection(BaseModel):
        min_liquidity_usd: float = 15_000
        max_liquidity_usd: float = 2_000_000
        min_token_age_s: int = 180
        max_token_age_s: int = 172_800
        require_mint_authority_revoked: bool = True
        require_freeze_authority_revoked: bool = True
        require_lp_locked_or_burned: bool = True
        min_lp_locked_pct: float = 0.90
        min_lp_lock_remaining_s: int = 86_400
        max_buy_tax_bps: int = 500
        max_sell_tax_bps: int = 500
        require_sell_simulation: bool = True
        min_holders: int = 75
        max_top1_holder_pct: float = 0.08
        max_top10_holder_pct: float = 0.30
        max_dev_holding_pct: float = 0.05
        max_bundled_supply_pct: float = 0.25
        max_insider_cluster_pct: float = 0.20
        max_deployer_prior_rugs: int = 0
        min_volume_5m_usd: float = 5_000
        min_unique_buyers_5m: int = 25
        max_volume_to_liquidity_ratio: float = 25.0
        allow_proxy_upgradeable: bool = False
        allow_blacklist_function: bool = False

    class ScoringSection(BaseModel):
        weights: WeightsSection
        min_score: float = 70.0
        min_win_probability: float = 0.42
        min_edge_bps: float = 250.0
        social_max_bot_ratio: float = 0.55

    class SizingSection(BaseModel):
        max_risk_per_trade: float = 0.0075
        max_notional_per_trade_usd: float = 750
        max_impact_bps: int = 120
        kelly_fraction: float = 0.25
        kelly_hard_cap: float = 0.02
        min_notional_usd: float = 25
        atr_stop_multiple: float = 2.2
        default_stop_pct: float = 0.25

    class RiskSection(BaseModel):
        max_open_positions: int = 5
        max_open_per_chain: int = 3
        max_gross_exposure_pct: float = 0.15
        max_daily_loss_pct: float = 0.06
        max_weekly_loss_pct: float = 0.12
        max_drawdown_pct: float = 0.20
        max_consecutive_losses: int = 6
        max_new_positions_per_hour: int = 12
        min_seconds_between_entries: int = 20
        max_exposure_single_token_pct: float = 0.04
        cooldown_after_loss_s: int = 300

    class ExitsSection(BaseModel):
        hard_stop_pct: float = -0.25
        break_even_trigger_pct: float = 0.30
        break_even_offset_pct: float = 0.02
        trailing_activate_pct: float = 0.45
        trailing_atr_multiple: float = 2.5
        trailing_min_pct: float = 0.18
        trailing_max_pct: float = 0.45
        time_stop_s: int = 2700
        time_stop_min_progress_pct: float = 0.20
        max_hold_s: int = 21_600
        liquidity_drop_exit_pct: float = 0.35
        momentum_fade_bars: int = 5
        social_fade_pct: float = 0.60
        whale_exit_net_usd: float = -15_000
        ladder: list[list[float]] = Field(default_factory=lambda: [
            [0.45, 0.30], [1.00, 0.25], [2.00, 0.20], [4.00, 0.15],
        ])

        @field_validator("ladder")
        @classmethod
        def _ladder_shape(cls, v: list[list[float]]) -> list[list[float]]:
            for rung in v:
                if len(rung) != 2:
                    raise ValueError(f"each ladder rung must be [trigger, fraction], got {rung}")
            return v

    class ExecutionSection(BaseModel):
        max_slippage_bps: int = 500
        tx_timeout_s: int = 45
        max_retries: int = 3
        retry_backoff_s: float = 0.8
        hot_wallet_max_usd: float = 2_000
        sweep_threshold_usd: float = 1_000
        cold_wallet_address: str | None = None
        min_shadow_days: int = 14
        min_shadow_trades: int = 100

    class MlSection(_Loose):
        enabled: bool = True
        label_threshold: float = 0.0
        purge_minutes: int = 360
        embargo_minutes: int = 720
        max_model_age_days: int = 21
        min_train_samples: int = 800
        min_auc: float = 0.56
        max_brier: float = 0.24
        retrain_cron: str = "0 3 * * *"

    class SecuritySection(_Loose):
        vault: str = "env"
        keystore_path: str = "/run/secrets/keystore"
        keystore_kdf: str = "scrypt"
        api_auth: str = "jwt"
        api_rate_limit_per_min: int = 120
        require_2fa_for_live_toggle: bool = True

    class ObservabilitySection(_Loose):
        log_level: str = "INFO"
        log_format: str = "json"
        prometheus_port: int = 9090
        watchdog_heartbeat_s: int = 15
        watchdog_missed_beats_before_halt: int = 4

    class AppConfig(BaseModel):
        model_config = ConfigDict(extra="allow")

        system: SystemSection = Field(default_factory=SystemSection)
        chains: dict[str, ChainSection] = Field(default_factory=dict)
        data_sources: DataSourcesSection = Field(default_factory=DataSourcesSection)
        gates: GatesSection = Field(default_factory=GatesSection)
        scoring: ScoringSection
        sizing: SizingSection = Field(default_factory=SizingSection)
        risk: RiskSection = Field(default_factory=RiskSection)
        exits: ExitsSection = Field(default_factory=ExitsSection)
        execution: ExecutionSection = Field(default_factory=ExecutionSection)
        ml: MlSection = Field(default_factory=MlSection)
        security: SecuritySection = Field(default_factory=SecuritySection)
        observability: ObservabilitySection = Field(default_factory=ObservabilitySection)

        # -- converters into the dataclasses the rest of the codebase expects --

        def to_gate_config(self):
            from mca.scoring.gates import GateConfig
            d = self.gates.model_dump()
            for k in ("min_liquidity_usd", "max_liquidity_usd", "min_volume_5m_usd"):
                d[k] = Decimal(str(d[k]))
            return GateConfig(**d)

        def to_scorer_config(self):
            from mca.scoring.scorer import ScorerConfig, ScoreWeights
            weights = ScoreWeights(**self.scoring.weights.model_dump())
            weights.validate()
            cfg = ScorerConfig(
                weights=weights, gates=self.to_gate_config(),
                min_score=self.scoring.min_score,
                min_win_probability=self.scoring.min_win_probability,
                min_edge_bps=self.scoring.min_edge_bps,
                social_max_bot_ratio=self.scoring.social_max_bot_ratio,
            )
            return cfg

        def to_sizing_config(self):
            from mca.risk.sizing import SizingConfig
            d = self.sizing.model_dump()
            d["max_notional_per_trade_usd"] = Decimal(str(d["max_notional_per_trade_usd"]))
            d["min_notional_usd"] = Decimal(str(d["min_notional_usd"]))
            return SizingConfig(**d)

        def to_risk_limits(self):
            from mca.risk.limits import RiskLimits
            return RiskLimits(**self.risk.model_dump())

        def to_exit_config(self):
            from mca.portfolio.exits import ExitConfig
            d = self.exits.model_dump()
            d["whale_exit_net_usd"] = Decimal(str(d["whale_exit_net_usd"]))
            d["ladder"] = [(float(t), float(f)) for t, f in d["ladder"]]
            return ExitConfig(**d)

        def to_execution_config(self, chain: Chain | None = None):
            """``chain`` overlays chain-specific execution knobs (priority fee,
            jito bundles) that live under ``chains.<name>`` rather than under
            ``execution:``, because they genuinely are per-chain, not global —
            an EVM executor has no notion of Solana priority-fee micro-lamports."""
            from mca.execution.base import ExecutionConfig
            d = self.execution.model_dump()
            d["hot_wallet_max_usd"] = Decimal(str(d["hot_wallet_max_usd"]))
            d["sweep_threshold_usd"] = Decimal(str(d["sweep_threshold_usd"]))

            extra: dict[str, Any] = {}
            if chain is not None:
                chain_cfg = self.chains.get(chain.value)
                if chain_cfg is not None:
                    raw = chain_cfg.model_dump()
                    if "priority_fee_micro_lamports" in raw and raw["priority_fee_micro_lamports"] is not None:
                        extra["priority_fee_micro_lamports"] = int(raw["priority_fee_micro_lamports"])
                    if "use_jito_bundles" in raw and raw["use_jito_bundles"] is not None:
                        extra["use_jito_bundles"] = bool(raw["use_jito_bundles"])
            return ExecutionConfig(**d, **extra)

        def to_ml_train_config(self):
            from mca.ml.calibration import TrainConfig
            m = self.ml
            return TrainConfig(
                label_threshold=m.label_threshold, purge_minutes=m.purge_minutes,
                embargo_minutes=m.embargo_minutes, max_model_age_days=m.max_model_age_days,
                min_train_samples=m.min_train_samples, min_auc=m.min_auc, max_brier=m.max_brier,
            )

except ImportError:
    PYDANTIC_AVAILABLE = False
    AppConfig = None  # type: ignore[assignment,misc]


def load_config(path: str | Path = "config/config.yaml", *,
                local_override: str | Path | None = "config/config.local.yaml",
                env: dict[str, str] | None = None) -> "AppConfig":
    """The one function most entry points call. Raises :class:`MissingEnvVars`
    with the complete list of unset secrets, or a Pydantic ``ValidationError``
    naming every structurally wrong field at once — both deliberately report
    everything in one pass rather than one problem per restart."""
    if not PYDANTIC_AVAILABLE:
        raise RuntimeError(
            "pydantic is not installed. Core dependencies are missing from this "
            "environment; install with `pip install -e .` (see pyproject.toml)."
        )
    raw = load_raw_config(path, local_override=local_override, env=env)
    return AppConfig.model_validate(raw)


@dataclass(slots=True, frozen=True)
class ConfigSummary:
    """What ``mca.ops.watchdog`` and the ``/health`` endpoint report — enough to
    confirm the right config loaded without printing secrets to a log line."""

    mode: str
    instance_name: str
    enabled_chains: tuple[str, ...]
    min_score: float
    max_open_positions: int
    strategy_hash: str


def summarize(cfg: "AppConfig") -> ConfigSummary:
    from mca.execution.base import strategy_fingerprint
    fp = strategy_fingerprint({
        "gates": cfg.gates.model_dump(),
        "scoring": cfg.scoring.model_dump(),
        "sizing": cfg.sizing.model_dump(),
        "risk": cfg.risk.model_dump(),
        "exits": cfg.exits.model_dump(),
    })
    enabled = tuple(sorted(name for name, c in cfg.chains.items() if c.enabled))
    return ConfigSummary(
        mode=cfg.system.mode, instance_name=cfg.system.instance_name,
        enabled_chains=enabled, min_score=cfg.scoring.min_score,
        max_open_positions=cfg.risk.max_open_positions, strategy_hash=fp,
    )
