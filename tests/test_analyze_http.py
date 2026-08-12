from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from trade_research.http import create_app


class RecordingApplication:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def research(self, request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "instrument": request.instrument.model_dump(mode="json"),
            "results": [{"analyst": name, "status": "complete"} for name in request.analysts],
        }


def test_analyze_runs_all_selected_skills_in_one_application_call() -> None:
    application = RecordingApplication()
    client = TestClient(create_app(application, bearer_token="secret"))  # type: ignore[arg-type]

    response = client.post(
        "/analyze",
        headers={"Authorization": "Bearer secret"},
        json={
            "instrument": {"symbol": "AAPL", "market": "US"},
            "analysts": ["technical-basic", "risk-analysis"],
        },
    )

    assert response.status_code == 200
    assert [item["analyst"] for item in response.json()["results"]] == [
        "technical-basic",
        "risk-analysis",
    ]
    assert len(application.requests) == 1
