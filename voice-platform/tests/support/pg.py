"""Throwaway PostgreSQL cluster for tests and local development.

Uses the server binaries installed on the machine (``initdb``/``pg_ctl``). In CI a service
container is used instead by setting ``VP_TEST_PG_ADMIN_URL``.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

OWNER_ROLE = "vp_owner"
APP_ROLE = "vp_app"
PASSWORD = "vp_test_password"


def _find_bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for base in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        candidate = base / name
        if candidate.exists():
            return str(candidate)
    raise RuntimeError(f"{name} not found; install PostgreSQL server binaries")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class LocalPostgres:
    datadir: Path
    port: int
    dbname: str = "voice_platform"

    @classmethod
    def start(cls) -> LocalPostgres:
        datadir = Path(tempfile.mkdtemp(prefix="vp-pg-"))
        port = _free_port()
        user = "postgres"
        env = {**os.environ, "LC_ALL": "C.UTF-8"}
        run_as: list[str] = []
        if os.geteuid() == 0:
            # postgres refuses to run as root; run through the 'postgres' system user if present
            shutil.chown(datadir, user="postgres")
            run_as = ["runuser", "-u", "postgres", "--"]
        subprocess.run(
            [*run_as, _find_bin("initdb"), "-D", str(datadir), "-U", user, "-A", "trust",
             "--no-sync", "-E", "UTF8"],
            check=True, capture_output=True, env=env,
        )
        sockdir = datadir / "sock"
        sockdir.mkdir()
        if run_as:
            shutil.chown(sockdir, user="postgres")
        subprocess.run(
            [*run_as, _find_bin("pg_ctl"), "-D", str(datadir), "-w", "-l", str(datadir / "log"),
             "-o", f"-p {port} -k {sockdir} -c fsync=off -c synchronous_commit=off "
                   f"-c full_page_writes=off -c listen_addresses=127.0.0.1", "start"],
            check=True, capture_output=True, env=env,
        )
        pg = cls(datadir=datadir, port=port)
        pg._bootstrap()
        return pg

    def psql(self, sql: str, db: str = "postgres") -> None:
        subprocess.run(
            [_find_bin("psql"), "-h", "127.0.0.1", "-p", str(self.port), "-U", "postgres",
             "-d", db, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql],
            check=True, capture_output=True,
        )

    def _bootstrap(self) -> None:
        for _ in range(50):
            try:
                self.psql("select 1")
                break
            except subprocess.CalledProcessError:
                time.sleep(0.1)
        self.psql(f"create role {OWNER_ROLE} login password '{PASSWORD}'")
        self.psql(f"create role {APP_ROLE} login password '{PASSWORD}' nosuperuser nobypassrls")
        self.psql(f"create database {self.dbname} owner {OWNER_ROLE}")
        self.psql("create extension if not exists vector", db=self.dbname)
        self.psql("create extension if not exists pgcrypto", db=self.dbname)

    def url(self, role: str = APP_ROLE, driver: str = "asyncpg") -> str:
        return f"postgresql+{driver}://{role}:{PASSWORD}@127.0.0.1:{self.port}/{self.dbname}"

    def stop(self) -> None:
        run_as = ["runuser", "-u", "postgres", "--"] if os.geteuid() == 0 else []
        subprocess.run(
            [*run_as, _find_bin("pg_ctl"), "-D", str(self.datadir), "-m", "immediate", "stop"],
            capture_output=True,
        )
        shutil.rmtree(self.datadir, ignore_errors=True)
