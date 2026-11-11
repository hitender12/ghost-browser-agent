"""GHOST CLI: run a job from a JSON file, or the demo / benchmark.

Usage:
    python -m ghost demo            run the demo job + replay + benchmark
    python -m ghost benchmark       run the deterministic benchmark
    python -m ghost run job.json    run a job described in JSON
"""

from __future__ import annotations

import json
import sys

from .demo import run_demo


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] == "demo":
        run_demo()
        return 0
    if argv[0] == "benchmark":
        from .benchmark import main as bench_main

        bench_main()
        return 0
    if argv[0] == "run" and len(argv) == 2:
        from .executor import Executor
        from .schema import ExtractField, Job

        with open(argv[1]) as fh:
            raw = json.load(fh)
        raw["extract"] = [ExtractField(**e) for e in raw.get("extract", [])]
        job = Job(**raw)
        result = Executor(job).run()
        print(result.to_json())
        return 0 if result.status == "completed" else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
