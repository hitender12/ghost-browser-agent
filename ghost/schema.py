"""Core data model for GHOST: jobs, steps, action logs, evidence, results.

Every record is a plain dataclass so a job, its log, and its result can be
serialised to JSON and replayed later without any hidden state.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Job definition
# ---------------------------------------------------------------------------

#: Actions that are always allowed (subject to domain policy).
READ_ACTIONS = frozenset({"navigate", "extract", "click", "snapshot", "fill"})

#: Consequential actions: externally visible or irreversible. These always
#: require explicit human approval, even when they appear in the allowlist.
CONSEQUENTIAL_ACTIONS = frozenset({"submit", "send", "buy", "delete", "post"})

#: Everything the executor knows how to do.
ALL_ACTIONS = READ_ACTIONS | CONSEQUENTIAL_ACTIONS | {"download"}


@dataclass
class ExtractField:
    """One output field to extract, with the selector used to find it."""

    name: str
    selector: Dict[str, Any]
    required: bool = True

    def describe(self) -> str:
        return f"{self.name} <- {json.dumps(self.selector, sort_keys=True)}"


@dataclass
class Job:
    """A bounded browser job submitted by a user.

    ``approved_domains`` is an explicit allowlist: the worker may only visit
    URLs whose host is on it. ``allowed_actions`` is a subset of ALL_ACTIONS;
    consequential actions additionally need ``approve`` to return True.
    ``allow_local_fixtures`` is a test-harness escape hatch: it permits the
    local fixture server (127.0.0.1) used by the benchmark and tests. It is
    False by default, and production policy must never enable it.
    """

    objective: str
    start_url: str
    approved_domains: List[str]
    allowed_actions: List[str]
    extract: List[ExtractField] = field(default_factory=list)
    step_budget: int = 25
    timeout_seconds: float = 60.0
    per_step_timeout_seconds: float = 10.0
    retry_limit: int = 2
    allow_downloads: bool = False
    allow_local_fixtures: bool = False
    follow_links: List[str] = field(default_factory=list)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["extract"] = [asdict(e) for e in self.extract]
        return d


# ---------------------------------------------------------------------------
# Steps, logs and evidence
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """One planned step in a bounded job plan."""

    index: int
    action: str
    target: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ActionRecord:
    """Structured log entry for one executed step.

    ``status`` is one of: ok, blocked, failed, skipped, cancelled.
    ``reason`` always explains a non-ok outcome.
    """

    step_index: int
    action: str
    url: str
    page_title: str
    timestamp: float
    status: str
    reason: str = ""
    excerpt: str = ""
    selector: str = ""
    attempts: int = 1
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp_iso"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)
        )
        return d


@dataclass
class FieldEvidence:
    """Evidence attached to one extracted output field."""

    field: str
    value: Any
    url: str
    page_title: str
    timestamp: float
    excerpt: str
    selector: str

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["timestamp_iso"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)
        )
        return d


@dataclass
class JobResult:
    """The complete, auditable outcome of one job run."""

    job_id: str
    status: str  # completed | blocked | failed | cancelled | partial
    output: Dict[str, Any]
    evidence: List[FieldEvidence]
    action_log: List[ActionRecord]
    policy_blocks: List[Dict[str, str]]
    started_at: float
    finished_at: float
    steps_planned: int
    steps_executed: int
    stop_reason: str = ""

    @property
    def duration_seconds(self) -> float:
        return round(self.finished_at - self.started_at, 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "output": self.output,
            "evidence": [e.to_dict() for e in self.evidence],
            "action_log": [a.to_dict() for a in self.action_log],
            "policy_blocks": self.policy_blocks,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "steps_planned": self.steps_planned,
            "steps_executed": self.steps_executed,
            "stop_reason": self.stop_reason,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)
