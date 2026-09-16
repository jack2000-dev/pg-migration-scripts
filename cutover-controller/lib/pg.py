from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import Endpoint


class PostgreSQLError(RuntimeError):
    pass


def quote_ident(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("invalid PostgreSQL identifier")
    return '"' + value.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("invalid PostgreSQL literal")
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def qualified(schema: str, relation: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(relation)}"


def libpq_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Psql:
    def __init__(self, endpoint: Endpoint, sql_dir: Path, logger: Any, lock_timeout_seconds: int = 5):
        self.endpoint = endpoint
        self.sql_dir = sql_dir
        self.logger = logger
        self.lock_timeout_seconds = lock_timeout_seconds

    def run(self, database: str, sql: str, *, read_only: bool = True, contains_secret: bool = False) -> str:
        env = os.environ.copy()
        env.update(self.endpoint.controller_environment())
        command = [
            "psql", "-X", "--no-psqlrc", "--quiet", "--no-align", "--tuples-only",
            "--set", "ON_ERROR_STOP=1", "--dbname", database,
        ]
        prefix = (
            "SET standard_conforming_strings = on;\n"
            f"SET lock_timeout = {quote_literal(str(self.lock_timeout_seconds) + 's')};\n"
        )
        if read_only:
            prefix += "SET default_transaction_read_only = on;\n"
        self.logger.debug("psql side=%s database=%s mode=%s", self.endpoint.name, database, "read" if read_only else "write")
        try:
            completed = subprocess.run(
                command,
                input=prefix + sql,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        except OSError as exc:
            raise PostgreSQLError(f"cannot execute psql: {exc}") from exc
        if completed.returncode:
            stderr = completed.stderr.strip()
            if contains_secret:
                stderr = "psql rejected a secret-bearing command; inspect PostgreSQL server logs"
            raise PostgreSQLError(f"{self.endpoint.name}/{database}: {stderr or 'psql failed'}")
        return completed.stdout.strip()

    def json(self, database: str, sql: str, *, read_only: bool = True, contains_secret: bool = False) -> Any:
        output = self.run(database, sql, read_only=read_only, contains_secret=contains_secret)
        lines = [line for line in output.splitlines() if line.strip()]
        if len(lines) != 1:
            raise PostgreSQLError(f"{self.endpoint.name}/{database}: expected one JSON row, got {len(lines)}")
        try:
            return json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise PostgreSQLError(f"{self.endpoint.name}/{database}: invalid JSON result") from exc

    def file(self, database: str, filename: str, variables: dict[str, str] | None = None) -> Any:
        sql = (self.sql_dir / filename).read_text(encoding="utf-8")
        for key, value in (variables or {}).items():
            sql = sql.replace(f"{{{{{key}}}}}", quote_literal(value))
        if "{{" in sql:
            raise PostgreSQLError(f"unresolved SQL template variable in {filename}")
        return self.json(database, sql)

    def identity(self, database: str) -> dict[str, Any]:
        return self.file(database, "identity.sql")

    def validate_identity(self, database: str) -> dict[str, Any]:
        identity = self.identity(database)
        if identity.get("database") != database:
            raise PostgreSQLError(
                f"{self.endpoint.name}/{database}: connected to unexpected database {identity.get('database')!r}"
            )
        actual = str(identity.get("system_identifier"))
        if actual != self.endpoint.system_identifier:
            raise PostgreSQLError(
                f"{self.endpoint.name}/{database}: system identifier {actual} does not match configured {self.endpoint.system_identifier}"
            )
        version = int(identity.get("server_version_num", 0))
        if not 170000 <= version < 190000:
            raise PostgreSQLError(
                f"{self.endpoint.name}/{database}: PostgreSQL server version {identity.get('server_version')} is unsupported"
            )
        if identity.get("in_recovery"):
            raise PostgreSQLError(f"{self.endpoint.name}/{database}: server is in recovery and cannot be mutated")
        return identity

    def reverse_conninfo(self, database: str) -> str:
        user = self.endpoint.replication_user or self.endpoint.user
        values = {
            "host": self.endpoint.host,
            "port": str(self.endpoint.port),
            "dbname": database,
            "user": user,
            "sslmode": self.endpoint.sslmode,
            "connect_timeout": str(self.endpoint.connect_timeout),
            "application_name": "cutover-controller-reverse",
        }
        if self.endpoint.replication_password_env:
            name = self.endpoint.replication_password_env
            try:
                values["password"] = os.environ[name]
            except KeyError as exc:
                raise PostgreSQLError(f"environment variable {name} is not set") from exc
        return " ".join(f"{key}={libpq_value(value)}" for key, value in values.items())
