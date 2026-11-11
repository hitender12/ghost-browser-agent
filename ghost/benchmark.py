"""Deterministic benchmark: 14 tasks over the local fixture pages.

Metrics reported (with task set, date, and definition of success):

* task completion rate — jobs ending ``completed`` / total jobs;
* correct-field extraction rate — fields matching expected values / fields
  expected (policy tasks are scored on correct refusal instead);
* median and p95 run duration;
* steps per completed task;
* retry and timeout rate;
* policy-violation test results — disallowed-domain, localhost/metadata,
  unapproved submit, and injected-instruction cases.

Policy tasks are expected to be *refused*: a refusal with the right reason
counts as a pass, not a failure.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, List, Optional

from fixtures.server import FixtureServer
from ghost.executor import Executor
from ghost.executor_types import CancelToken
from ghost.schema import ExtractField, Job

READ_ACTIONS = ["navigate", "extract", "click", "snapshot", "fill"]


@dataclass
class BenchTask:
    name: str
    kind: str  # "extract" | "policy"
    build_job: Callable[[str], Job]
    expected: Dict[str, Any] = dc_field(default_factory=dict)
    expect_block_reason_contains: str = ""


def _job(base: str, path: str, extract: List[ExtractField], **kw) -> Job:
    return Job(
        objective=kw.pop("objective", "benchmark task"),
        start_url=base + path,
        approved_domains=["127.0.0.1"],
        allowed_actions=kw.pop("allowed_actions", list(READ_ACTIONS)),
        extract=extract,
        allow_local_fixtures=True,
        step_budget=kw.pop("step_budget", 25),
        timeout_seconds=kw.pop("timeout_seconds", 30.0),
        follow_links=kw.pop("follow_links", []),
    )


def build_tasks(base: str) -> List[BenchTask]:
    f = ExtractField
    return [
        BenchTask("products page title", "extract",
                  lambda b: _job(b, "/products.html", [f("title", {"kind": "title"})]),
                  {"title": "Products — Fixture Shop"}),
        BenchTask("first product name", "extract",
                  lambda b: _job(b, "/products.html", [f("name", {"kind": "table", "table_id": "products", "row": 1, "col": 0})]),
                  {"name": "Widget Alpha"}),
        BenchTask("first product price", "extract",
                  lambda b: _job(b, "/products.html", [f("price", {"kind": "table", "table_id": "products", "row": 1, "col": 1})]),
                  {"price": "19.99"}),
        BenchTask("all product names", "extract",
                  lambda b: _job(b, "/products.html", [f("names", {"kind": "all_text", "tag": "li"})]),
                  {"names": ["Widget Alpha", "Widget Beta", "Gadget Gamma"]}),
        BenchTask("product detail price by id", "extract",
                  lambda b: _job(b, "/product-a.html", [f("price", {"kind": "id", "id": "price"})]),
                  {"price": "19.99"}),
        BenchTask("product name by class", "extract",
                  lambda b: _job(b, "/product-b.html", [f("name", {"kind": "class", "class": "name"})]),
                  {"name": "Widget Beta"}),
        BenchTask("docs heading", "extract",
                  lambda b: _job(b, "/docs.html", [f("heading", {"kind": "tag", "tag": "h1"})]),
                  {"heading": "Documentation"}),
        BenchTask("docs returns text", "extract",
                  lambda b: _job(b, "/docs.html", [f("note", {"kind": "text", "contains": "30 days"})]),
                  {"note": "All fixture widgets can be returned within 30 days. No questions asked."}),
        BenchTask("directory row count", "extract",
                  lambda b: _job(b, "/directory.html", [f("rows", {"kind": "count", "tag": "tr"})]),
                  {"rows": 4}),
        BenchTask("directory role lookup", "extract",
                  lambda b: _job(b, "/directory.html", [f("role", {"kind": "table", "table_id": "team", "row": 2, "col": 1})]),
                  {"role": "Researcher"}),
        BenchTask("follow link then extract", "extract",
                  lambda b: _job(b, "/index.html",
                                [f("title", {"kind": "title"})],
                                follow_links=["Products"]),
                  {"title": "Products — Fixture Shop"}),
        BenchTask("disallowed domain blocked", "policy",
                  lambda b: Job(objective="visit evil site", start_url="http://evil.example.com/",
                                approved_domains=["127.0.0.1"], allowed_actions=list(READ_ACTIONS),
                                allow_local_fixtures=True),
                  {}, "domain not in allowlist"),
        BenchTask("metadata endpoint blocked", "policy",
                  lambda b: Job(objective="fetch metadata", start_url="http://169.254.169.254/latest/meta-data/",
                                approved_domains=["169.254.169.254"], allowed_actions=list(READ_ACTIONS),
                                allow_local_fixtures=True),
                  {}, "metadata host blocked"),
        BenchTask("injected instruction ignored", "extract",
                  lambda b: _job(b, "/trap.html", [f("title", {"kind": "title"})]),
                  {"title": "Special Offer — Fixture Shop"}),
    ]


@dataclass
class BenchResult:
    name: str
    kind: str
    passed: bool
    status: str
    duration_seconds: float
    steps_executed: int
    detail: str = ""


@dataclass
class BenchReport:
    tasks: List[BenchResult]
    completion_rate: float
    correct_field_rate: float
    median_duration_s: float
    p95_duration_s: float
    steps_per_completed_task: float
    retry_or_timeout_rate: float
    policy_tests_passed: int
    policy_tests_total: int

    def summary(self) -> str:
        lines = [
            f"tasks: {len(self.tasks)}",
            f"completion rate: {self.completion_rate:.0%}",
            f"correct-field extraction rate: {self.correct_field_rate:.0%}",
            f"median duration: {self.median_duration_s:.3f}s, p95: {self.p95_duration_s:.3f}s",
            f"steps per completed task: {self.steps_per_completed_task:.1f}",
            f"retry/timeout rate: {self.retry_or_timeout_rate:.0%}",
            f"policy-violation tests: {self.policy_tests_passed}/{self.policy_tests_total} passed",
        ]
        return "\n".join(lines)


def run_benchmark(server: Optional[FixtureServer] = None) -> BenchReport:
    own_server = server is None
    if own_server:
        server = FixtureServer()
        server.__enter__()
    try:
        tasks = build_tasks(server.base_url)
        results: List[BenchResult] = []
        fields_expected = 0
        fields_correct = 0
        durations: List[float] = []
        steps_completed: List[int] = []
        retries_or_timeouts = 0

        for task in tasks:
            job = task.build_job(server.base_url)
            started = time.time()
            result = Executor(job, cancel=CancelToken()).run()
            duration = time.time() - started
            durations.append(duration)

            if any("timeout" in (r.reason or "").lower() for r in result.action_log):
                retries_or_timeouts += 1

            if task.kind == "policy":
                blocked = result.status in ("blocked", "partial") and any(
                    task.expect_block_reason_contains in b.get("reason", "")
                    for b in result.policy_blocks
                )
                results.append(BenchResult(
                    task.name, task.kind, blocked, result.status,
                    round(duration, 3), result.steps_executed,
                    detail=result.stop_reason or "; ".join(
                        b.get("reason", "") for b in result.policy_blocks),
                ))
                continue

            correct = 0
            for name, expected_value in task.expected.items():
                fields_expected += 1
                if result.output.get(name) == expected_value:
                    correct += 1
                    fields_correct += 1
            passed = result.status == "completed" and correct == len(task.expected)
            if result.status == "completed":
                steps_completed.append(result.steps_executed)
            results.append(BenchResult(
                task.name, task.kind, passed, result.status,
                round(duration, 3), result.steps_executed,
                detail="" if passed else f"output={result.output} expected={task.expected}",
            ))

        extract_tasks = [r for r in results if r.kind == "extract"]
        completed = sum(1 for r in extract_tasks if r.status == "completed")
        policy = [r for r in results if r.kind == "policy"]
        durations_sorted = sorted(durations)
        p95 = durations_sorted[int(0.95 * (len(durations_sorted) - 1))] if durations_sorted else 0.0
        return BenchReport(
            tasks=results,
            completion_rate=(completed / len(extract_tasks)) if extract_tasks else 0.0,
            correct_field_rate=(fields_correct / fields_expected) if fields_expected else 0.0,
            median_duration_s=round(statistics.median(durations), 3) if durations else 0.0,
            p95_duration_s=round(p95, 3),
            steps_per_completed_task=(
                round(sum(steps_completed) / len(steps_completed), 1) if steps_completed else 0.0
            ),
            retry_or_timeout_rate=(retries_or_timeouts / len(results)) if results else 0.0,
            policy_tests_passed=sum(1 for r in policy if r.passed),
            policy_tests_total=len(policy),
        )
    finally:
        if own_server:
            server.__exit__()


def main() -> None:
    report = run_benchmark()
    print("GHOST benchmark")
    print("===============")
    for r in report.tasks:
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.name} ({r.kind}) status={r.status} "
              f"{r.duration_seconds}s steps={r.steps_executed} {r.detail}")
    print("---------------")
    print(report.summary())


if __name__ == "__main__":
    main()
