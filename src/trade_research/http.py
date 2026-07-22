"""Bearer-authenticated bounded HTTP adapter."""

from __future__ import annotations

import hmac
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query

from trade_research.application import ResearchApplication
from trade_research.domain import AnalysisRequest
from trade_research.queue import RequestConflict
from trade_research.reporting import ReportFormat


def create_app(application: ResearchApplication, *, bearer_token: str) -> FastAPI:
    """Create an API that never reads or persists client network identity."""

    if not bearer_token:
        raise ValueError("a non-empty bearer token is required")
    api = FastAPI(
        title="Trade Research",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, candidate = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(candidate, bearer_token):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    authenticated = [Depends(authorize)]

    @api.get("/skills", dependencies=authenticated)
    def list_skills() -> list[dict[str, Any]]:
        return application.list_skills()

    @api.get("/skills/{name}", dependencies=authenticated)
    def describe_skill(name: str) -> dict[str, Any]:
        try:
            return application.describe_skill(name)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown skill") from error

    @api.post("/skills/{name}/run", dependencies=authenticated)
    async def run_skill(name: str, request: AnalysisRequest) -> dict[str, Any]:
        try:
            return await application.run_skill(name, request)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="unknown skill") from error

    @api.post("/research", dependencies=authenticated, status_code=202)
    async def research(request: AnalysisRequest) -> dict[str, Any]:
        try:
            return await application.start_research(request)
        except RequestConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except KeyError as error:
            raise HTTPException(status_code=422, detail="unknown selected skill") from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail="research queue unavailable") from error

    @api.get("/research/{request_id}/status", dependencies=authenticated)
    def research_status(request_id: str) -> dict[str, Any]:
        status = application.get_research_status(request_id)
        if status["status"] == "not_found":
            raise HTTPException(status_code=404, detail="unknown research request")
        return status

    @api.get("/research/{request_id}/result", dependencies=authenticated)
    def research_result(request_id: str) -> dict[str, Any]:
        try:
            return application.get_research_result(request_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail="unknown research request") from error

    @api.post("/reports/{request_id}", dependencies=authenticated)
    def compile_report(
        request_id: str,
        format_name: Annotated[ReportFormat, Query(alias="format")] = ReportFormat.MARKDOWN,
    ) -> dict[str, str]:
        try:
            return {
                "format": format_name.value,
                "content": application.compile_report(request_id, format_name),
            }
        except KeyError as error:
            raise HTTPException(status_code=404, detail="report unavailable") from error

    @api.get("/reports", dependencies=authenticated)
    def list_reports() -> list[str]:
        return application.list_reports()

    @api.get("/reports/{request_id}", dependencies=authenticated)
    def get_report(request_id: str) -> dict[str, Any]:
        try:
            return application.get_report(request_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=404, detail="unknown report") from error

    return api


def run_server(
    application: ResearchApplication,
    *,
    bearer_token: str,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Run Uvicorn with request access logging disabled."""

    uvicorn.run(
        create_app(application, bearer_token=bearer_token),
        host=host,
        port=port,
        access_log=False,
    )
