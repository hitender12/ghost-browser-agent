"""Job replay: re-run a job from its captured steps and compare evidence.

Replay rebuilds a fresh worker, re-executes the recorded navigate/click/
extract steps in order, and compares each re-extracted value with the
original evidence. A replay matches when every field reproduces the same
value from the same URL. Mismatches are reported per field — replay is a
debugging tool, not a guarantee: if the underlying pages changed, the
replay honestly reports the divergence instead of pretending to match.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, List, Optional

from .executor import Executor
from .executor_types import CancelToken
from .schema import Job, JobResult
from .worker import LocalHttpWorker


@dataclass
class FieldReplay:
    field: str
    original_value: Any
    replayed_value: Any
    matched: bool
    original_url: str
    replayed_url: str


@dataclass
class ReplayReport:
    job_id: str
    matched: bool
    fields: List[FieldReplay] = dc_field(default_factory=list)
    replay_status: str = ""
    notes: List[str] = dc_field(default_factory=list)


def replay_job(
    job: Job,
    original: JobResult,
    worker_factory: Optional[Callable[[], LocalHttpWorker]] = None,
    approve: Optional[Callable[[str, str], bool]] = None,
) -> ReplayReport:
    """Re-run ``job`` with a fresh worker and compare against ``original``."""
    factory = worker_factory or (lambda: LocalHttpWorker(job.per_step_timeout_seconds))
    executor = Executor(job, worker=factory(), approve=approve, cancel=CancelToken())
    replayed = executor.run()

    notes: List[str] = []
    if replayed.status in ("blocked", "failed"):
        notes.append(f"replay run ended {replayed.status}: {replayed.stop_reason}")

    original_by_field = {e.field: e for e in original.evidence}
    replayed_by_field = {e.field: e for e in replayed.evidence}

    fields: List[FieldReplay] = []
    all_matched = True
    for name, orig in original_by_field.items():
        rep = replayed_by_field.get(name)
        if rep is None:
            fields.append(FieldReplay(name, orig.value, None, False, orig.url, ""))
            all_matched = False
            continue
        matched = rep.value == orig.value and rep.url == orig.url
        if not matched:
            all_matched = False
        fields.append(FieldReplay(name, orig.value, rep.value, matched, orig.url, rep.url))

    for name in replayed_by_field:
        if name not in original_by_field:
            notes.append(f"replay produced extra field {name!r} not in original")
            all_matched = False

    return ReplayReport(
        job_id=job.job_id,
        matched=all_matched,
        fields=fields,
        replay_status=replayed.status,
        notes=notes,
    )
