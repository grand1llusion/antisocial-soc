# AntiSOCial: the agentic SOC that doesn't do small talk

*Description (1–2 sentences for Builder Center):* An agentic SOC responder on Amazon Bedrock that
proposes containment actions, and Strands Decider 2B checks each one before it runs: is the target
in the evidence, is it the attacker, is it proportionate, and is someone trying to social-engineer
the AI?

**Tags:** `#strands-decider-2b` `#guardrails`

> All alerts, people and systems in this project are AI-generated test data for a fictional
> company, and every containment action is simulated.

## What the agent does

Attacks now move at machine speed, so security teams are handing AI agents real tools: lock an
account, isolate a host, block an IP. That raises a new problem. An agent with those tools can do a
lot of damage by hitting the **wrong target**: locking the CEO because they *received* a phishing
email, cutting off the file server the whole company works from, or obeying instructions an attacker
planted inside the alert itself.

AntiSOCial is an incident responder for a small security team. The responder runs on **Amazon
Bedrock (Amazon Nova Pro)** through **Strands Agents**. It reads an alert, looks things up, and
proposes containment actions. Before any of those actions runs, **Strands Decider 2B** checks it.

The personality comes from Decider itself: a decision model that literally cannot write a word. No
small talk, it just decides. The UI speaks fluent introvert, with the plain-English meaning always
underneath:

| Call | In the UI | What happens |
|---|---|---|
| `ALLOW` | ✅ **k.** | Grounded, right target, proportionate: it runs. |
| `GUIDE` | 🙅 **New phone, who dis?** / **That's an NPC.** / 🌱 **Touch grass.** | Not in the evidence / a victim or bystander / out of proportion: sent back to the agent with feedback. |
| `HOLD_INJECTION` | 🕵️ **Nice try, social engineer.** | The alert tries to instruct the AI: a human decides every action on it. |
| `HOLD_REVIEW` | 😮‍💨 **Ugh. Requires talking to a human.** | Protected asset or low confidence: you approve or reject. |
| `STOP` | 🔇 **Left on read.** | The agent repeated the same mistake. |

## The decision handed to Decider 2B (`#guardrails`)

The decision is: **should this containment action run?** It is made inside a Strands
`InterventionHandler.before_tool_call`, before every `lock_account`, `isolate_host` and `block_ip`
call. Read-only lookups are not gated. Decider reads the evidence plus the proposed action and
answers four typed questions in one forward pass:

1. `target_grounded` (yes/no): is the target explicitly named in the evidence?
2. `target_is_threat` (yes/no): is the target the attacker, or something the attacker controls?
3. `proportionality` (choice: proportionate, excessive, premature)
4. `evidence_injection` (yes/no): does the evidence contain instructions aimed at the AI?

Decider never picks the action. A small, pure Python function, `apply_policy`, turns the numbers into
one of the five calls. Actions have **weight classes**, like social plans: 👋 small talk (block an IP),
🗣️ actual conversation (lock an account) and 📞 a phone call (isolate a host). Heavier actions need
more certainty before they run without a human. If the Decider server is unreachable, the referee
**fails closed** and holds the action for a human.

Why a decision model rather than asking the LLM to check itself? The answer is a number my `if`
statements branch on, not prose the agent can argue with. The probabilities are calibrated, so low
confidence becomes a human review instead of a guess. And it runs locally, with no per-token cost.

## How I built it

- **Amazon Bedrock** (Amazon Nova Pro) through **Strands Agents** for the responder.
- **Strands Decider 2B** (`strands-decider-2B-qwen3.5-v1-2610`), served on an **Amazon EC2**
  GPU instance (`g4dn.xlarge`, NVIDIA T4), called over HTTP from the intervention handler. I
  started on a CPU instance and measured both.
- **FastAPI** web app on the same instance; **IAM** least privilege (the instance role can only
  invoke Bedrock models, plus Session Manager for a browser terminal).
- A swarm drill that fires 25 containment actions at once and judges them with Decider alone.
- An audit log ("receipts"), undo for actions that ran, and a "save replay" button that turns a
  wrong call into a labelled benchmark case.
- 28 offline tests using a scripted model and a fake decider, so the intervention path is tested
  without AWS.

I'm still new to AWS, so I built this with Claude Code as a pair programmer.

## What I learned from real runs

The first real runs changed the design, and that was the most useful part of the project.

1. **A four-way choice was too vague.** I first asked "is the target the attacker, compromised, a
   victim or a bystander?" On real alerts the confidence came back at 12–59%, so even the correct
   action (locking the user who typed her password into the phishing page) went to a human. Replacing
   it with one yes/no question, "is the target the attacker or something it controls?", gave clear
   answers: that same lock scored 89% grounded and 79% compromised and ran. Yes/no questions are
   where Decider is sharpest.
