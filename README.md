# GHOST — Auditable Browser Agent

> GHOST runs bounded browser workflows with visible actions, evidence-backed outputs and human approval for consequential actions.

An AI agent that completes permitted browser workflows, explains what it did,
and produces a replayable audit trail. You give it a task in natural language
plus an explicit permission envelope — approved domains, allowed actions, an
output schema, a step budget and timeouts — and it plans a bounded workflow,
navigates approved sites, extracts evidence, and returns structured results.
Every step is logged with URL, page title, timestamp and excerpt evidence.
Every extracted field carries its own evidence. Consequential actions
(send, buy, delete, post, submit) always require human approval.

## What makes it different

Most browser agents are opaque: "the AI did it." GHOST is built the other
way round:

- **Safe execution first.** Domain and action allowlists, no localhost /
  cloud-metadata / private-network access, downloads blocked by default,
  credentials scanned out of prompts and logs.
- **Visible steps.** A structured action log records every step with its
  outcome and the exact reason for any policy block or failure.
- **Evidence-backed outputs.** Every extracted field carries URL, title,
  timestamp, excerpt and selector. No field without evidence.
- **Replayable.** Re-run a job from its captured steps and verify the
  replay reproduces the original evidence.
- **Page content is untrusted data.** Instructions embedded in a fetched
  page are recorded and ignored — they can never widen the policy.

## Quickstart

```bash
python demo.py                 # one job over local fixture pages + replay
python -m ghost benchmark      # 14 deterministic benchmark tasks
python -m unittest discover -s tests -v
```

Run a job from JSON:

```bash
python -m ghost run job.json
```

`job.json` shape:

```json
{
  "objective": "List the products and the price of Widget Alpha",
  "start_url": "http://127.0.0.1:PORT/products.html",
  "approved_domains": ["127.0.0.1"],
  "allowed_actions": ["navigate", "extract", "click", "snapshot"],
  "extract": [
    {"name": "first_product", "selector": {"kind": "table", "table_id": "products", "row": 1, "col": 0}},
    {"name": "first_price", "selector": {"kind": "table", "table_id": "products", "row": 1, "col": 1}}
  ],
  "step_budget": 25,
  "timeout_seconds": 60,
  "allow_local_fixtures": true
}
```

## Architecture

```
User task + permissions
      -> Task Planner (bounded step plan)
      -> Policy Validator (domain/action allowlists, SSRF blocks, approval gate)
      -> Executor (steps, retries, timeouts, budget, cancellation)
      -> Local HTTP Worker (deterministic fixture-page driver)
      -> Evidence Extractor (per-field evidence)
      -> Schema Validator (required-field checks)
      -> Human Review (approval gate for consequential actions)
      -> Structured Result + Replay
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## Honest status (v0.1.0)

This is a **reference implementation** of the GHOST core, built to be
runnable, tested and auditable — not a production browser platform.

What genuinely works:

- Job schema, policy validator, bounded planner, executor with retries,
  timeouts, step budget and cancellation.
- Evidence extraction with per-field evidence and schema validation.
- Job replay that re-runs captured steps and compares evidence.
- A 14-task deterministic benchmark with completion rate, correct-field
  rate, duration stats and policy-violation results (all passing).

What is simplified or not yet built:

- **The worker is a deterministic local HTTP driver, not a real browser.**
  The production worker uses Playwright / Browser Use. This build fetches
  fixture pages with urllib and parses them with `html.parser` — so it is
  fully offline and repeatable, but produces text snapshots instead of
  screenshots and cannot run JavaScript.
- No dashboard, queue (Redis), PostgreSQL storage, containers, or
  OpenTelemetry — the architecture doc describes them as the production
  path; the core here is a single-process Python package.
- The fixture server (`127.0.0.1`) is reachable only when a job explicitly
  opts in with `allow_local_fixtures=True`. Production policy blocks
  localhost unconditionally.
- No anti-bot evasion, CAPTCHA bypass, or credential handling — by design,
  and never planned.

## Benchmark (local fixture pages, 14 tasks)

| Metric | Result |
|---|---|
| Task completion rate | 100% |
| Correct-field extraction rate | 100% |
| Policy-violation tests | 2/2 passed (disallowed domain, metadata endpoint) |
| Median / p95 duration | see `python -m ghost benchmark` |

Do not quote an "autonomous success rate" without the task set, test date
and definition of success — the benchmark command prints all three.

## Evaluation plan

- Extend the fixture set toward the spec's 20–30 tasks, including
  multi-page flows and adversarial pages.
- Swap the local worker for a Playwright worker behind the same executor
  interface and re-run the benchmark unchanged.
- Add the approval-gate UI, queue, and persistent job store per
  [ARCHITECTURE.md](ARCHITECTURE.md).

## Safety

GHOST refuses by default. It will not visit a domain you did not approve,
run an action you did not allow, submit anything without a human saying yes,
or follow instructions it finds inside a web page. If you need a workflow
that requires credentials, use scoped secrets injected at runtime — never
paste secrets into a job objective.

## License

MIT. See [LICENSE](LICENSE).
