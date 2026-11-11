"""Task planner: turns a job into a bounded, ordered step plan.

The planner is deliberately explicit and deterministic: no hidden model
calls, no unbounded loops. It emits at most ``job.step_budget`` steps:

1. navigate to the start URL;
2. one click step per requested follow-link;
3. one extract step per requested output field.

If the plan would exceed the budget it is truncated and the truncation is
reported to the caller, so a job never silently drops work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from .schema import Job, Step


@dataclass
class Plan:
    steps: List[Step]
    truncated: bool
    truncated_reason: str = ""


def plan_job(job: Job) -> Plan:
    steps: List[Step] = []
    index = 0

    steps.append(Step(index=index, action="navigate", target=job.start_url))
    index += 1

    for link_text in job.follow_links:
        steps.append(
            Step(index=index, action="click", target=link_text,
                 detail={"kind": "link_text"})
        )
        index += 1

    for field in job.extract:
        steps.append(
            Step(
                index=index,
                action="extract",
                target=field.name,
                detail={"selector": field.selector, "required": field.required},
            )
        )
        index += 1

    # Final snapshot step, only when the job allows it: a visible checkpoint
    # of the last page visited.
    if "snapshot" in job.allowed_actions:
        steps.append(Step(index=index, action="snapshot", target="final page"))
        index += 1

    if len(steps) > job.step_budget:
        kept = steps[: job.step_budget]
        return Plan(
            steps=kept,
            truncated=True,
            truncated_reason=(
                f"plan of {len(steps)} steps exceeded step budget "
                f"{job.step_budget}; kept first {job.step_budget}"
            ),
        )
    return Plan(steps=steps, truncated=False)


def validate_plan(plan: Plan, job: Job) -> List[str]:
    """Check the plan against the job policy before execution."""
    problems = []
    for step in plan.steps:
        if step.action not in job.allowed_actions:
            problems.append(f"step {step.index}: action {step.action!r} not allowed")
    return problems
