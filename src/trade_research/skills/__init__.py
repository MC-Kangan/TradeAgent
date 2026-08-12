"""Built-in immutable analyst skills and their composition helpers."""

from trade_research.skills.core import (
    FilingsSkill,
    FundamentalSkill,
    ResearchCompiler,
    ResearchReviewer,
    ResearchSkill,
    SkillRegistry,
    TechnicalSkill,
)
from trade_research.skills.markov_method import MarkovMethodSkill
from trade_research.skills.parameters import (
    SKILL_PARAMETER_SCHEMAS,
    AssetAllocationParameters,
    MarkovMethodParameters,
    PortfolioSkillParameters,
    TechnicalSkillParameters,
    WorthBuyStocksParameters,
)
from trade_research.skills.portfolio import AssetAllocationSkill, CorrelationAnalysisSkill
from trade_research.skills.price_series import (
    RiskAnalysisSkill,
    TechnicalBasicSkill,
    VolatilityRegimeSkill,
)
from trade_research.skills.worth_buy_stocks import WorthBuyStocksSkill

__all__ = [
    "FilingsSkill",
    "AssetAllocationParameters",
    "AssetAllocationSkill",
    "CorrelationAnalysisSkill",
    "FundamentalSkill",
    "MarkovMethodParameters",
    "MarkovMethodSkill",
    "PortfolioSkillParameters",
    "ResearchCompiler",
    "ResearchReviewer",
    "ResearchSkill",
    "RiskAnalysisSkill",
    "SKILL_PARAMETER_SCHEMAS",
    "SkillRegistry",
    "TechnicalSkill",
    "TechnicalBasicSkill",
    "TechnicalSkillParameters",
    "WorthBuyStocksParameters",
    "WorthBuyStocksSkill",
    "VolatilityRegimeSkill",
]
