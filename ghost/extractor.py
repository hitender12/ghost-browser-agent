"""Evidence extractor: turns a fetched page into schema-validated output.

Every extracted field carries evidence: the URL, page title, timestamp,
excerpt, and the selector that produced it. Selectors are a small explicit
vocabulary (no arbitrary code execution):

* {"kind": "title"}                          -> page <title>
* {"kind": "tag", "tag": "h1"}               -> first matching tag's text
* {"kind": "id", "id": "price"}              -> element with that id
* {"kind": "class", "class": "name"}         -> first element with class
* {"kind": "text", "contains": "Total"}      -> first element containing text
* {"kind": "link", "text": "Products"}       -> href of that link
* {"kind": "table", "table_id": "t", "row": 1, "col": 2}
* {"kind": "count", "tag": "tr"}             -> number of matching elements
* {"kind": "all_text", "tag": "li"}          -> list of all matching texts

Output validation checks the job's required fields are present and
non-empty; missing required fields make the job partial, never silently
complete.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from .schema import ExtractField, FieldEvidence
from .worker import Page


class ExtractionError(Exception):
    pass


def _excerpt(page: Page, node_text: str, width: int = 160) -> str:
    text = node_text.strip()
    if len(text) > width:
        return text[:width] + "…"
    return text


def extract_one(page: Page, field: ExtractField) -> FieldEvidence:
    sel = field.selector
    kind = sel.get("kind")
    ts = time.time()

    def evidence(value: Any, selector_desc: str, excerpt_text: str) -> FieldEvidence:
        return FieldEvidence(
            field=field.name,
            value=value,
            url=page.url,
            page_title=page.title,
            timestamp=ts,
            excerpt=_excerpt(page, excerpt_text),
            selector=selector_desc,
        )

    if kind == "title":
        if not page.title:
            raise ExtractionError("page has no <title>")
        return evidence(page.title, "title", page.title)

    if kind == "tag":
        node = page.dom.find_first(sel.get("tag"))
        if node is None:
            raise ExtractionError(f"no <{sel.get('tag')}> element found")
        return evidence(node.text, f"tag:{sel.get('tag')}", node.text)

    if kind == "id":
        node = page.dom.find_first(id_=sel.get("id"))
        if node is None:
            raise ExtractionError(f"no element with id {sel.get('id')!r}")
        return evidence(node.text, f"id:{sel.get('id')}", node.text)

    if kind == "class":
        node = page.dom.find_first(class_=sel.get("class"))
        if node is None:
            raise ExtractionError(f"no element with class {sel.get('class')!r}")
        return evidence(node.text, f"class:{sel.get('class')}", node.text)

    if kind == "text":
        needle = (sel.get("contains") or "").lower()
        for node in page.dom.find_all():
            if needle and needle in node.own_text().lower():
                return evidence(node.own_text(), f"text contains:{sel.get('contains')}", node.own_text())
        raise ExtractionError(f"no element containing {sel.get('contains')!r}")

    if kind == "link":
        for link in page.links():
            if link["text"].strip().lower() == (sel.get("text") or "").strip().lower():
                return evidence(link["href"], f"link:{sel.get('text')}", link["text"])
        raise ExtractionError(f"no link with text {sel.get('text')!r}")

    if kind == "table":
        table = page.dom.find_first("table", id_=sel.get("table_id"))
        if table is None:
            raise ExtractionError(f"no table with id {sel.get('table_id')!r}")
        rows = table.find_all("tr")
        row_idx, col_idx = int(sel.get("row", 0)), int(sel.get("col", 0))
        if row_idx >= len(rows):
            raise ExtractionError(f"table has {len(rows)} rows, row {row_idx} out of range")
        cells = rows[row_idx].find_all("td") or rows[row_idx].find_all("th")
        if col_idx >= len(cells):
            raise ExtractionError(f"row {row_idx} has {len(cells)} cells, col {col_idx} out of range")
        value = cells[col_idx].text
        return evidence(value, f"table:{sel.get('table_id')}[{row_idx}][{col_idx}]", value)

    if kind == "count":
        nodes = page.dom.find_all(sel.get("tag"))
        return evidence(len(nodes), f"count:{sel.get('tag')}", f"{len(nodes)} elements")

    if kind == "all_text":
        nodes = page.dom.find_all(sel.get("tag"))
        values = [n.text for n in nodes if n.text]
        return evidence(values, f"all_text:{sel.get('tag')}", "; ".join(values)[:160])

    raise ExtractionError(f"unknown selector kind: {kind!r}")


def extract_all(page: Page, fields: List[ExtractField]) -> Tuple[Dict[str, Any], List[FieldEvidence], List[str]]:
    """Extract every field. Returns (output, evidence, errors)."""
    output: Dict[str, Any] = {}
    evidence: List[FieldEvidence] = []
    errors: List[str] = []
    for field in fields:
        try:
            ev = extract_one(page, field)
            output[field.name] = ev.value
            evidence.append(ev)
        except ExtractionError as exc:
            errors.append(f"{field.name}: {exc}")
    return output, evidence, errors


def validate_output(output: Dict[str, Any], fields: List[ExtractField]) -> List[str]:
    """Return a list of missing/invalid required fields (empty = valid)."""
    problems = []
    for field in fields:
        if not field.required:
            continue
        value = output.get(field.name)
        if value is None or (isinstance(value, str) and not value.strip()) or (
            isinstance(value, (list, dict)) and not value
        ):
            problems.append(f"required field missing or empty: {field.name}")
    return problems
