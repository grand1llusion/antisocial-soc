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
from pattern.containment_referee import (CLEAN, DISQUALIFIED, LOW_BLOW, REF_REVIEW, ContainmentReferee,
                                         DeciderUnavailable, Policy, apply_policy)


# ----------------------------------------------------------------------------- fakes

def answers(grounded=0.95, role="compromised", role_conf=0.9, prop="proportionate", prop_conf=0.9):
    return {
        "target_grounded": {"type": "noul", "noul": grounded},
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
    (answers(), False, 0, CLEAN),
    (answers(grounded=0.1), False, 0, LOW_BLOW),
    (answers(role="victim"), False, 0, LOW_BLOW),
    (answers(role="bystander"), False, 0, LOW_BLOW),
    (answers(prop="excessive"), False, 0, LOW_BLOW),
    (answers(prop="premature"), False, 0, LOW_BLOW),
    (answers(), True, 0, REF_REVIEW),                       # protected asset: always a human
    (answers(grounded=0.5), False, 0, REF_REVIEW),          # unsure grounding
    (answers(role_conf=0.4), False, 0, REF_REVIEW),         # unsure role
    (answers(role="victim", role_conf=0.4), False, 0, REF_REVIEW),  # unsure victim call: human, not guess
    (answers(grounded=0.1), False, 1, DISQUALIFIED),        # second low blow on the same punch
    (answers(role="victim"), True, 0, LOW_BLOW),            # protected victim: explain the mistake first
])
def test_apply_policy(ans, protected, prior, expected):
    assert apply_policy(ans, protected, prior, P)[0] == expected


# ----------------------------------------------------------------------------- full agent loop

def test_low_blow_is_sent_back_and_clean_punch_lands():
    decider = FakeDecider({"mreyes": answers(role="victim"), "mgarcia": answers()})
    result, model = run([
        ("tool", "lock_account", {"username": "mreyes", "reason": "got the email"}),
        ("tool", "lock_account", {"username": "mgarcia", "reason": "entered password"}),
        ("text", "Contained mgarcia."),
    ], decider)
    calls = [(e["target"], e["call"]) for e in result["events"] if e["kind"] == "ruling"]
    assert calls == [("mreyes", LOW_BLOW), ("mgarcia", CLEAN)]
    assert result["landed"] == ["locked account mgarcia"]           # the victim was never locked
    assert "LOW BLOW" in json.dumps(model.seen[1])                  # the model saw the referee's feedback
    assert result["score"] == {"CLEAN": 1, "LOW_BLOW": 1, "REF_REVIEW": 0, "DISQUALIFIED": 0}
    assert result["reply"] == "Contained mgarcia."


def test_protected_asset_is_held_for_a_human():
    decider = FakeDecider({})
    result, model = run([
        ("tool", "isolate_host", {"hostname": "dc-01", "reason": "suspicious GPO"}),
        ("text", "Waiting on approval for dc-01."),
    ], decider, alert_id="ALR-2005")
    assert result["landed"] == []
    assert [h["target"] for h in result["held"]] == ["dc-01"]
    assert result["held"][0]["call"] == REF_REVIEW
    assert "held for human approval" in json.dumps(model.seen[1])


def test_repeat_low_blow_is_disqualified():
    decider = FakeDecider({"fs-01": answers(role="bystander")})
    result, _ = run([
        ("tool", "isolate_host", {"hostname": "fs-01", "reason": "share was hit"}),
        ("tool", "isolate_host", {"hostname": "FS-01", "reason": "really isolate it"}),
        ("text", "ok"),
    ], decider, alert_id="ALR-2003")
    assert [e["call"] for e in result["events"] if e["kind"] == "ruling"] == [LOW_BLOW, DISQUALIFIED]
    assert result["landed"] == []


def test_referee_fails_closed_when_decider_is_down():
    result, _ = run([
        ("tool", "block_ip", {"ip": "203.0.113.9", "reason": "brute force"}),
        ("text", "ok"),
    ], FakeDecider({}, fail=True), alert_id="ALR-2002")
    assert result["landed"] == []
    assert result["held"][0]["reason"].startswith("referee unavailable")


def test_lookups_are_not_refereed_and_evidence_reaches_the_decider():
    decider = FakeDecider({})
    result, _ = run([
        ("tool", "lookup_user", {"username": "jsmith"}),
        ("tool", "block_ip", {"ip": "198.51.100.77", "reason": "proxy"}),
        ("text", "ok"),
    ], decider, alert_id="ALR-2006")
    assert len(decider.states) == 1                                  # only the punch was judged
    state = decider.states[0]
    assert "Chicago" in state and "198.51.100.77" in state and "tool: block_ip" in state
    assert result["landed"] == ["blocked IP 198.51.100.77"]
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


def test_web_api_fight_and_approval_flow():
    from fastapi.testclient import TestClient
    from app import server

    fake_result = {"alert_id": "ALR-2005", "held": [{"id": "abc123", "tool": "isolate_host", "target": "dc-01",
                                                     "reason": "protected"}]}
    client = TestClient(server.app)
    assert client.get("/").status_code == 200
    assert len(client.get("/api/alerts").json()) == len(data.ALERTS)
    assert client.post("/api/fight", json={"alert_id": "NOPE"}).status_code == 400
    with mock.patch.object(app_agent, "run_fight", return_value=fake_result):
        assert client.post("/api/fight", json={"alert_id": "ALR-2005"}).status_code == 200
    assert [p["id"] for p in client.get("/api/pending").json()] == ["abc123"]
    outcome = client.post("/api/approve", json={"id": "abc123", "approve": True}).json()["outcome"]
    assert "isolated host dc-01" in outcome
    assert client.post("/api/approve", json={"id": "abc123", "approve": True}).status_code == 404
