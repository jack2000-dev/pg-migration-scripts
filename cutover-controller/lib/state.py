from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from .config import Config


class StateError(RuntimeError):
    pass


FORWARD_PHASES = [
    "FORWARD_REPLICATING",
    "WRITES_FROZEN",
    "FINAL_LSN_CAPTURED",
    "FORWARD_CAUGHT_UP",
    "FORWARD_DISABLED",
    "SEQUENCES_SYNCED",
    "REVERSE_PREPARED",
    "REVERSE_ACTIVE",
    "CUTOVER_COMPLETE",
]

ROLLBACK_PHASES = [
    "CUTOVER_COMPLETE",
    "ROLLBACK_WRITES_FROZEN",
    "ROLLBACK_LSN_CAPTURED",
    "ROLLBACK_CAUGHT_UP",
    "ROLLBACK_SEQUENCES_SYNCED",
    "ROLLED_BACK",
]

VALID_PHASES = set(FORWARD_PHASES) | set(ROLLBACK_PHASES) | {"FINALIZED"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: str | Path, config: Config):
        self.path = Path(path).resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.config = config
        self.data: dict[str, Any] = {}

    @contextmanager
    def locked(self) -> Iterator["StateStore"]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.lock_path, 0o600)
        with self.lock_path.open("r+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.load()
            try:
                yield self
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def load(self) -> None:
        if not self.path.exists():
            self.data = {
                "schema_version": 1,
                "created_at": now(),
                "updated_at": now(),
                "config_digest": self.config.digest,
                "writer": "source",
                "instances": {},
                "databases": {
                    db.name: {
                        "phase": "FORWARD_REPLICATING",
                        "checkpoints": {},
                        "operations": {},
                    }
                    for db in self.config.databases
                },
            }
            return
        if self.path.stat().st_mode & 0o077:
            raise StateError(f"state file {self.path} must not be accessible by group or other users")
        try:
            loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise StateError(f"cannot read state {self.path}: {exc}") from exc
        if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
            raise StateError("unsupported or invalid state file")
        if loaded.get("config_digest") != self.config.digest:
            raise StateError("configuration changed since state was created; restore it or start a separately reviewed run")
        configured = {db.name for db in self.config.databases}
        databases = loaded.get("databases")
        if not isinstance(databases, dict) or set(databases) != configured:
            raise StateError("state database set does not match configuration")
        if loaded.get("writer") not in {"source", "target", "none"}:
            raise StateError("state writer is invalid")
        for name, entry in databases.items():
            if not isinstance(entry, dict) or entry.get("phase") not in VALID_PHASES:
                raise StateError(f"{name}: state phase is invalid")
            if not isinstance(entry.get("checkpoints"), dict) or not isinstance(entry.get("operations"), dict):
                raise StateError(f"{name}: state checkpoints or operations are invalid")
        self.data = loaded

    def save(self) -> None:
        self.data["updated_at"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                yaml.safe_dump(self.data, stream, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def database(self, name: str) -> dict[str, Any]:
        return self.data["databases"][name]

    def phase_at_least(self, name: str, required: str) -> bool:
        phase = self.database(name)["phase"]
        if phase in FORWARD_PHASES and required in FORWARD_PHASES:
            return FORWARD_PHASES.index(phase) >= FORWARD_PHASES.index(required)
        if phase in ROLLBACK_PHASES and required in ROLLBACK_PHASES:
            return ROLLBACK_PHASES.index(phase) >= ROLLBACK_PHASES.index(required)
        return phase == required

    def require_all(self, phase: str) -> None:
        missing = [db.name for db in self.config.databases if not self.phase_at_least(db.name, phase)]
        if missing:
            raise StateError(f"requires {phase} for every database; blocked: {', '.join(missing)}")

    def set_phase(self, name: str, phase: str, **checkpoint: Any) -> None:
        if phase not in VALID_PHASES:
            raise StateError(f"invalid phase: {phase}")
        entry = self.database(name)
        entry["phase"] = phase
        entry["checkpoints"].setdefault(phase, {}).update({"completed_at": now(), **checkpoint})
        self.save()

    def operation(self, name: str, command: str, status: str, error: str | None = None) -> None:
        value = {"status": status, "updated_at": now()}
        if error:
            value["error"] = error
        self.database(name)["operations"][command] = value
        self.save()

    def prepare_batch(self, command: str, resume: bool) -> None:
        statuses = [self.database(db.name)["operations"].get(command, {}).get("status") for db in self.config.databases]
        if "FAILED" in statuses and not resume:
            raise StateError(f"{command} previously failed; rerun with --resume")
        for db, status in zip(self.config.databases, statuses):
            if status != "COMPLETE":
                self.database(db.name)["operations"][command] = {"status": "NOT_STARTED", "updated_at": now()}
        self.save()

    def operation_rows(self, command: str) -> list[tuple[str, str]]:
        return [
            (db.name, self.database(db.name)["operations"].get(command, {}).get("status", "NOT_STARTED"))
            for db in self.config.databases
        ]
