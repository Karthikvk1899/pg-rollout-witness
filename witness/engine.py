"""Record SQL outcomes and capture positive, PID-specific observations."""
import hashlib
import time

import psycopg

FIXTURE = """CREATE TABLE accounts(id integer PRIMARY KEY, email text NOT NULL, nickname text);
INSERT INTO accounts SELECT i, md5(i::text || current_setting('witness.seed')), 'member'
FROM generate_series(1, 2048) AS i"""
WRITE = "UPDATE accounts SET nickname='active' WHERE id=2 RETURNING id"
OLD = "SELECT nickname FROM accounts WHERE id=1"
NEW = "SELECT display_name FROM accounts WHERE id=1"
BUDGET = 1200
OBSERVE = 5.0


class Unobserved(Exception):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def statement(events, conn, kind, name, sql, *, expect=..., budget=None, params=None):
    start = time.monotonic()
    state = None
    matches = None
    try:
        cur = conn.execute(sql, params)
        if expect is not ...:
            row = cur.fetchone() if cur.description else None
            matches = row == (expect,)
    except psycopg.Error as exc:
        if not exc.sqlstate:
            raise
        state = exc.sqlstate
    elapsed = round((time.monotonic() - start) * 1000, 3)
    event = dict(kind=kind, name=name, pid=conn.info.backend_pid, sqlstate=state,
                 digest=digest(sql), elapsed_ms=elapsed, budget_ms=budget,
                 over_budget=budget is not None and elapsed > budget, matched=matches)
    events.append(event)
    return event


def observe(events, admin, pid, blocker, phase=None, timeout=OBSERVE):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = admin.execute("""
            SELECT pg_blocking_pids(a.pid), a.wait_event_type, a.wait_event, p.phase,
              ARRAY(SELECT mode FROM pg_locks WHERE pid=a.pid AND NOT granted)
            FROM pg_stat_activity a LEFT JOIN pg_stat_progress_create_index p USING(pid)
            WHERE a.pid=%s""", (pid,)).fetchone()
        if row and blocker in row[0] and (phase is None or phase == row[3]):
            events.append(dict(kind="wait", name="captured blocker", pid=pid, blockers=row[0],
                               wait_type=row[1], wait_event=row[2], phase=row[3], modes=row[4]))
            return
        time.sleep(.01)
    raise Unobserved("required blocker or progress phase was not sampled before the deadline")


def catalog(events, conn, name):
    row = conn.execute("""SELECT indisready, indisvalid, indisunique FROM pg_index
        WHERE indexrelid=to_regclass('rehearsal.accounts_email_uq')""").fetchone()
    event = dict(kind="catalog", name=name, exists=row is not None,
                 ready=row[0] if row else None, valid=row[1] if row else None,
                 unique=row[2] if row else None)
    events.append(event)
    return event


def require(condition, reason):
    if not condition:
        raise Unobserved(reason)


def verdict(events):
    if any(e['kind']=='migration' and e['sqlstate'] for e in events):
        return "migration_failure"
    if any(e['kind']=='request' and (e['sqlstate'] or e['matched'] is False) for e in events):
        return "application_failure"
    if any(e.get('over_budget') for e in events):
        return "budget_violation"
    return "pass"
