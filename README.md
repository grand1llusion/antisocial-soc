# 🎧 AntiSOCial

**The agentic SOC that doesn't do small talk.** An AI incident responder proposes containment
actions, and **Strands Decider 2B**, a 2-billion-parameter decision model that literally can't write
a word, checks every one of them in under a second before it runs.

▶️ **Demo video:** https://www.youtube.com/watch?v=pM7DzrvjEMY

Built for the AWS Builder Center Strands Decider 2B hackathon. Pattern: **`#guardrails`**.

## Why

Agentic attacks move at machine speed: the fastest recorded eCrime breakout time is under a
minute. So defenders are handing agents real tools: lock an account, isolate a host, block an IP.
The worst failure of such an agent is hitting the wrong target: locking the CEO because they
*received* a phishing email, cutting off the file server the whole company works from, blocking the
approved vulnerability scanner, or obeying instructions an attacker planted inside the alert itself.

AntiSOCial's responder runs on **Amazon Bedrock** through **Strands Agents**. Before any
containment action runs, Decider 2B reads the evidence and the proposed action and answers four
typed questions in one fast local pass:

1. Is the target actually in the evidence?
2. Is it the attacker, or something the attacker controls (not a victim or bystander)?
3. Is the action proportionate?
4. Does the evidence contain instructions aimed at the AI (prompt injection)?

Plain Python turns the answers into a call. The UI speaks fluent introvert; the plain-English
meaning is always shown underneath:

| Call (in code) | In the UI | What happens |
|---|---|---|
| `ALLOW` | ✅ **k.** | Grounded, right target, proportionate: it runs. |
| `GUIDE` | 🙅 **New phone, who dis?** / **That's an NPC.** / 🙄 **Way too much.** | Target not in the evidence / a victim or bystander / out of proportion: sent back to the agent with feedback. |
| `HOLD_INJECTION` | 🕵️ **Nice try, social engineer.** | The alert tries to instruct the AI: every action on it waits for a human. |
| `HOLD_REVIEW` | 😮‍💨 **Ugh. Requires talking to a human.** | Protected asset or low confidence: you approve or reject. |
| `STOP` | 🔇 **Left on read.** | The agent repeated the same mistake. |

Heavier actions need more certainty, like social plans: 👋 **small talk** (block an IP),
🗣️ **actual conversation** (lock an account), 📞 **a phone call** (isolate a host).

Also in the ring:

- 🎉 **Extrovert mode**: an overeager agent that wants to lock everyone, so you can watch the checks work.
- ⚔️ **Raid incoming**: a swarm drill that fires 25 containment actions at once and judges them all with
  Decider alone, showing decisions per second and agreement with the analyst's labels.
- 🧾 **Receipts**: every decision, approval, undo and correction goes to a downloadable audit log.
- ↩ **Undo** for actions that ran, and 📼 **Save replay** to turn a wrong call into a labelled benchmark case.

> **All data is AI-generated test data** for a fictional company, and every containment action is
> simulated. IPs come from reserved documentation ranges.

## ♻️ Reusable decision pattern

**[`pattern/containment_referee/`](pattern/containment_referee/)**: a drop-in Strands
`InterventionHandler` that gates any destructive tool with Decider 2B. It has the typed questions
and option sets, the wiring into the agent's path, weight classes, the prompt-injection check, and a
short how-to. Its call names are theme-neutral (`ALLOW`, `GUIDE`, `HOLD_INJECTION`, `HOLD_REVIEW`,
`STOP`) so you can drop it into any agent.

📓 **Try it in a notebook:** [`notebooks/containment_referee_quickstart.ipynb`](notebooks/containment_referee_quickstart.ipynb)
walks through the questions, the state, the policy and four real calls. It runs without AWS: it asks a
live Decider server if one is reachable, otherwise it replays the answers Decider gave in the GPU benchmark.

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

**On AWS (what the demo uses):** launch an Ubuntu GPU instance with
[`deploy/bootstrap.sh`](deploy/bootstrap.sh) as user data and port 8080 open to your own IP, then
open `http://<instance-ip>:8080`. Give the instance a least-privilege role: an inline policy with
[`deploy/iam-policy.json`](deploy/iam-policy.json) (invoke Bedrock models, nothing else) plus
`AmazonSSMManagedInstanceCore` for a browser terminal. The agent never touches real systems, so it
needs no other permissions. No GPU? The script falls back to CPU (slower decisions, same answers).

**Locally:** needs AWS credentials with Bedrock access, and a GPU or Apple Silicon Mac for good speed.

```bash
pip install strands-decider -r requirements.txt
strands-decider serve StrandsAgents/strands-decider-2B-qwen3.5-v1-2610 --port 8099 &
uvicorn app.server:app --port 8080
```

## Measure it

[`bench/run_bench.py`](bench/run_bench.py) asks the same four questions about 25 hand-labelled
proposed actions using Decider 2B and a Bedrock LLM, runs both through the same policy, and writes
accuracy, dangerous misses and latency to `bench/results.md` (and a summary the raid view shows).

```bash
python -m bench.run_bench --llm-model us.amazon.nova-lite-v1:0
python -m bench.run_bench --with-feedback   # also replay the "wrong call" cases saved from the UI
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
