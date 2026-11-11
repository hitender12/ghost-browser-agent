"""Demo: one bounded job over the local fixture pages, then a replay.

Run from the repo root:  python demo.py  or  python -m ghost demo
"""

from __future__ import annotations

from fixtures.server import FixtureServer
from ghost.executor import Executor
from ghost.replay import replay_job
from ghost.schema import ExtractField, Job


def run_demo() -> None:
    with FixtureServer() as server:
        base = server.base_url
        job = Job(
            objective="List the products and the price of Widget Alpha",
            start_url=base + "/products.html",
            approved_domains=["127.0.0.1"],
            allowed_actions=["navigate", "extract", "click", "snapshot"],
            extract=[
                ExtractField("page_title", {"kind": "title"}),
                ExtractField("first_product", {"kind": "table", "table_id": "products", "row": 1, "col": 0}),
                ExtractField("first_price", {"kind": "table", "table_id": "products", "row": 1, "col": 1}),
                ExtractField("all_names", {"kind": "all_text", "tag": "li"}),
            ],
            allow_local_fixtures=True,
        )
        result = Executor(job).run()
        print("== job result ==")
        print(result.to_json())
        print("\n== replay ==")
        report = replay_job(job, result)
        print(f"matched: {report.matched} (replay status: {report.replay_status})")
        for f in report.fields:
            mark = "OK " if f.matched else "DIFF"
            print(f"  [{mark}] {f.field}: {f.original_value!r} -> {f.replayed_value!r}")
        if report.notes:
            print("notes:", "; ".join(report.notes))


if __name__ == "__main__":
    run_demo()
