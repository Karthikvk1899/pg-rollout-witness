"""One authenticated loopback listener and one newly initialized PostgreSQL cluster per trial."""
import os
import secrets
import socket
import shutil
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from importlib.util import find_spec
from pathlib import Path

import psycopg
from psycopg import sql


def binaries():
    override = os.environ.get("WITNESS_PGBIN")
    if override:
        return Path(override).resolve()
    if shutil.which("initdb"):
        return Path(shutil.which("initdb")).parent
    spec = find_spec("pgserver")
    if spec:
        return Path(spec.origin).parent / "pginstall/bin"
    raise RuntimeError("Install PostgreSQL 16 and set WITNESS_PGBIN to its bin directory.")


@contextmanager
def cluster():
    bin_dir = binaries()
    with tempfile.TemporaryDirectory(prefix="rollout-", dir="/tmp") as tmp, ExitStack() as stack:
        data = Path(tmp, "data")
        env = {"PATH": os.defpath, "LC_ALL": "C", "HOME": tmp}

        def pg(program, *args):
            subprocess.run([bin_dir / program, *args], env=env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)

        passwords = {role: secrets.token_urlsafe(32) for role in ("supervisor", "rehearsal")}
        pwfile = Path(tmp, "bootstrap")
        pwfile.write_text(passwords["supervisor"]+"\n")
        pg("initdb", "-D", data, "-U", "supervisor", "-A", "scram-sha-256",
           "--pwfile", pwfile, "--no-locale", "-E", "UTF8")
        pwfile.unlink()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with (data / "postgresql.conf").open("a") as config:
            config.write(f"\nlisten_addresses='127.0.0.1'\nport={port}\nunix_socket_directories=''\n"
                         "password_encryption='scram-sha-256'\nmax_connections=16\n")

        def stop():
            if (data / "postmaster.pid").exists():
                pg("pg_ctl", "-D", data, "-m", "immediate", "-w", "stop")

        stack.callback(stop)
        pg("pg_ctl", "-D", data, "-l", Path(tmp, "postgres.log"), "-w", "start")

        def connect(role="rehearsal", label="application"):
            conn = psycopg.connect(host="127.0.0.1", port=port, password=passwords[role], sslmode="disable", user=role, dbname="postgres",
                                   application_name=label, connect_timeout=4, autocommit=True,
                                   options="-c statement_timeout=20000 -c idle_in_transaction_session_timeout=30000")
            stack.callback(conn.close)
            return conn

        admin = connect("supervisor", "observer")
        admin.execute(sql.SQL("CREATE ROLE rehearsal LOGIN PASSWORD {}").format(sql.Literal(passwords["rehearsal"])))
        admin.execute("CREATE SCHEMA AUTHORIZATION rehearsal")
        admin.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        yield connect, admin
