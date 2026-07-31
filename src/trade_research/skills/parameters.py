"""Typed parameter contracts for configurable research skills."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class TechnicalSkillParameters(BaseModel):
    window: int = Field(default=20, ge=2, le=252)


class WorthBuyStocksParameters(BaseModel):
    benchmark_symbols: str = Field(default="SPY,QQQ", min_length=1)

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
    threshold: float = Field(default=0.05, gt=0, le=1.0)
    min_train: int = Field(default=252, ge=50, le=2520)
    run_walkforward: bool = False


SKILL_PARAMETER_SCHEMAS: dict[str, dict[str, object]] = {
    "technical": TechnicalSkillParameters.model_json_schema(),
    "worth-buy-stocks": WorthBuyStocksParameters.model_json_schema(),
    "markov-method": MarkovMethodParameters.model_json_schema(),
}
