"""Unit tests for skill parameter models and configure_skill()."""

import pytest
from pydantic import ValidationError

from trade_research.application import configure_skill
from trade_research.skills import (
    FundamentalSkill,
    MarkovMethodSkill,
    TechnicalSkill,
    WorthBuyStocksSkill,
)
from trade_research.skills.parameters import (
    SKILL_PARAMETER_SCHEMAS,
    MarkovMethodParameters,
    TechnicalSkillParameters,
    WorthBuyStocksParameters,
)


class TestTechnicalSkillParameters:
    def test_defaults(self):
        p = TechnicalSkillParameters()
        assert p.window == 20

    def test_valid_custom_window(self):
        p = TechnicalSkillParameters(window=10)
        assert p.window == 10

    def test_window_min_boundary(self):
        p = TechnicalSkillParameters(window=2)
        assert p.window == 2

    def test_window_max_boundary(self):
        p = TechnicalSkillParameters(window=252)
        assert p.window == 252

    def test_window_below_min_raises(self):
        with pytest.raises(ValidationError):
            TechnicalSkillParameters(window=1)

    def test_window_above_max_raises(self):
        with pytest.raises(ValidationError):
            TechnicalSkillParameters(window=300)


class TestWorthBuyStocksParameters:
    def test_defaults(self):
        p = WorthBuyStocksParameters()
        assert p.benchmark_symbols == "AUTO"
        assert p.as_tuple() == ("AUTO",)

    def test_single_symbol(self):
        p = WorthBuyStocksParameters(benchmark_symbols="SPY")
        assert p.as_tuple() == ("SPY",)

    def test_trims_and_uppercases(self):
        p = WorthBuyStocksParameters(benchmark_symbols=" spy , qqq , IWM ")
        assert p.as_tuple() == ("SPY", "QQQ", "IWM")

    def test_empty_raises(self):
        with pytest.raises(ValidationError):
            WorthBuyStocksParameters(benchmark_symbols="")


class TestMarkovMethodParameters:
    def test_defaults(self):
        p = MarkovMethodParameters()
        assert p.window == 20
        assert p.threshold == 0.05
        assert p.min_train == 252
        assert p.run_walkforward is False

    def test_custom_all(self):
        p = MarkovMethodParameters(window=10, threshold=0.1, min_train=500, run_walkforward=True)
        assert p.window == 10
        assert p.threshold == 0.1
        assert p.min_train == 500
        assert p.run_walkforward is True

    def test_threshold_zero_raises(self):
        with pytest.raises(ValidationError):
            MarkovMethodParameters(threshold=0)

    def test_threshold_above_one_raises(self):
        with pytest.raises(ValidationError):
            MarkovMethodParameters(threshold=1.5)

    def test_min_train_below_min_raises(self):
        with pytest.raises(ValidationError):
            MarkovMethodParameters(min_train=10)


class TestSkillParameterSchemas:
    def test_schemas_present_for_three_skills(self):
        assert "technical" in SKILL_PARAMETER_SCHEMAS
        assert "worth-buy-stocks" in SKILL_PARAMETER_SCHEMAS
        assert "markov-method" in SKILL_PARAMETER_SCHEMAS

    def test_technical_schema_has_window_property(self):
        schema = SKILL_PARAMETER_SCHEMAS["technical"]
        assert "window" in schema["properties"]
        assert schema["properties"]["window"]["type"] == "integer"
        assert schema["properties"]["window"]["default"] == 20
        assert schema["properties"]["window"]["minimum"] == 2
        assert schema["properties"]["window"]["maximum"] == 252

    def test_markov_schema_has_all_properties(self):
        schema = SKILL_PARAMETER_SCHEMAS["markov-method"]
        props = schema["properties"]
        assert "window" in props
        assert "threshold" in props
        assert "min_train" in props
        assert "run_walkforward" in props
        assert props["run_walkforward"]["type"] == "boolean"


class TestConfigureSkill:
    def test_technical_window_applied(self):
        base = TechnicalSkill()
        configured = configure_skill(base, {"window": 10})
        assert configured.window == 10
        assert base.window == 20  # original unchanged
        assert configured.name == "technical"

    def test_worth_buy_benchmarks_applied(self):
        base = WorthBuyStocksSkill()
        configured = configure_skill(base, {"benchmark_symbols": "IWM,QQQ"})
        assert configured.benchmark_symbols == ("IWM", "QQQ")
        assert base.benchmark_symbols == ("AUTO",)

    def test_markov_all_params_applied(self):
        base = MarkovMethodSkill()
        configured = configure_skill(
            base, {"window": 10, "threshold": 0.1, "min_train": 500, "run_walkforward": True}
        )
        assert configured.window == 10
        assert configured.threshold == 0.1
        assert configured.min_train == 500
        assert configured.run_walkforward is True

    def test_non_param_skill_with_params_raises_valueerror(self):
        with pytest.raises(ValueError, match="does not accept parameters"):
            configure_skill(FundamentalSkill(), {"window": 5})

    def test_non_param_skill_empty_params_returns_unchanged(self):
        base = FundamentalSkill()
        result = configure_skill(base, {})
        assert result is base

    def test_invalid_window_type_raises_validationerror(self):
        with pytest.raises(ValidationError):
            configure_skill(TechnicalSkill(), {"window": "not_a_number"})
