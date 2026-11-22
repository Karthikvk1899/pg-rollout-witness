import argparse
import json
import os
import platform
import signal
import subprocess
import sys
from concurrent.futures import TimeoutError
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import psycopg

from .cluster import cluster
from .engine import BUDGET, FIXTURE, OBSERVE, Unobserved, digest
from .report import save, validate
from .trials import columns, custom, index_trial

DECISIONS = {
    "pass": "The declared requests completed within their budgets and matched their assertions.",
    "migration_failure": "Hold this migration. Inspect the migration SQLSTATE and the last completed operation.",
    "application_failure": "Hold this rollout while the affected client is serving traffic.",
    "budget_violation": "Review the affected request against its declared service budget before approving.",
    "recovered": "Cancellation left an invalid index. Removal and retry restored uniqueness and the declared requests.",
    "inconclusive": "Required evidence was unavailable. Repeat this trial before making a release decision.",
}


def execute(args):
    # libpq accepts implicit service files, passwords and connection settings via PG*.
    for key in list(os.environ):
        if key.startswith("PG"):
            os.environ.pop(key)
    fixture = FIXTURE
    workload_version = "builtin/1"
    workload_hash = digest(Path(__file__).with_name("trials.py").read_text()+Path(__file__).with_name("engine.py").read_text())
    if args.command == "run":
        fixture = args.fixture.read_text()
        migration = args.migration.read_text()
        raw = args.workload.read_text()
        workload = json.loads(raw)
        validate(workload, "workload-v1.json")
        workload_hash = digest(raw)
        workload_version = "custom/1"
        names = ["custom"]
    else:
        names = []
        if args.scenario in ("all", "index"):
            names += ["regular", "concurrent", "interrupted"]
        if args.scenario in ("all", "columns"):
            names += ["rename", "expand-contract"]
    report = dict(schema_version=1, environment=dict(seed=args.seed, fixture_sha256=digest(fixture),
                  workload_version=workload_version, workload_sha256=workload_hash,
                  platform=f"{platform.system()} {platform.release()} {platform.machine()}",
                  python=platform.python_version(), runner="0.1.0", statement_timeout_ms=20000,
                  observation_timeout_s=OBSERVE, index_writer_budget_ms=BUDGET,
                  started_at=datetime.now(timezone.utc).isoformat()), trials=[])
    for name in names:
        events = []
        version = "unavailable"
        detail = ""
        try:
            with cluster() as (connect, admin):
                version = admin.execute("SHOW server_version").fetchone()[0]
                owner = connect(label="fixture")
                owner.execute("SELECT set_config('witness.seed',%s,false)", (str(args.seed),))
                owner.execute(fixture)
                if name == "custom":
                    status = custom(events, connect, migration, workload)
                elif name in ("regular", "concurrent", "interrupted"):
                    status = index_trial(events, connect, admin, name)
                else:
                    status = columns(events, connect, name)
        except (Unobserved, TimeoutError, psycopg.Error, subprocess.SubprocessError, OSError, RuntimeError) as exc:
            status = "inconclusive"
            # PostgreSQL details can contain fixture values. Only include vetted diagnostic categories.
            detail = " " + (str(exc) if isinstance(exc, Unobserved) else type(exc).__name__)
        report["trials"].append(dict(name=name, events=events, postgres=version, verdict=status,
                                     decision=DECISIONS[status]+detail))
    save(report, args.out)
    print(json.dumps({"report": str(args.out/"report.json"),
                      "trials": {t["name"]: t["verdict"] for t in report["trials"]}}))
    statuses = [t["verdict"] for t in report["trials"]]
    if "inconclusive" in statuses:
        return 3
    if args.command == "demo":
        expected = dict(regular="budget_violation", concurrent="pass", interrupted="recovered",
                        rename="application_failure", **{"expand-contract": "pass"})
        return int(any(t["verdict"] != expected[t["name"]] for t in report["trials"]))
    return int(statuses != ["pass"])


def main():
    parser = argparse.ArgumentParser(prog="pg-rollout-witness")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="compare the built-in release alternatives")
    demo.add_argument("--scenario", choices=["all", "index", "columns"], default="all")
    run = commands.add_parser("run", help="execute a fixture, migration and versioned workload")
    for opt in ["migration", "fixture", "workload"]:
        run.add_argument("--"+opt, type=Path, required=True)
    for sub in [run, demo]:
        sub.add_argument("--out", type=Path, default=Path("out"))
        sub.add_argument("--seed", type=int, default=41)
    args = parser.parse_args()

    def interrupt(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        return execute(args)
    except KeyboardInterrupt:
        return 130
    except (jsonschema.ValidationError, ValueError, OSError):
        print("Input or output error. Check paths, UTF-8 encoding and workload-v1.json.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
