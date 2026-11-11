"""Local HTTP worker: the deterministic browser replacement for this build.

The production GHOST worker uses Playwright / Browser Use against a real
browser. This build ships a deterministic local worker instead: it fetches
pages over plain HTTP with urllib, parses them into a small DOM, and exposes
the same step vocabulary (navigate / click / extract / snapshot / fill).
It drives the local fixture pages served by ``fixtures/server.py`` and is
fully offline and repeatable.

A real browser would also produce screenshots; here ``snapshot`` returns a
text snapshot (title, URL, content hash, excerpt) — honestly labelled as a
snapshot, never as a screenshot.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Minimal DOM
# ---------------------------------------------------------------------------

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class Node:
    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["Node"] = None):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: List["Node"] = []
        self.text_parts: List[str] = []

    @property
    def text(self) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            parts.append(child.text)
        return " ".join(p for p in parts if p).strip()

    def own_text(self) -> str:
        return " ".join(p for p in self.text_parts if p).strip()

    def find_all(self, tag: Optional[str] = None, id_: Optional[str] = None,
                 class_: Optional[str] = None) -> List["Node"]:
        out: List["Node"] = []
        tag_ok = tag is None or self.tag == tag
        id_ok = id_ is None or self.attrs.get("id") == id_
        class_ok = True
        if class_ is not None:
            class_ok = class_ in self.attrs.get("class", "").split()
        if tag_ok and id_ok and class_ok and self.tag != "#root":
            out.append(self)
        for child in self.children:
            out.extend(child.find_all(tag, id_, class_))
        return out

    def find_first(self, tag: Optional[str] = None, id_: Optional[str] = None,
                   class_: Optional[str] = None) -> Optional["Node"]:
        matches = self.find_all(tag, id_, class_)
        return matches[0] if matches else None


class _DOMParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), parent=self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, dict(attrs), parent=self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag):
        node = self.current
        while node is not None and node.tag != tag and node.tag != "#root":
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent

    def handle_data(self, data):
        text = data.strip()
        if text:
            self.current.text_parts.append(text)


def parse_html(html: str) -> Node:
    parser = _DOMParser()
    parser.feed(html)
    return parser.root


# ---------------------------------------------------------------------------
# Fetched page
# ---------------------------------------------------------------------------

class Page:
    def __init__(self, url: str, html: str, status: int, fetched_at: float):
        self.url = url
        self.html = html
        self.status = status
        self.fetched_at = fetched_at
        self.dom = parse_html(html)
        title_node = self.dom.find_first("title")
        self.title = title_node.text if title_node else ""
        self.text = self._visible_text()

    def _visible_text(self) -> str:
        # Exclude script/style content from visible text.
        parts: List[str] = []

        def walk(node: Node):
            if node.tag in ("script", "style"):
                return
            if node.tag not in ("#root",):
                own = node.own_text()
                if own:
                    parts.append(own)
            for child in node.children:
                walk(child)

        walk(self.dom)
        return " ".join(parts)

    def links(self) -> List[Dict[str, str]]:
        out = []
        for a in self.dom.find_all("a"):
            href = a.attrs.get("href", "")
            out.append({"text": a.text, "href": href})
        return out

    def snapshot(self) -> Dict[str, str]:
        digest = hashlib.sha256(self.html.encode("utf-8")).hexdigest()[:16]
        return {
            "url": self.url,
            "title": self.title,
            "content_sha256_16": digest,
            "excerpt": self.text[:300],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.fetched_at)),
        }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class WorkerError(Exception):
    pass


class LocalHttpWorker:
    """Fetches pages and performs read-only steps against them.

    The worker itself never decides policy: the executor checks every URL
    and action with the PolicyValidator before calling the worker.
    """

    def __init__(self, per_step_timeout_seconds: float = 10.0):
        self.timeout = per_step_timeout_seconds
        self.current: Optional[Page] = None
        self.history: List[str] = []

    def navigate(self, url: str) -> Page:
        request = urllib.request.Request(
            url, headers={"User-Agent": "GHOST/0.1 (local fixture worker)"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                html = raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            raise WorkerError(f"HTTP {exc.code} fetching {url}")
        except urllib.error.URLError as exc:
            raise WorkerError(f"network error fetching {url}: {exc.reason}")
        page = Page(url, html, status, time.time())
        self.current = page
        self.history.append(url)
        return page

    def click_link(self, link_text: str) -> Page:
        if self.current is None:
            raise WorkerError("no page loaded; navigate first")
        for link in self.current.links():
            if link["text"].strip().lower() == link_text.strip().lower():
                target = urllib.parse.urljoin(self.current.url, link["href"])
                return self.navigate(target)
        raise WorkerError(f"no link with text {link_text!r} on current page")

    def fill_form(self, form_id: str, values: Dict[str, str]) -> Dict[str, str]:
        """Simulate filling a form. Nothing is submitted (submit is a
        separate, approval-gated step). Returns the filled values."""
        if self.current is None:
            raise WorkerError("no page loaded; navigate first")
        form = self.current.dom.find_first("form", id_=form_id)
        if form is None:
            raise WorkerError(f"no form with id {form_id!r} on current page")
        known = {i.attrs.get("name") for i in form.find_all("input")}
        unknown = sorted(set(values) - known)
        if unknown:
            raise WorkerError(f"unknown form fields: {', '.join(unknown)}")
        return dict(values)

    def submit_form(self, form_id: str) -> str:
        """Simulated submission: records the intent only. The executor gates
        this behind human approval; the local worker never POSTs anywhere."""
        if self.current is None:
            raise WorkerError("no page loaded; navigate first")
        form = self.current.dom.find_first("form", id_=form_id)
        if form is None:
            raise WorkerError(f"no form with id {form_id!r} on current page")
        return f"simulated submit of form {form_id!r} on {self.current.url}"
