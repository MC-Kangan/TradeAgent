"""Typed parameter contracts for configurable research skills."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from trade_research.domain import InstrumentId
from trade_research.skills.core import ResearchSkill

PORTFOLIO_SKILLS = frozenset({"correlation-analysis", "asset-allocation"})


class TechnicalSkillParameters(BaseModel):
    window: int = Field(default=20, ge=2, le=252)


class WorthBuyStocksParameters(BaseModel):
    benchmark_symbols: str = Field(default="AUTO", min_length=1)

    @field_validator("benchmark_symbols")
    @classmethod
    def _normalise_symbols(cls, v: str) -> str:
        symbols = [s.strip().upper() for s in v.split(",") if s.strip()]
        if not symbols:
            raise ValueError("benchmark_symbols must contain at least one symbol")
        return ",".join(symbols)

    def as_tuple(self) -> tuple[str, ...]:
        return tuple(self.benchmark_symbols.split(","))


class MarkovMethodParameters(BaseModel):
    window: int = Field(default=20, ge=2, le=252)
    # Kept for native/HTTP compatibility. New callers should use the two
    # independent thresholds below.
    threshold: float = Field(default=0.05, gt=0, le=1.0)
    bull_threshold: float = Field(default=0.05, gt=0, le=1.0)
    bear_threshold: float = Field(default=-0.05, ge=-1.0, lt=0)
    min_train: int = Field(default=252, ge=50, le=2520)
    run_walkforward: bool = False


class PortfolioSkillParameters(BaseModel):
    lookback: int = Field(default=120, ge=20, le=252)


class AssetAllocationParameters(PortfolioSkillParameters):
    method: Literal["equal_weight", "inverse_volatility", "risk_parity", "max_diversification"] = (
        "risk_parity"
    )


def configure_skill(
    skill: ResearchSkill,
    params: Mapping[str, object],
    *,
    portfolio_instruments: tuple[InstrumentId, ...] = (),
) -> ResearchSkill:
    """Return an immutable per-run skill copy with validated parameters."""
    if skill.name == "technical":
        technical_params = TechnicalSkillParameters.model_validate(params)
        return replace(skill, window=technical_params.window)  # type: ignore[type-var]

    if skill.name == "worth-buy-stocks":
        worth_buy_params = WorthBuyStocksParameters.model_validate(params)
        return replace(  # type: ignore[type-var]
            skill,
            benchmark_symbols=worth_buy_params.as_tuple(),
        )

    if skill.name == "markov-method":
        markov_params = MarkovMethodParameters.model_validate(params)
        bull_threshold = markov_params.bull_threshold
        bear_threshold = markov_params.bear_threshold
        if "bull_threshold" not in params and "bear_threshold" not in params:
            bull_threshold = markov_params.threshold
            bear_threshold = -markov_params.threshold
        return replace(  # type: ignore[type-var]
            skill,
            window=markov_params.window,
            threshold=markov_params.threshold,
            bull_threshold=bull_threshold,
            bear_threshold=bear_threshold,
            min_train=markov_params.min_train,
            run_walkforward=markov_params.run_walkforward,
        )

    if skill.name == "correlation-analysis":
        portfolio_params = PortfolioSkillParameters.model_validate(params)
        if not 2 <= len(portfolio_instruments) <= 9:
            raise ValueError("correlation-analysis requires a portfolio-scoped request")
        return replace(  # type: ignore[type-var]
            skill,
            instruments=portfolio_instruments,
            lookback=portfolio_params.lookback,
        )

    if skill.name == "asset-allocation":
        allocation_params = AssetAllocationParameters.model_validate(params)
        if not 2 <= len(portfolio_instruments) <= 9:
            raise ValueError("asset-allocation requires a portfolio-scoped request")
        return replace(  # type: ignore[type-var]
            skill,
            instruments=portfolio_instruments,
            lookback=allocation_params.lookback,
            method=allocation_params.method,
        )

    if params:
        raise ValueError(f"Skill '{skill.name}' does not accept parameters")
    return skill


SKILL_PARAMETER_SCHEMAS: dict[str, dict[str, object]] = {
    "technical": TechnicalSkillParameters.model_json_schema(),
    "worth-buy-stocks": WorthBuyStocksParameters.model_json_schema(),
    "markov-method": MarkovMethodParameters.model_json_schema(),
    "correlation-analysis": PortfolioSkillParameters.model_json_schema(),
    "asset-allocation": AssetAllocationParameters.model_json_schema(),
}
