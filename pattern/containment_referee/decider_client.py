"""A minimal client for a running Strands Decider server.

Start a server with:

    strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8099

and point STRANDS_DECIDER_URL at it (default http://127.0.0.1:8099). Standard library only,
so the pattern adds no dependencies beyond strands-agents.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

DEFAULT_URL = os.environ.get("STRANDS_DECIDER_URL", "http://127.0.0.1:8099")


class DeciderUnavailable(RuntimeError):
    """The decider server did not answer."""


def noul(instructions: str, criteria: Mapping[str, str] | None = None) -> dict[str, Any]:
    """A yes/no question. The answer is P(yes) in 0..1."""
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if criteria:
        question["criteria"] = dict(criteria)
    return question


def choice(instructions: str, criteria: Mapping[str, str]) -> dict[str, Any]:
    """Pick one of N named options; criteria maps option name -> what it means."""
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


class DeciderClient:
    """One state, many typed questions, one round trip."""

    def __init__(self, url: str = DEFAULT_URL, timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.last_latency_ms: float | None = None  # model time reported by the server
        self.last_wall_ms: float | None = None  # round trip measured here

    def ask(self, state: str, questions: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
        """Return the answers keyed like `questions`."""
        body = json.dumps({"state": state, "questions": dict(questions)}).encode()
        request = urllib.request.Request(
            f"{self.url}/v1/systemone", data=body, headers={"content-type": "application/json"}
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise DeciderUnavailable(f"no strands-decider at {self.url}: {exc}") from exc
        self.last_wall_ms = (time.perf_counter() - started) * 1000
        self.last_latency_ms = payload.get("latency_ms")
        return payload["answers"]

    def health(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=10) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise DeciderUnavailable(f"no strands-decider at {self.url}: {exc}") from exc
