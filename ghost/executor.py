"""Executor: runs a planned job step by step, enforcing policy and budgets.

Order of checks for every step, before anything happens:

1. cancellation, overall timeout, step budget;
2. action allowlist (PolicyValidator.check_action);
3. consequential approval gate (PolicyValidator.check_consequential);
4. URL allowlist for navigate/click targets (PolicyValidator.check_url);
5. credential scan of anything about to be logged.

Every step — ok, blocked, failed or skipped — produces an ActionRecord
with the exact reason. Policy blocks are also collected separately so a
job result can show precisely what was refused and why. Page text is
treated as untrusted data: instruction-like content found in a fetched
page is recorded and ignored, never acted on.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Callable, Dict, List, Optional

from .executor_types import CancelToken
from .extractor import extract_one, validate_output
from .planner import plan_job, validate_plan
from .policy import PolicyValidator, redact, scan_for_credentials
from .schema import ActionRecord, FieldEvidence, Job, JobResult
from .worker import LocalHttpWorker, Page, WorkerError


class Executor:
    def __init__(
        self,
        job: Job,
        worker: Optional[LocalHttpWorker] = None,
        approve: Optional[Callable[[str, str], bool]] = None,
        cancel: Optional[CancelToken] = None,
    ):
        self.job = job
        self.worker = worker or LocalHttpWorker(job.per_step_timeout_seconds)
        self.policy = PolicyValidator(job, approve=approve)
        self.cancel = cancel or CancelToken()
        self.records: List[ActionRecord] = []
        self.policy_blocks: List[Dict[str, str]] = []
        self.evidence: List[FieldEvidence] = []
        self.output: Dict[str, object] = {}
        self._started_at = 0.0
        self._deadline = 0.0
        self._stop_reason = ""

    # -- public API -------------------------------------------------------

    def run(self) -> JobResult:
        self._started_at = time.time()
        self._deadline = self._started_at + self.job.timeout_seconds

        job_problems = self.policy.validate_job()
        if job_problems:
            return self._finish("blocked", "; ".join(job_problems), 0, 0)

        plan = plan_job(self.job)
        plan_problems = validate_plan(plan, self.job)
        if plan_problems:
            return self._finish("blocked", "; ".join(plan_problems), len(plan.steps), 0)
        if plan.truncated:
            self._stop_reason = plan.truncated_reason

        executed = 0
        for step in plan.steps:
            if self.cancel.is_cancelled():
                self._stop_reason = "cancelled by user"
                break
            if time.time() > self._deadline:
                self._stop_reason = (
                    f"job timeout after {self.job.timeout_seconds}s"
                )
                break
            if executed >= self.job.step_budget:
                self._stop_reason = f"step budget {self.job.step_budget} exhausted"
                break
            record = self._run_step(step)
            self.records.append(record)
            executed += 1
            if record.status == "blocked" and record.action in ("navigate",):
                # A blocked navigation means the rest of the plan cannot
                # proceed on this page; stop rather than guessing.
                self._stop_reason = f"stopped: {record.reason}"
                break

        status = self._derive_status()
        return self._finish(status, self._stop_reason, len(plan.steps), executed)

    # -- step execution -----------------------------------------------------

    def _run_step(self, step) -> ActionRecord:
        started = time.time()
        url = self.worker.current.url if self.worker.current else ""
        title = self.worker.current.title if self.worker.current else ""

        def record(status: str, reason: str = "", excerpt: str = "",
                   selector: str = "", attempts: int = 1) -> ActionRecord:
            return ActionRecord(
                step_index=step.index,
                action=step.action,
                url=url if self.worker.current is None else self.worker.current.url,
                page_title=self.worker.current.title if self.worker.current else "",
                timestamp=time.time(),
                status=status,
                reason=reason,
                excerpt=excerpt,
                selector=selector,
                attempts=attempts,
                duration_ms=round((time.time() - started) * 1000, 1),
            )

        # 1. action allowlist
        decision = self.policy.check_action(step.action, step.target)
        if not decision:
            self._block(step, decision.reason)
            return record("blocked", decision.reason)

        # 2. consequential approval gate
        if step.action in ("submit", "send", "buy", "delete", "post"):
            decision = self.policy.check_consequential(step.action, step.target)
            if not decision:
                self._block(step, decision.reason)
                return record("blocked", decision.reason)

        # 3. resolve the target URL for navigation-like steps
        target_url = ""
        if step.action == "navigate":
            target_url = step.target
        elif step.action == "click":
            target_url = self._resolve_link(step.target)
            if target_url is None:
                return record("failed", f"no link with text {step.target!r} on current page")

        # 4. URL policy for navigation
        if step.action in ("navigate", "click"):
            decision = self.policy.check_url(target_url)
            if not decision:
                self._block(step, decision.reason)
                return record("blocked", decision.reason)

        # 5. execute with retries on transient worker errors
        attempts = 0
        last_error = ""
        while attempts <= self.job.retry_limit:
            attempts += 1
            try:
                return self._execute(step, target_url, record, attempts)
            except WorkerError as exc:
                last_error = str(exc)
                if attempts > self.job.retry_limit:
                    break
                continue
        return record("failed", f"after {attempts} attempt(s): {last_error}", attempts=attempts)

    def _execute(self, step, target_url: str, record, attempts: int) -> ActionRecord:
        if step.action == "navigate":
            page = self.worker.navigate(target_url)
            self._scan_page(page)
            return record("ok", excerpt=page.text[:160], attempts=attempts)

        if step.action == "click":
            page = self.worker.navigate(target_url)
            self._scan_page(page)
            return record("ok", excerpt=page.text[:160], attempts=attempts)

        if step.action == "snapshot":
            page = self.worker.current
            if page is None:
                return record("failed", "no page loaded; navigate first")
            snap = page.snapshot()
            return record("ok", excerpt=snap["excerpt"], attempts=attempts)

        if step.action == "extract":
            page = self.worker.current
            if page is None:
                return record("failed", "no page loaded; navigate first")
            field = next(
                (f for f in self.job.extract if f.name == step.target), None
            )
            if field is None:
                return record("failed", f"unknown extract field {step.target!r}")
            try:
                ev = extract_one(page, field)
            except Exception as exc:
                return record("failed", f"extraction failed for {field.name}: {exc}")
            self.output[field.name] = ev.value
            self.evidence.append(ev)
            return record("ok", excerpt=ev.excerpt, selector=ev.selector, attempts=attempts)

        if step.action == "fill":
            detail = step.detail
            values = detail.get("values", {})
            if scan_for_credentials(" ".join(f"{k}={v}" for k, v in values.items())):
                self._block(step, "credential-like text in form values")
                return record("blocked", "credential-like text in form values")
            filled = self.worker.fill_form(step.target, values)
            return record("ok", excerpt=f"filled {len(filled)} field(s)", attempts=attempts)

        if step.action in ("submit", "send", "buy", "delete", "post"):
            result = self.worker.submit_form(step.target)
            return record("ok", excerpt=result, attempts=attempts)

        if step.action == "download":
            return record("blocked", "downloads are blocked by default")

        return record("failed", f"unsupported action {step.action!r}")

    # -- helpers --------------------------------------------------------------

    def _resolve_link(self, link_text: str) -> Optional[str]:
        page = self.worker.current
        if page is None:
            return None
        for link in page.links():
            if link["text"].strip().lower() == link_text.strip().lower():
                return urllib.parse.urljoin(page.url, link["href"])
        return None

    def _scan_page(self, page: Page) -> None:
        """Record instruction-like content found in page text; never act on it."""
        hits = self.policy.scan_page_text(page.text)
        if hits:
            self.policy_blocks.append({
                "type": "untrusted_page_content",
                "url": page.url,
                "reason": f"instruction-like text found and ignored: {hits[0]}",
            })

    def _block(self, step, reason: str) -> None:
        self.policy_blocks.append({
            "type": "policy_block",
            "step": str(step.index),
            "action": step.action,
            "target": redact(step.target),
            "reason": reason,
        })

    def _derive_status(self) -> str:
        if self.cancel.is_cancelled():
            return "cancelled"
        if any(r.status == "blocked" for r in self.records):
            # Blocked on the entry navigation = blocked; otherwise partial.
            if self.records and self.records[0].status == "blocked":
                return "blocked"
            return "partial"
        if any(r.status == "failed" for r in self.records):
            # Ran but something failed: partial if any step succeeded,
            # failed only when nothing at all succeeded.
            return "partial" if any(r.status == "ok" for r in self.records) else "failed"
        missing = validate_output(self.output, self.job.extract)
        if missing:
            return "partial"
        return "completed"

    def _finish(self, status: str, stop_reason: str, planned: int, executed: int) -> JobResult:
        finished = time.time()
        missing = validate_output(self.output, self.job.extract)
        if missing:
            if status == "completed":
                status = "partial"
            if not stop_reason:
                stop_reason = "; ".join(missing)
        return JobResult(
            job_id=self.job.job_id,
            status=status,
            output=dict(self.output),
            evidence=list(self.evidence),
            action_log=list(self.records),
            policy_blocks=list(self.policy_blocks),
            started_at=self._started_at,
            finished_at=finished,
            steps_planned=planned,
            steps_executed=executed,
            stop_reason=stop_reason,
        )
