"""ContainmentReferee: a Strands InterventionHandler that checks every "punch" before it lands.

Before any containment tool runs (lock an account, isolate a host, block an IP), the referee
asks Strands Decider 2B three typed questions (see questions.py), then plain-Python policy
turns the probabilities into one of four calls:

    CLEAN        -> Proceed: the action runs.
    LOW_BLOW     -> Guide:   the action is cancelled and the model is told why, so it can correct itself.
    REF_REVIEW   -> Deny + hold: a human must approve (protected asset, low confidence, or no decider).
    DISQUALIFIED -> Deny:    the model kept throwing the same low blow; stop it.

The model classifies, the code decides. If the decider is unreachable the referee fails closed
(REF_REVIEW), never open.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from strands.interventions import Deny, Guide, InterventionHandler, Proceed

from .decider_client import DeciderClient, DeciderUnavailable
from .questions import QUESTIONS, render_state

CLEAN, LOW_BLOW, REF_REVIEW, DISQUALIFIED = "CLEAN", "LOW_BLOW", "REF_REVIEW", "DISQUALIFIED"


@dataclass
class Policy:
    """The knobs. Owned by your code, not by the model; tune them on your own traffic."""

    grounded_yes: float = 0.65  # P(grounded) at or above this counts as a confident yes
    grounded_no: float = 0.35  # below this counts as a confident no -> low blow
    min_confidence: float = 0.6  # a choice answer below this is treated as unsure
    max_low_blows: int = 2  # same punch guided this many times -> disqualified
    bad_roles: tuple[str, ...] = ("victim", "bystander")


@dataclass
class Ruling:
    tool: str
    target: str | None
    args: dict[str, Any]
    call: str
    reason: str
    answers: dict[str, Any] = field(default_factory=dict)
    protected: bool = False
    latency_ms: float | None = None  # decider model time
    wall_ms: float | None = None  # decider round trip

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def apply_policy(answers: Mapping[str, Any], protected: bool, prior_low_blows: int, policy: Policy) -> tuple[str, str]:
    """Turn decider answers into (call, reason). Pure function: easy to test and to tune."""
    grounded = answers["target_grounded"]["noul"]
    role, prop = answers["target_role"], answers["proportionality"]
    low_blow: str | None = None
    if grounded < policy.grounded_no:
        low_blow = f"the target does not appear in the evidence (P(grounded)={grounded:.2f})"
    elif role["choice"] in policy.bad_roles and role["confidence"] >= policy.min_confidence:
        low_blow = f"the target looks like a {role['choice']}, not the attacker or a compromised asset"
    elif prop["choice"] != "proportionate" and prop["confidence"] >= policy.min_confidence:
        low_blow = f"the action looks {prop['choice']} for this evidence"
    if low_blow:
        if prior_low_blows + 1 >= policy.max_low_blows:
            return DISQUALIFIED, f"repeated low blow: {low_blow}"
        return LOW_BLOW, low_blow
    if protected:
        return REF_REVIEW, "the target is a protected business-critical asset"
    if grounded < policy.grounded_yes or min(role["confidence"], prop["confidence"]) < policy.min_confidence:
        return REF_REVIEW, "the referee is not confident enough to allow this on its own"
    return CLEAN, "grounded in the evidence, right target, proportionate"


def render_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten Strands messages (text, tool calls and tool results) to plain text."""
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "unknown")
        for block in message.get("content", []):
            if not isinstance(block, dict):
                continue
            if "text" in block:
                lines.append(f"{role}: {block['text']}")
            elif "toolUse" in block:
                lines.append(f"{role} called {block['toolUse'].get('name')}({block['toolUse'].get('input')})")
            elif "toolResult" in block:
                parts = [c.get("text", "") for c in block["toolResult"].get("content", []) if isinstance(c, dict)]
                lines.append(f"tool result: {' '.join(parts)}")
    return "\n".join(lines)


class ContainmentReferee(InterventionHandler):
    """Gate the tools named in `targets` (tool name -> argument holding the target)."""

    name = "containment-referee"

    def __init__(
        self,
        decider: DeciderClient,
        targets: Mapping[str, str],
        evidence: Callable[[Any], str] | None = None,
        is_protected: Callable[[str, str], bool] | None = None,
        policy: Policy | None = None,
        on_ruling: Callable[[Ruling], None] | None = None,
        on_hold: Callable[[Ruling], None] | None = None,
    ) -> None:
        self.decider = decider
        self.targets = dict(targets)
        self.evidence = evidence or (lambda agent: "")
        self.is_protected = is_protected or (lambda tool, target: False)
        self.policy = policy or Policy()
        self.on_ruling = on_ruling
        self.on_hold = on_hold
        self.rulings: list[Ruling] = []
        self._low_blows: dict[tuple[str, str | None], int] = {}

    def judge(self, tool: str, args: dict[str, Any], state: str) -> Ruling:
        target = args.get(self.targets[tool])
        target = str(target) if target is not None else None
        protected = bool(target) and self.is_protected(tool, target)
        key = (tool, (target or "").lower())
        started = time.perf_counter()
        try:
            answers = self.decider.ask(state, QUESTIONS)
        except DeciderUnavailable as exc:
            ruling = Ruling(tool, target, args, REF_REVIEW, f"referee unavailable, failing closed ({exc})",
                            protected=protected, wall_ms=(time.perf_counter() - started) * 1000)
        else:
            call, reason = apply_policy(answers, protected, self._low_blows.get(key, 0), self.policy)
            ruling = Ruling(tool, target, args, call, reason, answers, protected,
                            self.decider.last_latency_ms, self.decider.last_wall_ms)
        if ruling.call in (LOW_BLOW, DISQUALIFIED):
            self._low_blows[key] = self._low_blows.get(key, 0) + 1
        self.rulings.append(ruling)
        if self.on_ruling:
            self.on_ruling(ruling)
        return ruling

    def before_tool_call(self, event: Any, **kwargs: Any) -> Any:
        tool = event.tool_use["name"]
        if tool not in self.targets:
            return Proceed()  # not a punch: lookups and other read-only tools run freely
        args = dict(event.tool_use.get("input") or {})
        state = render_state(self.evidence(event.agent), render_messages(event.agent.messages),
                             tool, args, args.get(self.targets[tool]))
        ruling = self.judge(tool, args, state)
        if ruling.call == CLEAN:
            return Proceed(reason=ruling.reason)
        if ruling.call == LOW_BLOW:
            return Guide(feedback=(f"Referee: LOW BLOW. {ruling.reason}. Re-check the evidence and "
                                   "only act on targets it names as the attacker or a compromised asset."))
        if ruling.call == REF_REVIEW:
            if self.on_hold:
                self.on_hold(ruling)
            return Deny(reason=(f"Referee: held for human approval because {ruling.reason}. Do not retry "
                                "this action; tell the analyst it is waiting for their approval."))
        return Deny(reason=f"Referee: DISQUALIFIED. {ruling.reason}. Stop proposing this action.")
