"""Web app: the ring (UI) plus a small JSON API.

    uvicorn app.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import agent, data
from pattern.containment_referee import DeciderClient, DeciderUnavailable

app = FastAPI(title="No Low Blows")
STATIC = Path(__file__).parent / "static"
PENDING: dict[str, dict[str, Any]] = {}  # rulings held for a human, by id
DECISIONS: list[dict[str, Any]] = []


class FightRequest(BaseModel):
    alert_id: str | None = None
    custom_text: str | None = Field(default=None, max_length=1500)
    hothead: bool = False


class ApproveRequest(BaseModel):
    id: str
    approve: bool


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/alerts")
def alerts() -> list[dict[str, Any]]:
    return data.alert_summaries()


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        decider = DeciderClient().health()
    except DeciderUnavailable as exc:
        decider = {"error": str(exc)}
    return {"decider": decider, "bedrock_model": agent.MODEL_ID}


@app.post("/api/fight")
async def fight(req: FightRequest) -> dict[str, Any]:
    if not req.custom_text and req.alert_id not in data.ALERTS:
        raise HTTPException(400, "Pick an alert from the fight card or write your own.")
    result = await run_in_threadpool(agent.run_fight, req.alert_id, req.custom_text, req.hothead)
    for item in result["held"]:
        PENDING[item["id"]] = {**item, "alert_id": result["alert_id"]}
    return result


@app.get("/api/pending")
def pending() -> list[dict[str, Any]]:
    return list(PENDING.values())


@app.post("/api/approve")
def approve(req: ApproveRequest) -> dict[str, Any]:
    item = PENDING.pop(req.id, None)
    if not item:
        raise HTTPException(404, "That hold was already decided.")
    verb = {"lock_account": "locked account", "isolate_host": "isolated host", "block_ip": "blocked IP"}[item["tool"]]
    outcome = f"SIMULATED: {verb} {item['target']} (approved by a human)" if req.approve \
        else f"Rejected by a human: {item['tool']} on {item['target']} did not run"
    DECISIONS.append({"id": req.id, "approve": req.approve, "outcome": outcome})
    return {"outcome": outcome}
