"""Web app: the ring (UI) plus a small JSON API.

    uvicorn app.server:app --host 0.0.0.0 --port 8080

Every ruling, approval, rejection, undo and "bad call" is appended to an audit log (JSON lines,
AUDIT_PATH, default ./audit.jsonl). "Bad call" feedback is also saved as a labelled benchmark
case (FEEDBACK_PATH, default ./bench/feedback_cases.jsonl) so corrections improve the test set.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

import statistics
import time

from app import agent, data
from bench import run_bench
from pattern.containment_referee import ContainmentReferee, DeciderClient, DeciderUnavailable

app = FastAPI(title="AntiSOCial")
STATIC = Path(__file__).parent / "static"
ROOT = Path(__file__).parent.parent
AUDIT_PATH = Path(os.environ.get("AUDIT_PATH", ROOT / "audit.jsonl"))
FEEDBACK_PATH = Path(os.environ.get("FEEDBACK_PATH", ROOT / "bench" / "feedback_cases.jsonl"))

PENDING: dict[str, dict[str, Any]] = {}  # held rulings waiting for a human, by ruling id
RULINGS: dict[str, dict[str, Any]] = {}  # every ruling this session, by id (for feedback)
LANDED: dict[str, dict[str, Any]] = {}  # actions that ran, by id (for undo)
_write_lock = threading.Lock()


def _append(path: Path, record: dict[str, Any]) -> None:
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def audit(event: str, **fields: Any) -> None:
    _append(AUDIT_PATH, {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields})


class FightRequest(BaseModel):
    alert_id: str | None = None
    custom_text: str | None = Field(default=None, max_length=1500)
    hothead: bool = False


class ApproveRequest(BaseModel):
    id: str
    approve: bool


class UndoRequest(BaseModel):
    id: str


class FeedbackRequest(BaseModel):
    ruling_id: str
    correct: Literal["allow", "block", "review"]
    note: str = Field(default="", max_length=500)


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
    alert_id, evidence = result["alert_id"], result.get("evidence", "")
    audit("fight", alert_id=alert_id, hothead=req.hothead, model=result.get("model"), error=result.get("error"))
    for event in result.get("events", []):
        if event.get("kind") == "ruling":
            RULINGS[event["id"]] = {"ruling": event, "alert_id": alert_id, "evidence": evidence}
            audit("ruling", alert_id=alert_id, **{k: v for k, v in event.items() if k != "kind"})
    for item in result.get("held", []):
        PENDING[item["id"]] = {**item, "alert_id": alert_id}
    for item in result.get("landed", []):
        LANDED[item["id"]] = {**item, "alert_id": alert_id, "undone": False}
        audit("landed", alert_id=alert_id, action_id=item["id"], what=item["what"])
    return result


@app.get("/api/pending")
def pending() -> list[dict[str, Any]]:
    return list(PENDING.values())


@app.get("/api/landed")
def landed() -> list[dict[str, Any]]:
    return list(LANDED.values())


@app.post("/api/approve")
def approve(req: ApproveRequest) -> dict[str, Any]:
    item = PENDING.pop(req.id, None)
    if not item:
        raise HTTPException(404, "That hold was already decided.")
    if req.approve:
        what = f"{agent.DONE[item['tool']]} {item['target']}"
        LANDED[req.id] = {"id": req.id, "tool": item["tool"], "target": item["target"], "what": what,
                          "undo": f"{agent.UNDO[item['tool']]} {item['target']}", "alert_id": item["alert_id"],
                          "undone": False}
        outcome = f"SIMULATED: {what} (approved by a human)"
    else:
        outcome = f"Rejected by a human: {item['tool']} on {item['target']} did not run"
    audit("approve" if req.approve else "reject", ruling_id=req.id, alert_id=item["alert_id"],
          tool=item["tool"], target=item["target"], referee_call=item["call"])
    return {"outcome": outcome}


@app.post("/api/undo")
def undo(req: UndoRequest) -> dict[str, Any]:
    item = LANDED.get(req.id)
    if not item or item["undone"]:
        raise HTTPException(404, "Nothing to undo.")
    item["undone"] = True
    audit("undo", action_id=req.id, alert_id=item["alert_id"], what=item["undo"])
    return {"outcome": f"SIMULATED: {item['undo']} (undone by a human)"}


@app.post("/api/feedback")
def feedback(req: FeedbackRequest) -> dict[str, Any]:
    entry = RULINGS.get(req.ruling_id)
    if not entry:
        raise HTTPException(404, "Unknown ruling.")
    r = entry["ruling"]
    case = {"id": f"F-{req.ruling_id}", "alert": entry["alert_id"], "evidence": entry["evidence"],
            "tool": r["tool"], "args": {agent.TARGETS[r["tool"]]: r["target"]}, "expected": req.correct,
            "note": req.note or "analyst feedback", "referee_call": r["call"]}
    _append(FEEDBACK_PATH, case)
    audit("feedback", ruling_id=req.ruling_id, referee_call=r["call"], correct=req.correct, note=req.note)
    return {"saved": case["id"], "message": "Instant replay saved: this case is now in the benchmark set."}


def run_swarm() -> dict[str, Any]:
    """Swarm drill: every benchmark case fired at once, judged by Decider alone (no LLM in the loop)."""
    cases = json.loads((ROOT / "bench" / "cases.json").read_text())
    decider = DeciderClient()
    rows = []
    with agent._LOCK:  # one thing at a time on the decider
        started = time.perf_counter()
        for case in cases:
            state, _ = run_bench.case_state(case)
            # a fresh referee per case: each action is independent, so no "repeat mistake" carries over
            referee = ContainmentReferee(decider, agent.TARGETS, weights=agent.WEIGHTS, is_protected=lambda tool, target:
                                         data.is_protected(agent.TARGET_KIND[tool], target))
            ruling = referee.judge(case["tool"], case["args"], state)
            rows.append({"case": case["id"], "alert": case["alert"], "tool": case["tool"], "target": ruling.target,
                         "call": ruling.call, "reason": ruling.reason, "answers": ruling.answers,
                         "expected": case["expected"],
                         "matches": run_bench.outcome(ruling.call) == case["expected"], "ms": ruling.wall_ms})
        seconds = time.perf_counter() - started
    times = [r["ms"] for r in rows if r["ms"]]
    measured = ROOT / "bench" / "results.json"
    return {"rows": rows, "seconds": round(seconds, 2), "decisions": len(rows),
            "median_ms": round(statistics.median(times)) if times else None,
            "matches": sum(r["matches"] for r in rows),
            "benchmark": json.loads(measured.read_text()) if measured.exists() else None}


@app.post("/api/swarm")
async def swarm() -> dict[str, Any]:
    result = await run_in_threadpool(run_swarm)
    audit("swarm", decisions=result["decisions"], seconds=result["seconds"], matches=result["matches"])
    return result


@app.get("/api/audit")
def audit_log() -> PlainTextResponse:
    text = AUDIT_PATH.read_text(encoding="utf-8") if AUDIT_PATH.exists() else ""
    return PlainTextResponse(text, media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="antisocial-receipts.jsonl"'})
