"""
Central configuration layer for NEURAVEX.

Every risk/trading/execution parameter that matters lives here, sourced from
environment variables and an optional YAML override (config/neuravex.yaml).
Nothing else in the codebase should hardcode a risk limit.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

try:
    import yaml
except ImportError:  # e.g. Python 3.15, for which PyYAML has no wheel yet
    yaml = None
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

# Load .env into the process environment as early as possible. This module
# is imported by every entry point (uvicorn's main.py, standalone scripts,
# Celery tasks), so loading it here — rather than repeating this call in
# each entry point — guarantees .env values are available everywhere
# os.getenv() is used, including in backend/app/db/session.py.
#
# override=False (the default) means real OS/shell environment variables
# always win over .env — this matters for Docker/production deployments
# where secrets are injected via the environment, not a checked-in file.
_env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(dotenv_path=_env_path)

TradingMode = Literal["backtest", "paper", "live"]


class RiskConfig(BaseModel):
    max_trade_risk: float = 0.01
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.10
    max_open_positions: int = 5
    max_leverage: float = 1.0


class StrategyConfig(BaseModel):
    minimum_confidence: float = 0.70
    minimum_expected_edge: float = 0.003
    cooldown_seconds: int = 900
    max_trades_per_hour: int = 4
    min_time_between_entries_seconds: int = 300


class ExecutionConfig(BaseModel):
    slippage_limit: float = 0.005


class TradingConfig(BaseModel):
    mode: TradingMode = "paper"


class NeuravexSettings(BaseModel):
    env: str = "development"
    allow_live: bool = False
    trading: TradingConfig = Field(default_factory=TradingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)

    @field_validator("allow_live", mode="before")
    @classmethod
    def _parse_bool(cls, v):
        if isinstance(v, str):
            return v.strip().lower() == "true"
        return v


def _load_yaml_overrides() -> dict:
    yaml_path = Path(__file__).resolve().parents[3] / "config" / "neuravex.yaml"
    if not yaml_path.exists():
        return {}
    with open(yaml_path) as f:
        text = f.read()
    if yaml is not None:
        return yaml.safe_load(text) or {}
    return _parse_simple_yaml(text)


def _parse_scalar(raw: str):
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~", ""):
        return None
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def _parse_simple_yaml(text: str) -> dict:
    """Fallback for when PyYAML isn't installed: reads the subset that
    config/neuravex.yaml uses — nested "key: value" mappings by
    indentation, "# comments" and plain scalars. Anything else raises
    rather than being silently misread."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for lineno, line in enumerate(text.splitlines(), 1):
        content = line.split(" #", 1)[0].rstrip()
        if not content.strip() or content.lstrip().startswith("#"):
            continue
        indent = len(content) - len(content.lstrip(" "))
        key, sep, value = content.strip().partition(":")
        if not sep or not key or key.startswith("- "):
            raise ValueError(f"neuravex.yaml line {lineno}: unsupported without PyYAML: {line!r}")
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        value = value.strip()
        if value:
            parent[key] = _parse_scalar(value)
        else:
            parent[key] = {}
            stack.append((indent, parent[key]))
    return root


def load_settings() -> NeuravexSettings:
    overrides = _load_yaml_overrides()
    base = {
        "env": os.getenv("NEURAVEX_ENV", "development"),
        "allow_live": os.getenv("NEURAVEX_ALLOW_LIVE", "false"),
    }
    merged = {**base, **overrides}
    return NeuravexSettings(**merged)


settings = load_settings()
