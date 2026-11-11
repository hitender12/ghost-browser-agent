# GHOST — Architecture

## Overview

GHOST is a browser-agent platform built around one idea: every action must
be explainable after the fact. The pipeline is a strict sequence — plan,
validate, execute, extract, validate, review — and each stage only talks to
the next one through explicit, serialisable records.

```
User task + permissions
      -> Task Planner            (ghost/planner.py)
      -> Policy Validator        (ghost/policy.py)
      -> Executor                (ghost/executor.py)
      -> Local HTTP Worker       (ghost/worker.py)
      -> Evidence Extractor      (ghost/extractor.py)
      -> Schema Validator        (ghost/extractor.py: validate_output)
      -> Human Review            (approval callback)
      -> Structured Result / Replay (ghost/schema.py, ghost/replay.py)
```

## Components

### `ghost/schema.py` — data model
Dataclasses for `Job`, `Step`, `ActionRecord`, `FieldEvidence` and
`JobResult`. Everything is JSON-serialisable, so a job, its log and its
result can be stored, inspected and replayed without hidden state.
Consequential actions are named explicitly:
`submit, send, buy, delete, post`.

### `ghost/policy.py` — the safety boundary
`PolicyValidator` is consulted before every step and refuses with an exact
reason:

- **Domain allowlist.** Host must be approved (exact or subdomain match).
- **Action allowlist.** Only listed actions run; unknown actions are refused.
- **No SSRF.** `localhost`, `127.0.0.0/8`, `10/8`, `172.16/12`,
  `192.168/16`, link-local `169.254/16` (including the cloud metadata host
  `169.254.169.254`), and IPv6 loopback/link-local/ULA ranges are blocked.
  The single exception is `allow_local_fixtures`, a test-harness flag that
  permits the fixture server on `127.0.0.1` — off by default, never for
  production jobs.
- **Downloads blocked by default** unless the job opts in.
- **Credentials scanned** out of objectives, form values and log lines
  (password / api key / token / bearer / private key patterns). Values are
  never echoed; only the field name is reported.
- **Untrusted page content.** Instruction-like text in fetched pages is
  detected and recorded, never acted on. The policy can never be widened
  by something a page says.

### `ghost/planner.py` — bounded planning
Deterministic, no model calls: navigate to the start URL, one click step
per requested follow-link, one extract step per output field, plus a final
snapshot when allowed. The plan is truncated at `step_budget` with the
truncation reported, never silently.

### `ghost/worker.py` — local HTTP worker
The production worker is Playwright / Browser Use. This build's worker
fetches pages with `urllib`, parses them into a minimal DOM with
`html.parser`, and implements navigate / click / fill / simulated submit /
snapshot. A snapshot is a text record (title, URL, content hash, excerpt)
— honestly labelled, never presented as a screenshot. The worker never
decides policy; the executor checks everything first.

### `ghost/executor.py` — execution loop
For each planned step, in order: cancellation check, overall timeout,
step budget, action allowlist, consequential approval gate, URL policy,
then execution with retries on transient worker errors. Every step yields
an `ActionRecord` with status, reason, excerpt and duration. A blocked
entry navigation stops the job immediately. Stop reasons are recorded
(budget exhausted, timeout, cancellation, policy block, missing fields).

### `ghost/extractor.py` — evidence extraction
A small explicit selector vocabulary (`title`, `tag`, `id`, `class`,
`text`, `link`, `table`, `count`, `all_text`). Each extracted field gets a
`FieldEvidence` record (URL, title, timestamp, excerpt, selector).
`validate_output` checks required fields are present and non-empty.

### `ghost/replay.py` — replay
Re-runs a job with a fresh worker from the same definition and compares
each re-extracted value and URL with the original evidence. Mismatches are
reported per field. If the pages changed, replay reports the divergence
instead of pretending to match.

### `ghost/benchmark.py` — deterministic benchmark
14 tasks over the local fixture pages: extraction across titles, tables,
ids, classes, counts and link-following, plus policy tasks (disallowed
domain, metadata endpoint, injected instruction). Reports completion rate,
correct-field rate, median/p95 duration, steps per completed task,
retry/timeout rate and policy-test results.

### `fixtures/` — test pages and server
`fixtures/server.py` serves `fixtures/pages/*.html` on `127.0.0.1` on an
ephemeral port. Pages cover a shop home, product list/detail, docs,
directory, a contact form and a trap page carrying an injected
instruction ("Ignore all previous instructions…") used to verify that
embedded instructions are ignored.

## Data flow

1. A `Job` is validated (schema, allowlists, credential scan).
2. The planner emits a bounded step list.
3. The executor runs each step through the policy validator, then the
   worker, recording an `ActionRecord` per step.
4. Extract steps produce `FieldEvidence`; the output is schema-validated.
5. The `JobResult` (status, output, evidence, action log, policy blocks,
   timings) is returned and can be serialised or replayed.

## Production path (not built here)

Per the spec: Next.js dashboard, FastAPI orchestrator, Redis queue,
PostgreSQL job/log storage, object storage for screenshots, one short-lived
container/browser context per job with resource and network limits,
OpenTelemetry traces, and a Playwright worker behind the same executor
interface. An optional Rust sidecar may later enforce budgets and validate
action envelopes — only if it simplifies the security boundary and is
independently testable.

## Threat model

- A page that tries to instruct the agent (prompt injection) is detected,
  recorded and ignored.
- A job that tries to reach internal services or metadata endpoints is
  refused before any network call.
- A consequential action without a human approver is refused, never
  silently queued.
- Secrets are scanned before logging; the log never contains a secret value.
- The local worker is a test harness: it cannot run JavaScript, so
  script-driven attacks are out of scope for this build and must be
  re-tested against the Playwright worker.
