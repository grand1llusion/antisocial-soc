# 🥊 No Low Blows

**An AI incident responder that throws containment punches, and a 2-billion-parameter referee that
checks every punch before it lands.**

Built for the AWS Builder Center Strands Decider 2B hackathon. Pattern: **`#guardrails`**.

An agentic SOC (Security Operations Center) responder runs on **Amazon Bedrock** through
**Strands Agents**. It investigates a security alert and proposes containment actions: lock an
account, isolate a host, block an IP. Hand an agent those tools and its worst failure is hitting
the wrong target: locking the CEO because they *received* a phishing email, isolating the file
server the whole company works from, or blocking the approved vulnerability scanner.

So every punch goes past a referee first. **Strands Decider 2B** reads the evidence and the
proposed action and answers three typed questions in one fast local pass: is the target grounded
in the evidence, what role does it play (attacker, compromised, victim, bystander), and is the
action proportionate? Plain Python turns the answers into a call:

- ✅ **Clean punch**: the action runs.
- 🛑 **Low blow**: cancelled; the agent gets feedback and corrects itself.
- 🟨 **Ref's review**: protected asset or low confidence; a human approves or rejects in the UI.
- ⛔ **Disqualified**: the same low blow again; stop.

> **All data is AI-generated test data** for a fictional company, and every containment action is
> simulated. IPs come from reserved documentation ranges.

## ♻️ Reusable decision pattern

**[`pattern/containment_referee/`](pattern/containment_referee/)**: a drop-in Strands
`InterventionHandler` that gates any destructive tool with Decider 2B. It contains the typed
questions and option sets, the wiring into the agent's path, and a short how-to.

## Architecture

```
Browser ──> FastAPI (app/server.py) ──> Strands Agent (app/agent.py) ──> Amazon Bedrock (Nova Pro)
                                              │ before every containment tool call
                                              ▼
                                   ContainmentReferee ──HTTP──> Strands Decider 2B (local, GPU)
                                              │ Proceed / Guide / Deny + hold
                                              ▼
                                   simulated lock / isolate / block
```

Everything runs on one EC2 GPU instance (g6.xlarge, NVIDIA L4): the decider server on
127.0.0.1:8099 and the web app on port 8080. Bedrock is called from the instance using its IAM role.

## Run it

**On AWS (what the demo uses):** launch an Ubuntu GPU instance with the setup script in
[`deploy/bootstrap.sh`](deploy/bootstrap.sh) as user data, an instance role allowed to call
Bedrock ([`deploy/iam-policy.json`](deploy/iam-policy.json)), and port 8080 open to your own IP.
Then open `http://<instance-ip>:8080`.

**Locally:** needs AWS credentials with Bedrock access, and a GPU or Apple Silicon Mac for good speed.

```bash
pip install strands-decider -r requirements.txt
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8099 &
uvicorn app.server:app --port 8080
```

## Measure it

[`bench/run_bench.py`](bench/run_bench.py) asks the same three questions about 22 hand-labelled
proposed actions using Decider 2B and a Bedrock LLM, runs both through the same policy, and writes
accuracy, dangerous misses and latency to `bench/results.md`.

```bash
python -m bench.run_bench --llm-model us.amazon.nova-lite-v1:0
```

## Tests

Offline, with a scripted model and a fake decider (no AWS or GPU needed):

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

## License

Apache-2.0. Uses [Strands Agents](https://github.com/strands-agents/sdk-python) and
[Strands Decider](https://github.com/strands-labs/strands-decider) (both Apache-2.0).
