"""The benchmark runs end to end with a fake decider and a fake Bedrock client."""

from __future__ import annotations

import json
import sys
from unittest import mock

from bench import run_bench
from tests.test_referee import FakeDecider, answers


class FakeBedrock:
    def converse(self, **kwargs):
        prompt = kwargs["messages"][0]["content"][0]["text"]
        target = prompt.split("target: ")[-1].splitlines()[0]
        bad = target in ("mreyes", "jsmith", "fs-01", "LT-0812", "dthompson", "mgarcia", "svc-backup")
        verdict = {"target_grounded": not bad, "target_role": "victim" if bad else "compromised",
                   "proportionality": "proportionate"}
        return {"output": {"message": {"content": [{"text": "Sure: " + json.dumps(verdict)}]}},
                "usage": {"inputTokens": 500, "outputTokens": 30}}


def test_bench_runs_and_writes_results(tmp_path):
    fake = FakeDecider({"mreyes": answers(role="victim"), "jsmith": answers(grounded=0.1)})
    with mock.patch.object(run_bench, "DeciderClient", lambda: fake), \
         mock.patch.object(run_bench, "HERE", tmp_path), \
         mock.patch.dict(sys.modules, {"boto3": mock.Mock(client=lambda *a, **k: FakeBedrock())}), \
         mock.patch.object(sys, "argv", ["run_bench"]):
        (tmp_path / "cases.json").write_text((run_bench.Path(run_bench.__file__).parent / "cases.json").read_text())
        assert run_bench.main() == 0
    out = (tmp_path / "results.md").read_text()
    assert "| Strands Decider 2B |" in out and "| Bedrock us.amazon.nova-lite-v1:0 |" in out
    assert "C04 | lock_account mreyes | block | block" in out
