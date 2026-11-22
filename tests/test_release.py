import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

from witness.cluster import cluster
from witness.engine import Unobserved, observe
from witness.report import validate

ROOT = Path(__file__).resolve().parents[1]
CLI = os.environ.get("WITNESS_CLI", str(ROOT/".venv/bin/pg-rollout-witness"))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)

    def run_cli(self, *args, code):
        result = subprocess.run([CLI, *args, "--out", self.tmp], cwd=self.tmp,
                                capture_output=True, text=True, timeout=140)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        data = json.loads((self.tmp/"report.json").read_text())
        validate(data, "report-v1.json")
        return {t["name"]: t for t in data["trials"]}

    def test_release_alternatives(self):
        trials = self.run_cli("demo", code=0)
        ordinary = trials["regular"]["events"]
        build = next(e for e in ordinary if e["kind"] == "migration")
        write = next(e for e in ordinary if e["name"] == "old.write-row-2")
        holder = next(e for e in ordinary if e["kind"] == "barrier")
        edges = {(e["pid"], b) for e in ordinary if e["kind"] == "wait" for b in e["blockers"]}
        self.assertIn((write["pid"], build["pid"]), edges)
        self.assertIn((build["pid"], holder["pid"]), edges)
        self.assertTrue(write["over_budget"])
        self.assertIsNone(write["sqlstate"])
        concurrent = trials["concurrent"]["events"]
        self.assertLess(next(i for i,e in enumerate(concurrent) if e["name"]=="old.write-row-2"),
                        next(i for i,e in enumerate(concurrent) if e["kind"]=="migration"))
        interrupted = {e["name"]: e for e in trials["interrupted"]["events"]}
        self.assertEqual(interrupted["captured blocker"]["phase"], "waiting for writers before build")
        self.assertEqual(interrupted["build unique index"]["sqlstate"], "57014")
        self.assertEqual(interrupted["retry identical migration"]["digest"], interrupted["build unique index"]["digest"])
        self.assertFalse(interrupted["after cancellation"]["valid"])
        self.assertFalse(interrupted["after cancellation"]["ready"])
        self.assertFalse(interrupted["after removal"]["exists"])
        self.assertTrue(interrupted["after completion"]["valid"])
        self.assertTrue(interrupted["after completion"]["ready"])
        self.assertTrue(interrupted["old.write-after-build"]["matched"])
        self.assertEqual(interrupted["duplicate email rejected"]["sqlstate"], "23505")
        states = {e['name']:e.get('sqlstate') for e in trials["rename"]["events"]}
        self.assertEqual(states["mixed.old-read"], "42703")
        self.assertIsNone(states["mixed.new-read"])
        self.assertTrue(all(e["matched"] for e in trials["expand-contract"]["events"] if e["kind"]=="request"))

    def test_custom_release_gate(self):
        args = ["run", "--fixture", ROOT/"examples/fixture.sql", "--workload", ROOT/"examples/workload.json",
                "--migration"]
        self.run_cli(*args, ROOT/"examples/migration.sql", code=0)
        self.assertNotIn("synthetic", (self.tmp/"report.json").read_text())
        migration = self.tmp/"change.sql"
        migration.write_text("ALTER TABLE orders RENAME COLUMN reference TO retired")
        result = self.run_cli(*args, migration, code=1)["custom"]
        self.assertEqual(result["verdict"], "application_failure")
        self.assertIn("42703", [e.get("sqlstate") for e in result["events"]])
        migration.write_text("ALTER TABLE absent ADD COLUMN broken int")
        self.assertEqual(self.run_cli(*args, migration, code=1)["custom"]["verdict"], "migration_failure")
        migration.write_text("CREATE ROLE cannot_create_roles")
        self.assertEqual(self.run_cli(*args, migration, code=1)["custom"]["events"][-1]["sqlstate"], "42501")

    def test_unavailable_observation_and_cleanup(self):
        with cluster() as (connect, admin):
            data = Path(admin.execute("SHOW data_directory").fetchone()[0])
            with self.assertRaises(Unobserved):
                observe([], admin, connect().info.backend_pid, admin.info.backend_pid, timeout=.1)
        self.assertFalse(data.exists())
        # Exercise the same deadline through the CLI verdict and report path.
        from unittest.mock import patch
        from witness.cli import main
        with patch("witness.trials.observe", side_effect=Unobserved("required phase absent")), \
             patch("sys.argv", ["witness","demo","--scenario","index","--out",str(self.tmp)]):
            self.assertEqual(main(), 3)
        report = json.loads((self.tmp/"report.json").read_text())
        self.assertTrue(all(t["verdict"]=="inconclusive" for t in report["trials"]))

    def test_sigterm_removes_live_cluster(self):
        fixture = self.tmp/"slow.sql"
        fixture.write_text("SELECT pg_sleep(8)")
        command = [CLI, "run","--fixture",str(fixture),"--migration",str(ROOT/"examples/migration.sql"),
                   "--workload",str(ROOT/"examples/workload.json"),"--out",str(self.tmp)]
        previous = set(Path("/tmp").glob("rollout-*/data/postmaster.pid"))
        proc = subprocess.Popen(command, cwd=self.tmp)
        try:
            deadline = time.monotonic()+15
            while time.monotonic()<deadline:
                fresh = set(Path("/tmp").glob("rollout-*/data/postmaster.pid"))-previous
                if fresh and any("ready" in p.read_text() for p in fresh if p.exists()):
                    break
                time.sleep(.025)
            else:
                self.fail("server never reached ready state")
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=30),130)
            self.assertTrue(all(not p.parent.exists() for p in fresh))
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=30)
