"""Policy validator: the safety boundary of GHOST.

The validator enforces, before any step executes:

* domain allowlist — only approved hosts may be visited;
* action allowlist — only approved actions may run;
* no localhost / cloud-metadata / private-network access, unless the job
  explicitly opts into the local-fixture test harness;
* downloads blocked by default;
* credentials never in prompts or logs (scanned, with findings reported);
* page content is untrusted data — the validator never lets page text widen
  the policy, and the executor ignores instructions found in page text;
* consequential actions (send / buy / delete / post / submit) require
  explicit human approval through a callback.

Every refusal returns a Decision carrying the exact reason, which the
executor records in the action log and the policy-block list.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Callable, List, Optional
from urllib.parse import urlparse

from .schema import ALL_ACTIONS, CONSEQUENTIAL_ACTIONS, Job

#: Hosts that must never be reached by the browser worker. 169.254.169.254
#: is the cloud metadata endpoint (AWS/GCP/Azure link-local metadata).
METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal"}

#: Private / reserved networks, checked with ipaddress.
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("0.0.0.0/8"),
]

#: Patterns that indicate a credential or secret in text. Used to scan job
#: objectives, prompts and log lines before they are stored.
CREDENTIAL_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(api[_-]?key|apikey)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(secret|token)\s*[:=]\s*[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9_\-\.]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(aws_secret_access_key|client_secret)\s*[:=]\s*\S+"),
]

#: Text that looks like an instruction embedded in page content. The executor
#: treats page text as data; if a fetched page contains one of these, the
#: executor records an untrusted-content note and does not act on it.
INSTRUCTION_PATTERNS = [
    re.compile(r"(?i)ignore (all )?(previous|prior|above) instructions"),
    re.compile(r"(?i)disregard (all )?(previous|prior|above) instructions"),
    re.compile(r"(?i)you are now (a|an|in)[^.]{0,80}"),
    re.compile(r"(?i)override (your|the) (system )?(instructions|policy|rules)"),
    re.compile(r"(?i)navigate to [^\s]+"),
    re.compile(r"(?i)visit (https?://\S+|localhost[^\s]*)"),
]


@dataclass
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class PolicyValidator:
    """Validates jobs, URLs and actions against the job's policy."""

    def __init__(self, job: Job, approve: Optional[Callable[[str, str], bool]] = None):
        self.job = job
        # approve(action, target) -> True if a human approved this action.
        self.approve = approve

    # -- job-level validation -------------------------------------------

    def validate_job(self) -> List[str]:
        """Return a list of job-definition problems (empty = valid)."""
        problems = []
        if not self.job.objective.strip():
            problems.append("job objective is empty")
        if not self.job.approved_domains:
            problems.append("approved_domains allowlist is empty")
        unknown = sorted(set(self.job.allowed_actions) - ALL_ACTIONS)
        if unknown:
            problems.append(f"unknown actions in allowlist: {', '.join(unknown)}")
        if self.job.step_budget < 1:
            problems.append("step_budget must be at least 1")
        if self.job.timeout_seconds <= 0:
            problems.append("timeout_seconds must be positive")
        if self.job.retry_limit < 0:
            problems.append("retry_limit must be >= 0")
        bad = scan_for_credentials(self.job.objective)
        if bad:
            problems.append(f"credential-like text in objective: {bad[0]}")
        return problems

    # -- URL validation ---------------------------------------------------

    def check_url(self, url: str) -> Decision:
        """Decide whether the worker may fetch this URL."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return Decision(False, f"unsupported URL scheme: {parsed.scheme!r}")
        host = (parsed.hostname or "").lower()
        if not host:
            return Decision(False, "URL has no host")
        if host in METADATA_HOSTS:
            return Decision(False, f"cloud metadata host blocked: {host}")
        if self._is_private_or_local(host):
            if host in ("127.0.0.1", "localhost", "::1") and self.job.allow_local_fixtures:
                pass  # explicit test-harness opt-in, recorded by the executor
            else:
                return Decision(
                    False,
                    f"private/localhost address blocked: {host}",
                )
        allowed = {d.lower() for d in self.job.approved_domains}
        if host not in allowed and not any(
            host == d or host.endswith("." + d) for d in allowed
        ):
            return Decision(False, f"domain not in allowlist: {host}")
        return Decision(True, "allowed")

    def _is_private_or_local(self, host: str) -> bool:
        if host in ("localhost",):
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(ip in net for net in _PRIVATE_NETWORKS)

    # -- action validation --------------------------------------------------

    def check_action(self, action: str, target: str = "") -> Decision:
        """Decide whether this action may run (before approval is consulted)."""
        if action not in ALL_ACTIONS:
            return Decision(False, f"unknown action: {action!r}")
        if action not in self.job.allowed_actions:
            return Decision(False, f"action not in allowlist: {action}")
        if action == "download" and not self.job.allow_downloads:
            return Decision(False, "downloads are blocked by default")
        return Decision(True, "allowed")

    def check_consequential(self, action: str, target: str = "") -> Decision:
        """Approval gate for consequential actions. Never auto-approves."""
        if action not in CONSEQUENTIAL_ACTIONS:
            return Decision(True, "not consequential")
        if self.approve is None:
            return Decision(
                False,
                f"human approval required for {action} and no approver is configured",
            )
        try:
            approved = bool(self.approve(action, target))
        except Exception as exc:  # approver failure = refuse
            return Decision(False, f"approver error for {action}: {exc}")
        if not approved:
            return Decision(False, f"human approval denied for {action}")
        return Decision(True, "human approval granted")

    # -- untrusted content ----------------------------------------------------

    def scan_page_text(self, text: str) -> List[str]:
        """Find instruction-like content in fetched page text.

        Page content is untrusted data. A match does not change policy; it is
        reported so the action log can record that embedded instructions were
        seen and ignored.
        """
        return sorted({m.group(0)[:80] for p in INSTRUCTION_PATTERNS for m in p.finditer(text)})


def scan_for_credentials(text: str) -> List[str]:
    """Return short descriptions of credential-like patterns found in text."""
    found = []
    for pattern in CREDENTIAL_PATTERNS:
        m = pattern.search(text or "")
        if m:
            snippet = m.group(0)
            # Never echo a secret value: keep only the field name.
            field = snippet.split(":", 1)[0].split("=", 1)[0].strip()
            found.append(field or "credential-like text")
    return found


def redact(text: str) -> str:
    """Redact credential-like values from text before it is logged."""
    redacted = text or ""
    for pattern in CREDENTIAL_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
