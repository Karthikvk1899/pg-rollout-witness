from concurrent.futures import ThreadPoolExecutor, TimeoutError

from .engine import (BUDGET, NEW, OLD, WRITE, catalog, observe, require,
                     statement, verdict)

INDEX = "CREATE UNIQUE INDEX {}accounts_email_uq ON accounts(email)"
RENAME = "ALTER TABLE accounts RENAME COLUMN nickname TO display_name"
EXPAND = """ALTER TABLE accounts ADD COLUMN display_name text;
UPDATE accounts SET display_name=nickname"""
CONTRACT = "ALTER TABLE accounts DROP COLUMN nickname"


def index_trial(events, connect, admin, style):
    holder = connect(label="old-open-write")
    migrator = connect(label="build")
    client = connect(label="old-next-write")
    catalog(events, migrator, "before")
    holder.execute("BEGIN")
    holder.execute("UPDATE accounts SET nickname='busy' WHERE id=1")
    events.append(dict(kind="barrier", name="old transaction wrote row 1", pid=holder.info.backend_pid))
    concurrent = style != "regular"
    sql = INDEX.format("CONCURRENTLY " if concurrent else "")
    with ThreadPoolExecutor(max_workers=2) as pool:
        build = pool.submit(statement, events, migrator, "migration", "build unique index", sql)
        try:
            observe(events, admin, migrator.info.backend_pid, holder.info.backend_pid,
                    "waiting for writers before build" if concurrent else None)
            if style == "interrupted":
                sent = admin.execute("SELECT pg_cancel_backend(%s)", (migrator.info.backend_pid,)).fetchone()[0]
                require(sent and build.result(timeout=5)["sqlstate"] == "57014", "cancellation not confirmed")
            else:
                writer = pool.submit(statement, events, client, "request", "old.write-row-2",
                                     WRITE, expect=2, budget=BUDGET)
                if concurrent:
                    writer.result(timeout=5)
                else:
                    observe(events, admin, client.info.backend_pid, migrator.info.backend_pid)
                    # Hold the contention window through the declared service budget.
                    try:
                        writer.result(timeout=BUDGET / 1000)
                    except TimeoutError:
                        pass
        finally:
            holder.execute("ROLLBACK")
        built = build.result(timeout=5)
        if style == "regular":
            # Propagate a worker disconnect even if the budget timer expired first.
            writer.result(timeout=5)
    if style == "interrupted":
        residue = catalog(events, client, "after cancellation")
        require(residue["exists"] and residue["valid"] is False, "invalid residue not established")
        drop = statement(events, migrator, "migration", "remove invalid index",
                         "DROP INDEX CONCURRENTLY accounts_email_uq")
        if drop["sqlstate"]:
            return "migration_failure"
        require(not catalog(events, client, "after removal")["exists"], "removal not established")
        built = statement(events, migrator, "migration", "retry identical migration", sql)
    if built["sqlstate"]:
        return "migration_failure"
    final = catalog(events, client, "after completion")
    require(all(final[k] for k in ("exists", "ready", "valid", "unique")), "final catalog check failed")
    statement(events, client, "request", "old.write-after-build", WRITE, expect=2)
    statement(events, client, "request", "new.lookup-email",
              "SELECT count(*) FROM accounts WHERE email=(SELECT email FROM accounts WHERE id=1)", expect=1)
    duplicate = statement(events, client, "control", "duplicate email rejected",
                          "INSERT INTO accounts SELECT 9000,email,'duplicate' FROM accounts WHERE id=1")
    require(duplicate["sqlstate"] == "23505", "unique constraint rejection not observed")
    if style == "interrupted":
        outcomes = verdict([e for e in events if not (e["kind"] == "migration" and e["name"] == "build unique index")])
        return "recovered" if outcomes == "pass" else outcomes
    return verdict(events)


def columns(events, connect, style):
    ddl, old, new = connect(label="migration"), connect(label="old"), connect(label="new")
    statement(events, old, "request", "baseline.old-read", OLD, expect="member")

    def migrate(name, sql):
        return statement(events, ddl, "migration", name, sql)["sqlstate"] is None

    if style == "rename":
        if migrate("rename immediately", RENAME):
            statement(events, old, "request", "mixed.old-read", OLD, expect="member")
            statement(events, new, "request", "mixed.new-read", NEW, expect="member")
    elif migrate("expand and backfill", EXPAND):
        statement(events, old, "request", "mixed.old-read", OLD, expect="member")
        statement(events, new, "request", "mixed.new-read", NEW, expect="member")
        events.append(dict(kind="stage", name="old read-only client retired"))
        if migrate("contract after retirement", CONTRACT):
            statement(events, new, "request", "new-only.read", NEW, expect="member")
    return verdict(events)


def custom(events, connect, migration, workload):
    conn = connect(label="migration")
    for stage in ["before", "after"]:
        if stage == "after":
            if statement(events, conn, "migration", "supplied migration", migration)["sqlstate"]:
                break
        for case in workload["requests"]:
            if case["stage"] == stage:
                client = connect(label=case["client"])
                try:
                    statement(events, client, "request", f'{stage}.{case["client"]}.{case["name"]}',
                              case["sql"], params=case.get("params"), expect=case.get("expect", ...),
                              budget=case.get("budget_ms"))
                finally:
                    client.close()
        if verdict(events) != "pass":
            break
    return verdict(events)
