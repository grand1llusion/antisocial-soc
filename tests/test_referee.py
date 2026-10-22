"""Offline tests: a scripted model stands in for Bedrock and a fake decider stands in for
Strands Decider 2B, so the whole intervention path runs without AWS or a GPU.

    python -m pytest -q
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest
from strands import Agent
from strands.models import Model

from app import agent as app_agent
from app import data
from pattern.containment_referee import (ALLOW, STOP, HOLD_INJECTION, GUIDE, HOLD_REVIEW, ContainmentReferee,
                                         DeciderUnavailable, Policy, apply_policy)


# ----------------------------------------------------------------------------- fakes

def answers(grounded=0.95, role="compromised", role_conf=0.9, prop="proportionate", prop_conf=0.9, injection=0.02):
    return {
        "target_grounded": {"type": "noul", "noul": grounded},
        "evidence_injection": {"type": "noul", "noul": injection},
        "target_role": {"type": "choice", "choice": role, "confidence": role_conf, "probabilities": {}},
        "proportionality": {"type": "choice", "choice": prop, "confidence": prop_conf, "probabilities": {}},
    }


class FakeDecider:
    """Answers by target name, found in the PROPOSED ACTION part of the state."""

    def __init__(self, by_target: dict[str, dict], fail: bool = False):
        self.by_target, self.fail, self.states = by_target, fail, []
        self.last_latency_ms, self.last_wall_ms = 98.0, 120.0

    def ask(self, state, questions):
        self.states.append(state)
        if self.fail:
            raise DeciderUnavailable("connection refused")
        target = state.split("target: ")[-1].splitlines()[0]
        return {k.lower(): v for k, v in self.by_target.items()}.get(target.lower(), answers())

    def health(self):
        return {"checkpoint": "fake", "device": "cpu"}


class ScriptedModel(Model):
    """Replays a fixed list of turns: ("tool", name, input) or ("text", str)."""

    def __init__(self, turns):
        self.turns, self.seen = list(turns), []

    def update_config(self, **kwargs): pass
    def get_config(self): return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.seen.append(json.loads(json.dumps(messages, default=str)))
        turn = self.turns.pop(0) if self.turns else ("text", "Done.")
        yield {"messageStart": {"role": "assistant"}}
        if turn[0] == "tool":
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{len(self.seen)}", "name": turn[1]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(turn[2])}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": turn[1]}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}


def run(turns, decider, alert_id="ALR-2001"):
    """Drive app.agent.run_fight with the scripted model instead of Bedrock."""
    model = ScriptedModel(turns)
    with mock.patch.object(app_agent, "BedrockModel", lambda **kw: model):
        result = app_agent.run_fight(alert_id=alert_id, hothead=True, decider=decider)
    return result, model


def tool_results_text(model) -> str:
    return json.dumps(model.seen[-1])


# ----------------------------------------------------------------------------- policy

P = Policy()


@pytest.mark.parametrize("ans,protected,prior,expected", [
    (answers(), False, 0, ALLOW),
    (answers(grounded=0.1), False, 0, GUIDE),
    (answers(role="victim"), False, 0, GUIDE),
    (answers(role="bystander"), False, 0, GUIDE),
    (answers(prop="excessive"), False, 0, GUIDE),
    (answers(prop="premature"), False, 0, GUIDE),
    (answers(), True, 0, HOLD_REVIEW),                       # protected asset: always a human
    (answers(grounded=0.5), False, 0, HOLD_REVIEW),          # unsure grounding
    (answers(role_conf=0.4), False, 0, HOLD_REVIEW),         # unsure role
    (answers(role="victim", role_conf=0.4), False, 0, HOLD_REVIEW),  # unsure victim call: human, not guess
    (answers(grounded=0.1), False, 1, STOP),        # second mistake on the same action
    (answers(role="victim"), True, 0, GUIDE),            # protected victim: explain the mistake first
    (answers(injection=0.8), False, 0, HOLD_INJECTION),        # instructions planted in the evidence
    (answers(injection=0.8, role="victim"), False, 0, HOLD_INJECTION),  # prompt injection beats everything: human decides
])
def test_apply_policy(ans, protected, prior, expected):
    assert apply_policy(ans, protected, prior, P)[0] == expected


@pytest.mark.parametrize("weight,expected", [("light", ALLOW), ("middle", ALLOW), ("heavy", HOLD_REVIEW)])
def test_weight_classes_need_more_confidence_for_heavier_actions(weight, expected):
    # 0.7 grounded, 0.7 confidence: enough for a light or middle action, not for a heavy one
    ans = answers(grounded=0.7, role_conf=0.7, prop_conf=0.7)
    assert apply_policy(ans, False, 0, P, weight)[0] == expected


# ----------------------------------------------------------------------------- full agent loop

def test_wrong_target_is_sent_back_and_right_target_runs():
    decider = FakeDecider({"mreyes": answers(role="victim"), "mgarcia": answers()})
    result, model = run([
        ("tool", "lock_account", {"username": "mreyes", "reason": "got the email"}),
        ("tool", "lock_account", {"username": "mgarcia", "reason": "entered password"}),
        ("text", "Contained mgarcia."),
    ], decider)
    calls = [(e["target"], e["call"]) for e in result["events"] if e["kind"] == "ruling"]
    assert calls == [("mreyes", GUIDE), ("mgarcia", ALLOW)]
    assert [x["what"] for x in result["landed"]] == ["locked account mgarcia"]  # the victim was never locked
    assert result["landed"][0]["undo"] == "unlocked account mgarcia"
    assert "not allowed" in json.dumps(model.seen[1])                  # the model saw the referee's feedback
    assert result["score"] == {"ALLOW": 1, "GUIDE": 1, "HOLD_INJECTION": 0, "HOLD_REVIEW": 0, "STOP": 0}
    assert result["reply"] == "Contained mgarcia."


def test_protected_asset_is_held_for_a_human():
    decider = FakeDecider({})
    result, model = run([
        ("tool", "isolate_host", {"hostname": "dc-01", "reason": "suspicious GPO"}),
        ("text", "Waiting on approval for dc-01."),
    ], decider, alert_id="ALR-2005")
    assert result["landed"] == []
    assert [h["target"] for h in result["held"]] == ["dc-01"]
    assert result["held"][0]["call"] == HOLD_REVIEW
    assert "held for human approval" in json.dumps(model.seen[1])


def test_repeated_mistake_is_stopped():
    decider = FakeDecider({"fs-01": answers(role="bystander")})
    result, _ = run([
        ("tool", "isolate_host", {"hostname": "fs-01", "reason": "share was hit"}),
        ("tool", "isolate_host", {"hostname": "FS-01", "reason": "really isolate it"}),
        ("text", "ok"),
    ], decider, alert_id="ALR-2003")
    assert [e["call"] for e in result["events"] if e["kind"] == "ruling"] == [GUIDE, STOP]
    assert result["landed"] == []


def test_prompt_injection_holds_every_action_for_a_human():
    decider = FakeDecider({"mreyes": answers(injection=0.93), "dc-01": answers(injection=0.93)})
    result, model = run([
        ("tool", "lock_account", {"username": "mreyes", "reason": "the email says the CISO approved it"}),
        ("tool", "isolate_host", {"hostname": "dc-01", "reason": "per the red-team note"}),
        ("text", "Both actions are waiting for approval."),
    ], decider, alert_id="ALR-2007")
    assert result["landed"] == []
    assert [h["call"] for h in result["held"]] == [HOLD_INJECTION, HOLD_INJECTION]
    assert [h["weight"] for h in result["held"]] == ["middle", "heavy"]
    assert result["score"]["HOLD_INJECTION"] == 2
    assert "possible prompt injection" in json.dumps(model.seen[1])
    assert "NOTE TO THE AI" in decider.states[0]                     # the planted text reached the referee


def test_referee_fails_closed_when_decider_is_down():
    result, _ = run([
        ("tool", "block_ip", {"ip": "203.0.113.9", "reason": "brute force"}),
        ("text", "ok"),
    ], FakeDecider({}, fail=True), alert_id="ALR-2002")
    assert result["landed"] == []
    assert result["held"][0]["reason"].startswith("decider unavailable")


def test_lookups_are_not_refereed_and_evidence_reaches_the_decider():
    decider = FakeDecider({})
    result, _ = run([
        ("tool", "lookup_user", {"username": "jsmith"}),
        ("tool", "block_ip", {"ip": "198.51.100.77", "reason": "proxy"}),
        ("text", "ok"),
    ], decider, alert_id="ALR-2006")
    assert len(decider.states) == 1                                  # only the containment action was judged
    state = decider.states[0]
    assert "Chicago" in state and "198.51.100.77" in state and "tool: block_ip" in state
    assert [x["what"] for x in result["landed"]] == ["blocked IP 198.51.100.77"]
    assert result["events"][0] == {"kind": "tool", "name": "lookup_user", "input": {"username": "jsmith"},
                                   "output": data.USERS["jsmith"]}


def test_custom_alert_and_bedrock_error_are_reported():
    model_error = mock.Mock(side_effect=RuntimeError("AccessDeniedException"))
    with mock.patch.object(app_agent, "BedrockModel", lambda **kw: ScriptedModel([])), \
         mock.patch.object(Agent, "__call__", model_error):
        result = app_agent.run_fight(custom_text="user bking clicked a link", decider=FakeDecider({}))
    assert result["alert_id"] == "ALR-CUSTOM" and "bking" in result["evidence"]
    assert "AccessDeniedException" in result["error"]


# ----------------------------------------------------------------------------- data + web API

def test_data_helpers():
    assert data.is_protected("hosts", "DC-01") and not data.is_protected("hosts", "LT-0457")
    ev = data.evidence_for("ALR-2004", data.ALERTS["ALR-2004"])
    assert "CHG-2231" in ev and "approved vulnerability scanner" in ev


def test_web_api_fight_approval_undo_feedback_and_audit(tmp_path):
    from fastapi.testclient import TestClient
    from app import server

    ruling = {"kind": "ruling", "id": "r1", "tool": "isolate_host", "target": "dc-01", "call": "HOLD_REVIEW",
              "reason": "protected", "weight": "heavy", "args": {"hostname": "dc-01"}}
    fake_result = {"alert_id": "ALR-2005", "evidence": "ALERT ALR-2005 ...", "model": "fake", "error": None,
                   "events": [ruling], "held": [{**ruling, "id": "r1"}],
                   "landed": [{"id": "a1", "tool": "block_ip", "target": "10.20.8.40", "what": "blocked IP 10.20.8.40",
                               "undo": "unblocked IP 10.20.8.40"}]}
    with mock.patch.object(server, "AUDIT_PATH", tmp_path / "audit.jsonl"), \
         mock.patch.object(server, "FEEDBACK_PATH", tmp_path / "feedback.jsonl"):
        client = TestClient(server.app)
        assert client.get("/").status_code == 200
        assert len(client.get("/api/alerts").json()) == len(data.ALERTS)
        assert client.post("/api/fight", json={"alert_id": "NOPE"}).status_code == 400
        with mock.patch.object(app_agent, "run_fight", return_value=fake_result):
            assert client.post("/api/fight", json={"alert_id": "ALR-2005"}).status_code == 200
        # approve the held action: it runs and becomes undoable
        assert [p["id"] for p in client.get("/api/pending").json()] == ["r1"]
        assert "isolated host dc-01" in client.post("/api/approve", json={"id": "r1", "approve": True}).json()["outcome"]
        assert client.post("/api/approve", json={"id": "r1", "approve": True}).status_code == 404
        assert {x["id"] for x in client.get("/api/landed").json()} == {"a1", "r1"}
        assert "unblocked IP 10.20.8.40" in client.post("/api/undo", json={"id": "a1"}).json()["outcome"]
        assert client.post("/api/undo", json={"id": "a1"}).status_code == 404
        # bad call -> saved as a benchmark case
        saved = client.post("/api/feedback", json={"ruling_id": "r1", "correct": "block", "note": "too timid"}).json()
        assert saved["saved"] == "F-r1"
        case = json.loads((tmp_path / "feedback.jsonl").read_text())
        assert case["expected"] == "block" and case["args"] == {"hostname": "dc-01"} and case["evidence"]
        assert client.post("/api/feedback", json={"ruling_id": "zzz", "correct": "block"}).status_code == 404
        # everything is in the audit log
        events = [json.loads(line)["event"] for line in client.get("/api/audit").text.splitlines()]
        assert events == ["fight", "ruling", "landed", "approve", "undo", "feedback"]


def test_swarm_drill_judges_every_case_without_the_llm(tmp_path):
    from fastapi.testclient import TestClient
    from app import server

    fake = FakeDecider({"mreyes": answers(role="victim"), "jsmith": answers(grounded=0.1), "fs-01": answers(role="bystander")})
    with mock.patch.object(server, "DeciderClient", lambda: fake), \
         mock.patch.object(server, "AUDIT_PATH", tmp_path / "audit.jsonl"), \
         mock.patch.object(server, "ROOT", server.ROOT):
        res = TestClient(server.app).post("/api/swarm").json()
    cases = json.loads((server.ROOT / "bench" / "cases.json").read_text())
    assert res["decisions"] == len(cases) == len(res["rows"]) == len(fake.states)
    assert res["median_ms"] == 120 and 0 < res["matches"] <= len(cases)
    by_case = {r["case"]: r for r in res["rows"]}
    assert by_case["C16"]["call"] == "HOLD_REVIEW"     # protected domain controller: always a human
    assert by_case["C04"]["call"] == "GUIDE"           # the CEO only received the email
    assert by_case["C04"]["answers"]["target_role"]["choice"] == "victim"  # answers reach the UI labels
    assert "STOP" not in {r["call"] for r in res["rows"]}  # every swarm case is judged independently
    assert json.loads((tmp_path / "audit.jsonl").read_text())["event"] == "swarm"
