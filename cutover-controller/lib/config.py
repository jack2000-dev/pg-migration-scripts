from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


def _required(mapping: dict[str, Any], key: str, context: str) -> Any:
    value = mapping.get(key)
    if value is None or value == "":
        raise ConfigError(f"{context}.{key} is required")
    return value


def _string(mapping: dict[str, Any], key: str, context: str, *, required: bool = True) -> str | None:
    value = _required(mapping, key, context) if required else mapping.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        qualifier = f"{context}.{key}"
        raise ConfigError(f"{qualifier} must be a non-empty string without NUL bytes")
    return value

def _table(value: Any, context: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must contain schema and table mappings")
    schema = _required(value, "schema", context)
    table = _required(value, "table", context)
    if not isinstance(schema, str) or not isinstance(table, str):
        raise ConfigError(f"{context} schema and table must be strings")
    if "\x00" in schema or "\x00" in table:
        raise ConfigError(f"{context} contains a NUL byte")
    return schema, table


@dataclass(frozen=True)
class Endpoint:
    name: str
    host: str
    port: int
    user: str
    sslmode: str
    system_identifier: str
    password_env: str | None
    replication_user: str | None
    replication_password_env: str | None
    connect_timeout: int

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any]) -> "Endpoint":
        if not isinstance(raw, dict):
            raise ConfigError(f"{name} must be a mapping")
        if "password" in raw:
            raise ConfigError(f"{name}.password is forbidden; use .pgpass or password_env")
        try:
            port = int(raw.get("port", 5432))
            timeout = int(raw.get("connect_timeout", 10))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{name}.port and connect_timeout must be integers") from exc
        if not (1 <= port <= 65535) or timeout <= 0:
            raise ConfigError(f"invalid {name} port or connect_timeout")
        endpoint = cls(
            name=name,
            host=_string(raw, "host", name),
            port=port,
            user=_string(raw, "user", name),
            sslmode=_string(raw, "sslmode", name, required=False) or "require",
            system_identifier=str(_required(raw, "expected_system_identifier", name)),
            password_env=_string(raw, "password_env", name, required=False),
            replication_user=_string(raw, "replication_user", name, required=False),
            replication_password_env=_string(raw, "replication_password_env", name, required=False),
            connect_timeout=timeout,
        )
        return endpoint

    def controller_environment(self) -> dict[str, str]:
        env = {
            "PGHOST": self.host,
            "PGPORT": str(self.port),
            "PGUSER": self.user,
            "PGSSLMODE": self.sslmode,
            "PGCONNECT_TIMEOUT": str(self.connect_timeout),
            "PGAPPNAME": "cutover-controller",
        }
        if self.password_env:
            try:
                env["PGPASSWORD"] = os.environ[self.password_env]
            except KeyError as exc:
                raise ConfigError(f"environment variable {self.password_env} is not set") from exc
        return env


@dataclass(frozen=True)
class Database:
    name: str
    forward_publication: str
    forward_subscription: str
    reverse_publication: str
    reverse_subscription: str
    expected_tables: tuple[tuple[str, str], ...]
    expected_publish: frozenset[str]
    forward_slot: str | None
    reverse_slot: str
    test_table: tuple[str, str] | None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int) -> "Database":
        context = f"databases[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{context} must be a mapping")
        tables_raw = _required(raw, "expected_tables", context)
        if not isinstance(tables_raw, list) or not tables_raw:
            raise ConfigError(f"{context}.expected_tables must be a non-empty list")
        tables = tuple(_table(value, f"{context}.expected_tables[{i}]") for i, value in enumerate(tables_raw))
        if len(set(tables)) != len(tables):
            raise ConfigError(f"{context}.expected_tables contains duplicates")
        actions_raw = raw.get("expected_publish", ["insert", "update", "delete", "truncate"])
        if not isinstance(actions_raw, list):
            raise ConfigError(f"{context}.expected_publish must be a list")
        if not all(isinstance(value, str) for value in actions_raw):
            raise ConfigError(f"{context}.expected_publish entries must be strings")
        actions = frozenset(value.lower() for value in actions_raw)
        valid_actions = {"insert", "update", "delete", "truncate"}
        if not actions or not actions <= valid_actions:
            raise ConfigError(f"{context}.expected_publish must use {sorted(valid_actions)}")
        test = raw.get("test_table")
        reverse_subscription = _string(raw, "reverse_subscription", context)
        return cls(
            name=_string(raw, "name", context),
            forward_publication=_string(raw, "forward_publication", context),
            forward_subscription=_string(raw, "forward_subscription", context),
            reverse_publication=_string(raw, "reverse_publication", context),
            reverse_subscription=reverse_subscription,
            expected_tables=tables,
            expected_publish=actions,
            forward_slot=_string(raw, "forward_slot", context, required=False),
            reverse_slot=_string(raw, "reverse_slot", context, required=False) or reverse_subscription,
            test_table=_table(test, f"{context}.test_table") if test is not None else None,
        )


@dataclass(frozen=True)
class Config:
    path: Path
    source: Endpoint
    target: Endpoint
    databases: tuple[Database, ...]
    poll_interval: float
    lock_timeout_seconds: int
    healthy_lag_bytes: int
    digest: str

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        config_path = Path(path).resolve()
        try:
            raw_text = config_path.read_text(encoding="utf-8")
            raw = yaml.safe_load(raw_text)
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("configuration root must be a mapping")
        source = Endpoint.from_dict("source", raw.get("source"))
        target = Endpoint.from_dict("target", raw.get("target"))
        if source.system_identifier == target.system_identifier:
            raise ConfigError("source and target system identifiers must be different")
        databases_raw = raw.get("databases")
        if not isinstance(databases_raw, list) or not databases_raw:
            raise ConfigError("databases must be a non-empty list")
        databases = tuple(Database.from_dict(v, i) for i, v in enumerate(databases_raw))
        names = [db.name for db in databases]
        if len(set(names)) != len(names):
            raise ConfigError("database names must be unique")
        reverse_slots = [db.reverse_slot for db in databases]
        if any(not name for name in reverse_slots) or len(set(reverse_slots)) != len(reverse_slots):
            raise ConfigError("reverse slot names must be non-empty and cluster-wide unique")
        configured_forward_slots = [db.forward_slot for db in databases if db.forward_slot]
        if len(set(configured_forward_slots)) != len(configured_forward_slots):
            raise ConfigError("configured forward slot names must be cluster-wide unique")
        settings = raw.get("settings", {})
        if not isinstance(settings, dict):
            raise ConfigError("settings must be a mapping")
        try:
            poll_interval = float(settings.get("poll_interval_seconds", 5))
            lock_timeout = int(settings.get("lock_timeout_seconds", 5))
            healthy_lag = int(settings.get("healthy_lag_bytes", 0))
        except (TypeError, ValueError) as exc:
            raise ConfigError("settings values must be numeric") from exc
        if not math.isfinite(poll_interval) or poll_interval <= 0 or lock_timeout <= 0 or healthy_lag < 0:
            raise ConfigError("poll interval and lock timeout must be positive; healthy lag cannot be negative")
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return cls(
            path=config_path,
            source=source,
            target=target,
            databases=databases,
            poll_interval=poll_interval,
            lock_timeout_seconds=lock_timeout,
            healthy_lag_bytes=healthy_lag,
            digest=hashlib.sha256(canonical.encode()).hexdigest(),
        )

    def database(self, name: str) -> Database:
        for database in self.databases:
            if database.name == name:
                return database
        raise ConfigError(f"unknown configured database: {name}")
