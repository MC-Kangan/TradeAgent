"""Integration tests for skill parameter API endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from trade_research.application import ResearchApplication
from trade_research.engine import ResearchEngine
from trade_research.http import create_app
from trade_research.providers import ProviderRegistry
from trade_research.reporting import ReportStore
from trade_research.skills import (
    FilingsSkill,
    FundamentalSkill,
    MarkovMethodSkill,
    SkillRegistry,
    TechnicalSkill,
    WorthBuyStocksSkill,
)

BEARER = "test-token"


def _make_client(tmp_path: Path, providers=None):
    skills = SkillRegistry(
        (FundamentalSkill(), TechnicalSkill(), FilingsSkill(),
         WorthBuyStocksSkill(), MarkovMethodSkill())
    )
    engine = ResearchEngine(
        skills,
        providers or ProviderRegistry({}),
    )
    app = ResearchApplication(engine, ReportStore(tmp_path / "reports"))
    return TestClient(create_app(app, bearer_token=BEARER))


def _auth():
    return {"Authorization": f"Bearer {BEARER}"}


class TestSkillsEndpointParameters:
    def test_list_skills_includes_parameters(self, tmp_path):
        client = _make_client(tmp_path)
        resp = client.get("/skills", headers=_auth())
        assert resp.status_code == 200
        skills = resp.json()
        skill_map = {s["name"]: s for s in skills}

        assert "parameters" in skill_map["technical"]
        assert skill_map["technical"]["parameters"] is not None
        assert "window" in skill_map["technical"]["parameters"]["properties"]

        assert "parameters" in skill_map["worth-buy-stocks"]
        assert skill_map["worth-buy-stocks"]["parameters"] is not None

        assert "parameters" in skill_map["markov-method"]
        assert skill_map["markov-method"]["parameters"] is not None

    def test_fundamental_has_no_parameters(self, tmp_path):
        client = _make_client(tmp_path)
        resp = client.get("/skills", headers=_auth())
        assert resp.status_code == 200
        skills = resp.json()
        fundamental = next(s for s in skills if s["name"] == "fundamental")
        assert fundamental["parameters"] is None

    def test_describe_skill_includes_parameters(self, tmp_path):
        client = _make_client(tmp_path)
        resp = client.get("/skills/technical", headers=_auth())
        assert resp.status_code == 200
        skill = resp.json()
        assert skill["parameters"] is not None
        assert skill["parameters"]["properties"]["window"]["default"] == 20


class TestSkillRunParameters:
    def test_run_skill_with_valid_parameters_accepted(self, tmp_path):
        """A skill run with valid parameters should be accepted, though it may
        fail at the provider level if no price provider is configured."""
        client = _make_client(tmp_path)
        resp = client.post(
            "/skills/technical/run",
            json={
                "instrument": {"symbol": "AAPL", "market": "US"},
                "analysts": ["technical"],
                "skill_parameters": {"technical": {"window": 10}},
            },
            headers=_auth(),
        )
        # 503 means params were accepted but no price provider configured
        # 200 would mean params accepted AND price data available
        # 422 would mean parameter validation failed
        assert resp.status_code != 422, f"Should not get 422 for valid params: {resp.json()}"

    def test_run_skill_with_invalid_window_returns_422(self, tmp_path):
        client = _make_client(tmp_path)
        resp = client.post(
            "/skills/technical/run",
            json={
                "instrument": {"symbol": "AAPL", "market": "US"},
                "analysts": ["technical"],
                "skill_parameters": {"technical": {"window": 0}},
            },
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_run_skill_with_non_param_skill_and_params_returns_422(self, tmp_path):
        client = _make_client(tmp_path)
        resp = client.post(
            "/skills/fundamental/run",
            json={
                "instrument": {"symbol": "AAPL", "market": "US"},
                "analysts": ["fundamental"],
                "skill_parameters": {"fundamental": {"nonsense": True}},
            },
            headers=_auth(),
        )
        assert resp.status_code == 422

    def test_no_stack_trace_in_error_response(self, tmp_path):
        client = _make_client(tmp_path)
        resp = client.post(
            "/skills/technical/run",
            json={
                "instrument": {"symbol": "AAPL", "market": "US"},
                "analysts": ["technical"],
                "skill_parameters": {"technical": {"window": -999}},
            },
            headers=_auth(),
        )
        assert resp.status_code == 422
        body = resp.json()
        assert "Traceback" not in str(body)
        assert "stack" not in str(body).lower()