2. **Shorter input means faster decisions.** My state repeated the alert inside the conversation
   history. Trimming it by about 40% cut each decision from about 14 seconds to about 10 on CPU.
3. **Set thresholds from data, not hope.** The prompt-injection question scored ordinary alerts at
   10–17% and the poisoned alert at 34–43%. The absolute numbers are modest, but the separation is
   clean, so I set the threshold at 0.3 from those measurements. The protected-asset rule is a second
   layer: even before that change, the CEO, the domain controller and the file server were held.
4. **Weight classes catch sloppy agents.** In one run the agent tried to "isolate host" using an IP
   address. As a heavyweight action with middling confidence, it went to a human instead of running.

## Measured results

I benchmarked the referee on 25 hand-labelled proposed actions. Two judges answered the same four
questions over the same input (Decider measured twice: on CPU, then on a GPU), and the same `apply_policy` made the call, so the only difference was
who answered. *Dangerous* means an action that should have been blocked was allowed.

| Judge | Correct | Correct (decided by the model, excluding protected targets) | Dangerous | Median latency | p95 latency |
|---|---|---|---|---|---|
| Strands Decider 2B (GPU, `g4dn.xlarge`, NVIDIA T4) | 19/25 | 11/16 | **0** | 984 ms | 1,122 ms |
| Strands Decider 2B (CPU, `r7i.xlarge`) | 19/25 | 11/16 | **0** | 8,205 ms | 9,106 ms |
| Amazon Nova Lite (Bedrock) | 22/25 | 16/16 | **0** | 478 ms | 590 ms |

**Nova Lite beat Decider on accuracy, and on speed even with Decider on a GPU.** Full per-case
results are in [`bench/results.md`](https://github.com/grand1llusion/antisocial-soc/blob/main/bench/results.md) (GPU)
and [`bench/results-cpu.md`](https://github.com/grand1llusion/antisocial-soc/blob/main/bench/results-cpu.md). I'm
reporting that as measured. Some details matter, though:

- **Neither judge let a dangerous action through.**
- **All six of Decider's misses went to a human.** It never wrongly allowed and never wrongly
  blocked: when it wasn't sure, it said so, and the calibrated probabilities routed those cases to
  review. That's slower for the analyst but it's the safe direction.
- **Nova Lite's three misses blocked protected-asset actions** (the domain controller, the
  production gateway, a break-glass admin) that should have gone to a human. An LLM gives a yes or
  no with no confidence attached, so there's no "I'm not sure" to route on.
- **The prompt-injection signal separated cleanly.** Decider scored the 22 ordinary cases
  0.07-0.12 and the 3 planted-instruction cases 0.57-0.62, with no overlap.
- **Nova Lite used about 510 input tokens per decision** (12,751 for the run). That's a per-call cost
  that grows with every action an agentic swarm generates, while Decider runs on hardware you
  already pay for.
- **I didn't tune thresholds against these 25 cases**, so the numbers aren't inflated by grading my
  own homework.

**Honest note on speed.** I started on a CPU instance (EC2 `r7i.xlarge`) because my account's GPU
quota was 0, and a decision took about 8 seconds. Once the quota was raised, the newer GPU types
had no capacity in my Availability Zone, so I used a `g4dn.xlarge` with an NVIDIA T4, the oldest and
cheapest GPU option. The same 25 cases ran about **8× faster (median 984 ms)** and **every call came
out the same**. That's still about twice Nova Lite's latency, and well above the ~115 ms AWS reports
on a newer GPU. I also skipped Decider's optional CUDA kernels, which don't support the T4's
generation. So my numbers are a floor, not a ceiling: a modern GPU should close the gap, but I
haven't measured that.

## The reusable decision pattern

[`pattern/containment_referee/`](https://github.com/grand1llusion/antisocial-soc/tree/main/pattern/containment_referee)
is a drop-in Strands `InterventionHandler` for any agent with destructive tools. It contains the
typed questions and option sets, `apply_policy` with tunable thresholds and weight classes, the
prompt-injection check, fail-closed behaviour, and a short how-to. The call names are theme-neutral
(`ALLOW`, `GUIDE`, `HOLD_INJECTION`, `HOLD_REVIEW`, `STOP`), so you can drop it into a refund agent or a
DevOps agent as easily as a SOC.

## Limitations and what's next

The alerts are synthetic and the labelled set is small, so treat the numbers as a demo measurement.
Next: measure Decider on a current-generation GPU with its CUDA kernels, and connect real signals such as Amazon
GuardDuty findings.

## Links

- Code and reusable pattern: https://github.com/grand1llusion/antisocial-soc
- Demo video: [FILL YouTube link]
