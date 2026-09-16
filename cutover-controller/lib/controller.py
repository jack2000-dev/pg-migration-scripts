from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import Config, Database
from .output import bytes_text, emit, table
from .pg import Psql, qualified, quote_ident, quote_literal
from .state import StateStore, now


class ControllerError(RuntimeError):
    exit_code = 2


class PartialFailure(ControllerError):
    exit_code = 5


def lsn_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        high, low = value.split("/", 1)
        return (int(high, 16) << 32) + int(low, 16)
    except (ValueError, AttributeError) as exc:
        raise ControllerError(f"invalid PostgreSQL LSN: {value!r}") from exc


def schema_map(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(row["schema"], row["table"]): row for row in rows}


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def publication_actions(publication: dict[str, Any] | None) -> set[str]:
    if not publication:
        return set()
    return {
        action
        for action, column in (("insert", "pubinsert"), ("update", "pubupdate"),
                               ("delete", "pubdelete"), ("truncate", "pubtruncate"))
        if publication.get(column)
    }


def publication_rules(rows: list[dict[str, Any]]) -> list[tuple[str, str, tuple[str, ...], str | None]]:
    return sorted(
        (row["schemaname"], row["tablename"], tuple(row.get("attnames") or ()), row.get("rowfilter"))
        for row in rows
    )


def _type_lossless(publisher: dict[str, Any], subscriber: dict[str, Any]) -> bool:
    if publisher["type"] == subscriber["type"]:
        return True
    integer_rank = {"smallint": 1, "integer": 2, "bigint": 3}
    left = integer_rank.get(publisher["type"])
    right = integer_rank.get(subscriber["type"])
    return left is not None and right is not None and right >= left


class Controller:
    """State-machine orchestration.

    PostgreSQL cannot make changes atomically across databases. Every public
    command therefore validates all global gates first, changes one database at
    a time, and records the verified result immediately.
    """
    def __init__(self, config: Config, state: StateStore, sql_dir: Path, logger: logging.Logger):
        self.config = config
        self.state = state
        self.logger = logger
        self.source = Psql(config.source, sql_dir, logger, config.lock_timeout_seconds)
        self.target = Psql(config.target, sql_dir, logger, config.lock_timeout_seconds)

    def _require_phases(self, command: str, allowed: set[str]) -> None:
        invalid = [
            f"{db.name}={self.state.database(db.name)['phase']}"
            for db in self.config.databases
            if self.state.database(db.name)["phase"] not in allowed
        ]
        if invalid:
            raise ControllerError(f"{command} is invalid for phase: {', '.join(invalid)}")

    def _validate_batch_identities(self, *, source: bool = True, target: bool = True) -> None:
        """Pin all endpoints before the first database in a batch is changed."""
        for db in self.config.databases:
            if source:
                self.source.validate_identity(db.name)
            if target:
                self.target.validate_identity(db.name)

    def _identity_line(self, side: str, database: str, psql: Psql) -> dict[str, Any]:
        identity = psql.validate_identity(database)
        print(
            f"{side.upper()} host={psql.endpoint.host}:{psql.endpoint.port} "
            f"server={identity['server_addr']} system_id={identity['system_identifier']} "
            f"version={identity['server_version']} db={identity['database']} user={identity['user']}"
        )
        if identity.get("in_recovery"):
            raise ControllerError(f"{side}/{database} is in recovery and cannot be mutated")
        return identity

    def _source_info(self, db: Database, slot: str) -> dict[str, Any]:
        return self.source.file(db.name, "precheck_source.sql", {
            "publication": db.forward_publication, "slot": slot,
        })

    def _target_info(self, db: Database, reverse: bool = False) -> dict[str, Any]:
        subscription = db.reverse_subscription if reverse else db.forward_subscription
        runner = self.source if reverse else self.target
        return runner.file(db.name, "precheck_target.sql", {"subscription": subscription})

    def _forward_slot(self, db: Database, target_info: dict[str, Any] | None = None) -> str:
        if db.forward_slot:
            return db.forward_slot
        checkpoint = self.state.database(db.name)["checkpoints"].get("forward_slot")
        if checkpoint:
            return checkpoint["name"]
        info = target_info or self._target_info(db)
        subscription = info.get("subscription")
        if not subscription or not subscription.get("subslotname"):
            raise ControllerError(f"{db.name}: cannot discover forward slot from subscription")
        slot = subscription["subslotname"]
        self.state.database(db.name)["checkpoints"]["forward_slot"] = {"name": slot, "captured_at": now()}
        self.state.save()
        return slot

    def _manifest(self, db: Database) -> set[tuple[str, str]]:
        return set(db.expected_tables)

    def _table_set(self, rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
        return {(row["schemaname"], row["tablename"]) for row in rows}

    def _main_worker(self, info: dict[str, Any]) -> dict[str, Any] | None:
        for worker in info.get("workers", []):
            if worker.get("worker_type") == "apply" and worker.get("relid") is None:
                return worker
        return None

    def inventory(self, as_json: bool) -> None:
        result: dict[str, Any] = {}
        for db in self.config.databases:
            self.source.validate_identity(db.name)
            info = self.source.file(db.name, "precheck_source.sql", {
                "publication": db.forward_publication,
                "slot": db.forward_slot or db.forward_subscription,
            })
            rows = [{"schema": row["schemaname"], "table": row["tablename"]} for row in info["tables"]]
            result[db.name] = rows
        if as_json:
            emit(result, True)
            return
        print("# Copy each list into the matching database.expected_tables entry")
        for name, rows in result.items():
            print(f"{name}:")
            for row in rows:
                print(f"  - schema: {json.dumps(row['schema'])}")
                print(f"    table: {json.dumps(row['table'])}")

    def _inspect_forward(self, db: Database) -> dict[str, Any]:
        target = self._target_info(db)
        slot_name = self._forward_slot(db, target)
        source = self._source_info(db, slot_name)
        subscription = target.get("subscription")
        publication = source.get("publication")
        slot = source.get("slot")
        stats = target.get("stats") or {"apply_error_count": 0, "sync_error_count": 0}
        worker = self._main_worker(target)
        problems: list[str] = []
        if not publication:
            problems.append("publication missing")
        elif publication_actions(publication) != set(db.expected_publish):
            problems.append("publication actions differ")
        pub_tables = self._table_set(source.get("tables", []))
        sub_tables = self._table_set(target.get("relations", []))
        if pub_tables != self._manifest(db):
            problems.append("publication table manifest differs")
        if sub_tables != self._manifest(db):
            problems.append("subscription table manifest differs")
        if not subscription:
            problems.append("subscription missing")
        else:
            if not subscription.get("subenabled"):
                problems.append("subscription disabled")
            if set(subscription.get("subpublications", [])) != {db.forward_publication}:
                problems.append("subscription publication differs")
            if subscription.get("subslotname") != slot_name:
                problems.append("subscription slot differs")
            if subscription.get("subbinary"):
                problems.append("subscription binary mode is enabled")
        if not slot:
            problems.append("slot missing")
        else:
            if slot.get("slot_type") != "logical" or slot.get("plugin") != "pgoutput":
                problems.append("slot is not logical pgoutput")
            if slot.get("database") != db.name:
                problems.append("slot database differs")
            if not slot.get("active"):
                problems.append("slot inactive")
            if not slot.get("confirmed_flush_lsn"):
                problems.append("slot has no confirmed flush LSN")
            if slot.get("wal_status") not in {"reserved", "extended"}:
                problems.append(f"slot WAL status {slot.get('wal_status')}")
        if not worker:
            problems.append("apply worker missing")
        if int(target.get("not_ready_count", 0)):
            problems.append("tables not ready")
        if not target.get("origin"):
            problems.append("replication origin missing")
        if slot and slot.get("lag_bytes") is not None and slot["lag_bytes"] > self.config.healthy_lag_bytes:
            problems.append(f"lag exceeds {self.config.healthy_lag_bytes} bytes")
        return {
            "database": db.name,
            "slot_name": slot_name,
            "slot": slot,
            "worker": worker,
            "stats": stats,
            "target": target,
            "source": source,
            "problems": problems,
        }

    def _reverse_status(
        self, db: Database, *, require_active: bool
    ) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
        forward = self._source_info(db, self._forward_slot(db))
        publisher = self.target.file(db.name, "precheck_source.sql", {
            "publication": db.reverse_publication, "slot": db.reverse_slot,
        })
        subscriber = self._target_info(db, reverse=True)
        publication = publisher.get("publication")
        subscription = subscriber.get("subscription")
        slot = publisher.get("slot")
        problems: list[str] = []
        if not publication:
            problems.append("reverse publication missing")
        else:
            if publication_actions(publication) != set(db.expected_publish):
                problems.append("reverse publication actions differ")
            if self._table_set(publisher.get("tables", [])) != self._manifest(db):
                problems.append("reverse publication table manifest differs")
            if publication_rules(publisher.get("tables", [])) != publication_rules(forward.get("tables", [])):
                problems.append("reverse publication column or row-filter rules differ")
            if bool(publication.get("pubviaroot")) != bool((forward.get("publication") or {}).get("pubviaroot")):
                problems.append("reverse publication partition-root rule differs")
        if not subscription:
            problems.append("reverse subscription missing")
        else:
            if set(subscription.get("subpublications", [])) != {db.reverse_publication}:
                problems.append("reverse subscription publication differs")
            if subscription.get("subslotname") != db.reverse_slot:
                problems.append("reverse subscription slot differs")
            if subscription.get("suborigin") != "none":
                problems.append("reverse subscription origin differs")
            if subscription.get("subbinary"):
                problems.append("reverse subscription binary mode is enabled")
            if subscription.get("substream") != "t":
                problems.append("reverse subscription streaming mode differs")
            if subscription.get("subtwophasestate") != "d":
                problems.append("reverse subscription two-phase mode differs")
            if not subscription.get("subdisableonerr"):
                problems.append("reverse subscription disable_on_error is off")
            if subscription.get("subrunasowner"):
                problems.append("reverse subscription run_as_owner is enabled")
            if subscription.get("subfailover"):
                problems.append("reverse subscription failover mode is enabled")
            if bool(subscription.get("subenabled")) != require_active:
                problems.append("reverse subscription enabled state differs")
        if not subscriber.get("origin"):
            problems.append("reverse replication origin missing")
        if self._table_set(subscriber.get("relations", [])) != self._manifest(db):
            problems.append("reverse subscription table manifest differs")
        if int(subscriber.get("not_ready_count", 0)):
            problems.append("reverse subscription tables not ready")
        if not slot:
            problems.append("reverse slot missing")
        else:
            if slot.get("slot_type") != "logical" or slot.get("plugin") != "pgoutput":
                problems.append("reverse slot is not logical pgoutput")
            if slot.get("database") != db.name:
                problems.append("reverse slot database differs")
            if slot.get("wal_status") not in {"reserved", "extended"}:
                problems.append(f"reverse slot WAL status {slot.get('wal_status')}")
            if bool(slot.get("active")) != require_active:
                problems.append("reverse slot active state differs")
        if bool(self._main_worker(subscriber)) != require_active:
            problems.append("reverse apply worker state differs")
        return problems, publisher, subscriber

    def _capacity(self) -> dict[str, Any]:
        db = self.config.databases[0].name
        source = self.source.file(db, "capacity.sql")
        target = self.target.file(db, "capacity.sql")
        issues: list[str] = []
        count = len(self.config.databases)
        missing_slots = 0
        missing_origins = 0
        for configured in self.config.databases:
            reverse_publisher = self.target.file(configured.name, "precheck_source.sql", {
                "publication": configured.reverse_publication,
                "slot": configured.reverse_slot,
            })
            if not reverse_publisher.get("slot"):
                missing_slots += 1
            if not self._target_info(configured, reverse=True).get("subscription"):
                missing_origins += 1
        target_settings = target["settings"]
        source_settings = source["settings"]
        if target["replication_slots_used"] + missing_slots > target_settings.get("max_replication_slots", 0):
            issues.append("TARGET lacks replication slots for reverse protection")
        if source["active_origins"] + missing_origins > source_settings.get("max_active_replication_origins", 2**63 - 1):
            issues.append("SOURCE lacks active replication origins for reverse subscriptions")
        if source["logical_workers_visible"] + count > source_settings.get("max_logical_replication_workers", 0):
            issues.append("SOURCE lacks logical replication workers for reverse subscriptions")
        if source["logical_workers_visible"] + count + 1 > source_settings.get("max_worker_processes", 0):
            issues.append("SOURCE lacks worker-process capacity for reverse subscriptions")
        if target["wal_senders_used"] + count > target_settings.get("max_wal_senders", 0):
            issues.append("TARGET lacks WAL sender capacity for reverse subscriptions")
        return {
            "source": source,
            "target": target,
            "projected_reverse_subscriptions": count,
            "additional_slots_needed": missing_slots,
            "additional_origins_needed": missing_origins,
            "issues": issues,
        }

    def precheck(self, observe_seconds: float, as_json: bool) -> None:
        if observe_seconds < 0:
            raise ControllerError("--observe-seconds cannot be negative")
        first: dict[str, dict[str, Any]] = {}
        identities: dict[str, Any] = {"source": {}, "target": {}}
        for db in self.config.databases:
            identities["source"][db.name] = self.source.validate_identity(db.name)
            identities["target"][db.name] = self.target.validate_identity(db.name)
            first[db.name] = self._inspect_forward(db)

        if observe_seconds:
            time.sleep(observe_seconds)
        rows = []
        details = []
        healthy = True
        for db in self.config.databases:
            current = self._inspect_forward(db)
            before = first[db.name]["stats"]
            after = current["stats"]
            apply_delta = int(after.get("apply_error_count", 0)) - int(before.get("apply_error_count", 0))
            sync_delta = int(after.get("sync_error_count", 0)) - int(before.get("sync_error_count", 0))
            problems = list(current["problems"])
            try:
                self._check_direction(db, current["source"]["tables"], self.source, self.target)
            except ControllerError as exc:
                problems.append(str(exc))
            if apply_delta > 0:
                problems.append(f"apply errors increased by {apply_delta}")
            if sync_delta > 0:
                problems.append(f"sync errors increased by {sync_delta}")
            counters_reset = before.get("stats_reset") != after.get("stats_reset")
            if counters_reset:
                problems.append("subscription error statistics reset during observation")
            current["problems"] = problems
            ready = not problems
            healthy &= ready
            slot = current["slot"] or {}
            rows.append([
                db.name, "active" if slot.get("active") else "inactive",
                bytes_text(slot.get("lag_bytes")),
                "yes" if current["target"].get("not_ready_count", 0) == 0 else "no",
                f"{after.get('apply_error_count', 0)} ({'reset' if counters_reset else '+' + str(max(apply_delta, 0))})",
                f"{after.get('sync_error_count', 0)} ({'reset' if counters_reset else '+' + str(max(sync_delta, 0))})",
                "YES" if ready else "NO",
            ])
            details.append({
                **current,
                "apply_error_delta": None if counters_reset else apply_delta,
                "sync_error_delta": None if counters_reset else sync_delta,
                "counters_reset_during_observation": counters_reset,
                "ready": ready,
            })
        capacity = self._capacity()
        if capacity["issues"]:
            healthy = False
        self.state.data["instances"] = {
            "source": {"system_identifier": self.config.source.system_identifier},
            "target": {"system_identifier": self.config.target.system_identifier},
        }
        self.state.data["last_precheck"] = {"at": now(), "healthy": healthy}
        self.state.save()
        result = {"healthy": healthy, "databases": details, "capacity": capacity, "identities": identities}
        if as_json:
            emit(result, True)
        else:
            table(["DATABASE", "SLOT", "LAG", "TABLES_READY", "APPLY_ERR", "SYNC_ERR", "READY"], rows)
            for item in details:
                if item["problems"]:
                    print(f"{item['database']}: " + "; ".join(item["problems"]))
            for issue in capacity["issues"]:
                print(f"CAPACITY: {issue}")
            capacity_rows = []
            usage_columns = {
                "max_replication_slots": "replication_slots_used",
                "max_wal_senders": "wal_senders_used",
                "max_logical_replication_workers": "logical_workers_visible",
                "max_worker_processes": "worker_processes_visible",
                "max_active_replication_origins": "active_origins",
            }
            for side in ("source", "target"):
                values = capacity[side]
                for setting, limit in values["settings"].items():
                    used = values.get(usage_columns.get(setting), "n/a")
                    capacity_rows.append([side.upper(), setting, used, limit])
            print()
            table(["SIDE", "RESOURCE", "USED/VISIBLE", "LIMIT"], capacity_rows)
        if not healthy:
            raise ControllerError("precheck failed")

    def status(self, as_json: bool) -> None:
        rows = []
        details = []
        healthy = True
        for db in self.config.databases:
            self.source.validate_identity(db.name)
            self.target.validate_identity(db.name)
            phase = self.state.database(db.name)["phase"]
            if phase == "FINALIZED":
                rows.append([db.name, phase, "-", "-", "-", "-", "FINALIZED"])
                details.append({"database": db.name, "phase": phase, "healthy": True, "problems": []})
                continue
            use_reverse = phase in {
                "REVERSE_PREPARED", "REVERSE_ACTIVE", "CUTOVER_COMPLETE",
                "ROLLBACK_WRITES_FROZEN", "ROLLBACK_LSN_CAPTURED",
                "ROLLBACK_CAUGHT_UP", "ROLLBACK_SEQUENCES_SYNCED", "ROLLED_BACK",
            }
            reverse_active = phase in {
                "REVERSE_ACTIVE", "CUTOVER_COMPLETE", "ROLLBACK_WRITES_FROZEN",
                "ROLLBACK_LSN_CAPTURED", "ROLLBACK_CAUGHT_UP", "ROLLBACK_SEQUENCES_SYNCED",
            }
            if use_reverse:
                problems, publisher, subscriber = self._reverse_status(
                    db, require_active=reverse_active
                )
                slot = publisher.get("slot") or {}
            elif phase in {"FORWARD_DISABLED", "SEQUENCES_SYNCED"}:
                problems = self._forward_disabled_problems(db)
                subscriber = self._target_info(db)
                publisher = self._source_info(db, self._forward_slot(db, subscriber))
                slot = publisher.get("slot") or {}
            else:
                inspected = self._inspect_forward(db)
                problems = list(inspected["problems"])
                subscriber = inspected["target"]
                publisher = inspected["source"]
                slot = inspected["slot"] or {}
            if use_reverse and self._schema_drift(db):
                problems.append("schema drift detected")
            lag = slot.get("lag_bytes")
            if reverse_active and lag is not None and lag > self.config.healthy_lag_bytes:
                problems.append(f"lag exceeds {self.config.healthy_lag_bytes} bytes")
            stats = subscriber.get("stats") or {}
            ok = not problems
            healthy &= ok
            rows.append([
                db.name, phase, str(bool(slot.get("active"))).lower(), bytes_text(lag),
                stats.get("apply_error_count", 0), stats.get("sync_error_count", 0),
                "HEALTHY" if ok else "UNHEALTHY",
            ])
            details.append({
                "database": db.name,
                "phase": phase,
                "slot": slot,
                "subscription": subscriber,
                "problems": problems,
                "healthy": ok,
            })
        if as_json:
            emit({"healthy": healthy, "writer": self.state.data["writer"], "databases": details}, True)
        else:
            table(
                ["DATABASE", "PHASE", "SLOT_ACTIVE", "LAG", "APPLY_ERRORS", "SYNC_ERRORS", "STATUS"],
                rows,
            )
            for item in details:
                if item["problems"]:
                    print(f"{item['database']}: " + "; ".join(item["problems"]))
        if not healthy:
            raise ControllerError("one or more databases are unhealthy")

    def _assert_quiescent(self, runner: Psql, side: str) -> None:
        blockers = []
        for db in self.config.databases:
            data = runner.file(db.name, "active_transactions.sql")
            if data["active_transactions"] or data["prepared_transactions"]:
                blockers.append(db.name)
        if blockers:
            raise ControllerError(f"{side} has open or prepared transactions in: {', '.join(blockers)}")

    def _batch(self, command: str, resume: bool, dry_run: bool, function: Callable[[Database], None]) -> None:
        if dry_run:
            self.logger.info("command=%s dry_run=true", command)
            table(["DATABASE", command], [[db.name, "WOULD RUN"] for db in self.config.databases])
            return
        self.state.prepare_batch(command, resume)
        for db in self.config.databases:
            current = self.state.database(db.name)["operations"].get(command, {}).get("status")
            if current == "COMPLETE":
                self.logger.info("command=%s database=%s status=revalidating", command, db.name)
            else:
                self.logger.info("command=%s database=%s status=starting", command, db.name)
            self.state.operation(db.name, command, "RUNNING")
            try:
                function(db)
                self.state.operation(db.name, command, "COMPLETE")
                self.logger.info("command=%s database=%s status=complete", command, db.name)
            except Exception as exc:
                self.logger.exception("%s failed for %s", command, db.name)
                self.state.operation(db.name, command, "FAILED", str(exc))
                table(["DATABASE", command], self.state.operation_rows(command))
                hints = {
                    "capture-lsn": "./cutover capture-lsn --confirm-source-writes-frozen --resume",
                    "rollback-precheck": "./cutover rollback-precheck --confirm-target-writes-frozen --resume",
                    "finalize-disable": "./cutover finalize --execute --confirm-no-rollback --resume",
                    "finalize": "./cutover finalize --execute --confirm-no-rollback --resume",
                }
                print(f"Resume with: {hints.get(command, f'./cutover {command} --resume')}")
                raise PartialFailure(f"{command} failed for {db.name}: {exc}") from exc
        table(["DATABASE", command], self.state.operation_rows(command))

    def capture_lsn(self, confirmed: bool, force: bool, resume: bool) -> None:
        if not confirmed:
            raise ControllerError("capture-lsn requires --confirm-source-writes-frozen")
        self._require_phases(
            "capture-lsn", {"FORWARD_REPLICATING", "WRITES_FROZEN", "FINAL_LSN_CAPTURED"}
        )
        if not self.state.data.get("last_precheck", {}).get("healthy"):
            raise ControllerError("a successful precheck is required before final LSN capture")
        if self.state.data["writer"] not in {"source", "none"}:
            raise ControllerError("state does not designate SOURCE as the writer before this freeze")
        self._validate_batch_identities(target=False)
        for db in self.config.databases:
            entry = self.state.database(db.name)
            if force and entry["phase"] not in {"FORWARD_REPLICATING", "WRITES_FROZEN", "FINAL_LSN_CAPTURED"}:
                raise ControllerError(f"{db.name}: captured LSN cannot be replaced after catchup")
        self._assert_quiescent(self.source, "SOURCE")
        self.state.data["writer"] = "none"
        self.state.save()

        def capture(db: Database) -> None:
            self._identity_line("source", db.name, self.source)
            checkpoint = self.state.database(db.name)["checkpoints"].get("FINAL_LSN_CAPTURED")
            if checkpoint and not force:
                return
            self.state.set_phase(db.name, "WRITES_FROZEN", confirmed_by=os.environ.get("USER", "unknown"))
            lsn = self.source.run(db.name, "SELECT pg_current_wal_lsn()::text;")
            if not lsn_int(lsn):
                raise ControllerError(f"{db.name}: failed to capture a valid LSN")
            self.state.set_phase(db.name, "FINAL_LSN_CAPTURED", final_source_lsn=lsn, captured_at=now())
            print(f"{db.name} final_source_lsn={lsn}")

        self._batch("capture-lsn", resume, False, capture)

    def _progress(self, db: Database, reverse: bool, final_lsn: str) -> tuple[bool, int | None, str | None]:
        final_value = lsn_int(final_lsn)
        if final_value is None:
            raise ControllerError(f"{db.name}: final LSN is missing")
        if reverse:
            problems, publisher, subscriber = self._reverse_status(db, require_active=True)
        else:
            subscriber = self._target_info(db)
            slot_name = self._forward_slot(db, subscriber)
            publisher = self._source_info(db, slot_name)
            subscription = subscriber.get("subscription")
            publication = publisher.get("publication")
            slot = publisher.get("slot")
            worker = self._main_worker(subscriber)
            problems = []
            if not subscription:
                problems.append("forward subscription missing")
            else:
                if not subscription.get("subenabled"):
                    problems.append("forward subscription disabled")
                if set(subscription.get("subpublications", [])) != {db.forward_publication}:
                    problems.append("forward subscription publication differs")
                if subscription.get("subslotname") != slot_name:
                    problems.append("forward subscription slot differs")
                if subscription.get("subbinary"):
                    problems.append("forward subscription binary mode is enabled")
            if not publication:
                problems.append("forward publication missing")
            else:
                if publication_actions(publication) != set(db.expected_publish):
                    problems.append("forward publication actions differ")
                if self._table_set(publisher.get("tables", [])) != self._manifest(db):
                    problems.append("forward publication table manifest differs")
            if self._table_set(subscriber.get("relations", [])) != self._manifest(db):
                problems.append("forward subscription table manifest differs")
            if int(subscriber.get("not_ready_count", 0)):
                problems.append("forward subscription tables not ready")
            if not subscriber.get("origin"):
                problems.append("forward replication origin missing")
            if not slot:
                problems.append("forward slot missing")
            else:
                if slot.get("slot_type") != "logical" or slot.get("plugin") != "pgoutput":
                    problems.append("forward slot is not logical pgoutput")
                if slot.get("database") != db.name:
                    problems.append("forward slot database differs")
                if not slot.get("active"):
                    problems.append("forward slot inactive")
                if slot.get("wal_status") not in {"reserved", "extended"}:
                    problems.append(f"forward slot WAL status {slot.get('wal_status')}")
            if not worker:
                problems.append("forward apply worker missing")
        if problems:
            raise ControllerError(f"{db.name}: replication topology is unsafe: " + "; ".join(problems))
        worker = self._main_worker(subscriber)
        latest = worker.get("latest_end_lsn") if worker else None
        confirmed = (publisher.get("slot") or {}).get("confirmed_flush_lsn")
        latest_value = lsn_int(latest)
        confirmed_value = lsn_int(confirmed)
        observed = min(v for v in (latest_value, confirmed_value) if v is not None) if any(v is not None for v in (latest_value, confirmed_value)) else None
        remaining = None if observed is None else max(final_value - observed, 0)
        ready = (
            latest_value is not None and confirmed_value is not None
            and latest_value >= final_value and confirmed_value >= final_value
        )
        return ready, remaining, latest

    def wait_catchup(self, interval: float, timeout: float | None) -> None:
        if interval <= 0 or (timeout is not None and timeout < 0):
            raise ControllerError("poll interval must be positive and timeout cannot be negative")
        self._require_phases("wait-catchup", {"FINAL_LSN_CAPTURED", "FORWARD_CAUGHT_UP"})
        self._validate_batch_identities()
        started = time.monotonic()
        while True:
            rows = []
            all_ready = True
            for db in self.config.databases:
                final = self.state.database(db.name)["checkpoints"]["FINAL_LSN_CAPTURED"]["final_source_lsn"]
                ready, remaining, latest = self._progress(db, False, final)
                all_ready &= ready
                rows.append([db.name, final, latest or "unknown", "READY" if ready else f"{bytes_text(remaining)} remaining"])
            table(["DATABASE", "FINAL", "LATEST", "STATUS"], rows)
            if all_ready:
                for db in self.config.databases:
                    if self.state.database(db.name)["phase"] != "FORWARD_CAUGHT_UP":
                        self.state.set_phase(db.name, "FORWARD_CAUGHT_UP")
                return
            if timeout is not None and time.monotonic() - started >= timeout:
                raise ControllerError("wait-catchup timed out")
            time.sleep(interval)

    def disable_forward(self, dry_run: bool, resume: bool, ignore_missing: bool) -> None:
        self._require_phases("disable-forward", {"FORWARD_CAUGHT_UP", "FORWARD_DISABLED"})
        self._validate_batch_identities()
        for db in self.config.databases:
            phase = self.state.database(db.name)["phase"]
            info = self._target_info(db)
            if not info.get("subscription"):
                if not ignore_missing:
                    raise ControllerError(f"{db.name}: forward subscription is missing")
                checkpoint = self.state.database(db.name)["checkpoints"].get("forward_slot", {})
                slot_name = db.forward_slot or checkpoint.get("name")
                if not slot_name:
                    raise ControllerError(
                        f"{db.name}: cannot safely ignore a missing subscription without a recorded forward slot"
                    )
                source = self._source_info(db, slot_name)
                slot = source.get("slot")
                if not slot:
                    raise ControllerError(f"{db.name}: recorded forward slot is missing")
                if slot.get("active"):
                    raise ControllerError(f"{db.name}: cannot ignore a missing subscription while its slot is active")
            elif phase == "FORWARD_CAUGHT_UP":
                final = self.state.database(db.name)["checkpoints"]["FINAL_LSN_CAPTURED"]["final_source_lsn"]
                ready, _, _ = self._progress(db, False, final)
                if not ready:
                    raise ControllerError(f"{db.name}: forward replication has not reached the final LSN")
            else:
                problems = self._forward_disabled_problems(db)
                if problems:
                    raise ControllerError(
                        f"{db.name}: forward replication is not safely disabled: " + "; ".join(problems)
                    )

        def disable(db: Database) -> None:
            self._identity_line("target", db.name, self.target)
            info = self._target_info(db)
            if not info.get("subscription"):
                if not ignore_missing:
                    raise ControllerError("subscription missing")
                checkpoint = self.state.database(db.name)["checkpoints"].get("forward_slot", {})
                slot_name = db.forward_slot or checkpoint.get("name")
                source = self._source_info(db, slot_name) if slot_name else {}
                slot = source.get("slot")
                if not slot or slot.get("active"):
                    raise ControllerError("recorded forward slot is missing or active")
                self.state.set_phase(db.name, "FORWARD_DISABLED", ignored_missing=True)
                problems = self._forward_disabled_problems(db)
                if problems:
                    raise ControllerError("forward disabled-state validation failed: " + "; ".join(problems))
                return
            if info["subscription"].get("subenabled"):
                self.target.run(
                    db.name,
                    f"ALTER SUBSCRIPTION {quote_ident(db.forward_subscription)} DISABLE;",
                    read_only=False,
                )
            verified = self._target_info(db)
            if not verified.get("subscription"):
                raise ControllerError("subscription disappeared while it was being disabled")
            if verified["subscription"].get("subenabled") or self._main_worker(verified):
                raise ControllerError("subscription did not become disabled")
            problems = self._forward_disabled_problems(db)
            if problems:
                raise ControllerError("forward disabled-state validation failed: " + "; ".join(problems))
            self.state.set_phase(db.name, "FORWARD_DISABLED")

        self._batch("disable-forward", resume, dry_run, disable)

    def _forward_disabled_problems(self, db: Database) -> list[str]:
        subscriber = self._target_info(db)
        subscription = subscriber.get("subscription")
        checkpoint = self.state.database(db.name)["checkpoints"].get("forward_slot", {})
        slot_name = db.forward_slot or checkpoint.get("name")
        if not slot_name and subscription:
            slot_name = subscription.get("subslotname")
        publisher = self._source_info(db, slot_name) if slot_name else {}
        publication = publisher.get("publication")
        slot = publisher.get("slot")
        ignored_missing = bool(
            self.state.database(db.name)["checkpoints"].get("FORWARD_DISABLED", {}).get("ignored_missing")
        )
        problems: list[str] = []
        if not subscription:
            if not ignored_missing:
                problems.append("forward subscription missing")
        else:
            if subscription.get("subenabled"):
                problems.append("forward subscription enabled")
            if self._main_worker(subscriber):
                problems.append("forward apply worker still present")
            if set(subscription.get("subpublications", [])) != {db.forward_publication}:
                problems.append("forward subscription publication differs")
            if subscription.get("subslotname") != slot_name:
                problems.append("forward subscription slot differs")
            if self._table_set(subscriber.get("relations", [])) != self._manifest(db):
                problems.append("forward subscription table manifest differs")
        if not publication:
            problems.append("forward publication missing")
        else:
            if publication_actions(publication) != set(db.expected_publish):
                problems.append("forward publication actions differ")
            if self._table_set(publisher.get("tables", [])) != self._manifest(db):
                problems.append("forward publication table manifest differs")
        if not slot:
            problems.append("forward slot missing")
        else:
            if slot.get("slot_type") != "logical" or slot.get("plugin") != "pgoutput":
                problems.append("forward slot is not logical pgoutput")
            if slot.get("database") != db.name:
                problems.append("forward slot database differs")
            if slot.get("active"):
                problems.append("forward slot is still active")
            if slot.get("wal_status") not in {"reserved", "extended"}:
                problems.append(f"forward slot WAL status {slot.get('wal_status')}")
        return problems

    def _sequence_rows(self, runner: Psql, db: Database) -> list[dict[str, Any]]:
        rows = runner.file(db.name, "sequence_inventory.sql")
        for row in rows:
            if row["extension_owned"]:
                print(f"{db.name} {qualified(row['schema'], row['name'])} skipped extension-owned")
                continue
            qname = qualified(row["schema"], row["name"])
            state = runner.json(db.name, f"SELECT json_build_object('last_value', last_value, 'is_called', is_called)::text FROM {qname};")
            row.update(state)
        return rows

    def _sync_sequences_db(self, db: Database, publisher: Psql, subscriber: Psql) -> dict[str, int]:
        source_rows = {(r["schema"], r["name"]): r for r in self._sequence_rows(publisher, db) if not r["extension_owned"]}
        target_rows = {(r["schema"], r["name"]): r for r in self._sequence_rows(subscriber, db) if not r["extension_owned"]}
        if set(source_rows) != set(target_rows):
            missing = sorted(set(source_rows) ^ set(target_rows))
            raise ControllerError(f"sequence inventory differs: {missing}")
        result = {"changed": 0, "skipped": 0}
        for key in sorted(source_rows):
            source = source_rows[key]
            target = target_rows[key]
            definition = ("data_type", "increment_by", "min_value", "max_value", "cycle")
            if any(source[name] != target[name] for name in definition):
                raise ControllerError(f"{key[0]}.{key[1]} sequence definitions differ")
            if source["cycle"] and (source["last_value"], source["is_called"]) != (target["last_value"], target["is_called"]):
                raise ControllerError(f"{key[0]}.{key[1]} is cyclic with ambiguous ordering")
            increment = int(source["increment_by"])
            source_next = int(source["last_value"]) + (increment if source["is_called"] else 0)
            target_next = int(target["last_value"]) + (increment if target["is_called"] else 0)
            source_ahead = source_next > target_next if increment > 0 else source_next < target_next
            qname = qualified(*key)
            owner = "standalone"
            if source.get("owned_table"):
                owner = f"owned by {source['owned_schema']}.{source['owned_table']}.{source['owned_column']}"
            if source_ahead:
                sql = (
                    f"SELECT setval({quote_literal(qname)}::regclass, "
                    f"{int(source['last_value'])}, {'true' if source['is_called'] else 'false'});"
                )
                subscriber.run(db.name, sql, read_only=False)
                result["changed"] += 1
                print(f"{db.name} {qname} changed last={source['last_value']} called={source['is_called']} ({owner})")
            else:
                result["skipped"] += 1
                print(f"{db.name} {qname} skipped destination is equal or ahead ({owner})")
        return result

    def sync_sequences(self, dry_run: bool, resume: bool) -> None:
        self.state.require_all("FORWARD_DISABLED")
        self._validate_batch_identities()
        if self.state.data["writer"] != "none":
            raise ControllerError("SOURCE to TARGET sequence sync requires both sides to be quiescent")
        self._assert_quiescent(self.source, "SOURCE")
        self._assert_quiescent(self.target, "TARGET")
        invalid = [
            db.name for db in self.config.databases
            if self.state.database(db.name)["phase"] not in {"FORWARD_DISABLED", "SEQUENCES_SYNCED"}
        ]
        if invalid:
            raise ControllerError(f"sync-sequences is no longer valid for phase in: {', '.join(invalid)}")
        for db in self.config.databases:
            problems = self._forward_disabled_problems(db)
            if problems:
                raise ControllerError(f"{db.name}: forward replication is not safely disabled: " + "; ".join(problems))

        def sync(db: Database) -> None:
            self._identity_line("source", db.name, self.source)
            self._identity_line("target", db.name, self.target)
            result = self._sync_sequences_db(db, self.source, self.target)
            self.state.set_phase(db.name, "SEQUENCES_SYNCED", **result)

        self._batch("sync-sequences", resume, dry_run, sync)

    def _schema_inventory(self, runner: Psql, db: Database) -> list[dict[str, Any]]:
        return runner.file(db.name, "schema_inventory.sql")

    def _schema_snapshot(self, runner: Psql, db: Database) -> dict[str, Any]:
        sequences = runner.file(db.name, "sequence_inventory.sql")
        return {"tables": self._schema_inventory(runner, db), "sequences": sequences}

    def _schema_drift(self, db: Database) -> bool:
        checkpoint = self.state.database(db.name)["checkpoints"].get("REVERSE_PREPARED", {})
        if not checkpoint:
            return False
        return (
            fingerprint(self._schema_snapshot(self.source, db)) != checkpoint.get("source_schema_fingerprint")
            or fingerprint(self._schema_snapshot(self.target, db)) != checkpoint.get("target_schema_fingerprint")
        )

    def _check_direction(
        self,
        db: Database,
        published_rows: list[dict[str, Any]],
        publisher_runner: Psql,
        subscriber_runner: Psql,
    ) -> tuple[str, str]:
        publisher_snapshot = self._schema_snapshot(publisher_runner, db)
        subscriber_snapshot = self._schema_snapshot(subscriber_runner, db)
        publisher_rows = publisher_snapshot["tables"]
        subscriber_rows = subscriber_snapshot["tables"]
        publisher_map = schema_map(publisher_rows)
        subscriber_map = schema_map(subscriber_rows)
        manifest = self._manifest(db)
        if not manifest <= set(publisher_map) or not manifest <= set(subscriber_map):
            raise ControllerError(f"{db.name}: a manifested table is missing on one side")
        needs_identity = bool({"update", "delete"} & set(db.expected_publish))
        pub_by_name = {(r["schemaname"], r["tablename"]): r for r in published_rows}
        if set(pub_by_name) != manifest:
            raise ControllerError(f"{db.name}: published table manifest differs")
        for name in sorted(manifest):
            publisher = publisher_map[name]
            subscriber = subscriber_map[name]
            published = set(pub_by_name[name].get("attnames") or [])
            pub_columns = {c["name"]: c for c in publisher["columns"]}
            sub_columns = {c["name"]: c for c in subscriber["columns"]}
            for column in published:
                if column not in pub_columns or column not in sub_columns:
                    raise ControllerError(f"{db.name}: published column {name}.{column} is missing")
                if sub_columns[column]["generated"]:
                    raise ControllerError(
                        f"{db.name}: published column {name}.{column} targets a generated subscriber column"
                    )
                if not _type_lossless(pub_columns[column], sub_columns[column]):
                    raise ControllerError(f"{db.name}: reverse type conversion is not demonstrably lossless for {name}.{column}")
            for column, metadata in sub_columns.items():
                if column not in published and metadata["not_null"] and not metadata["has_default"] and not metadata["generated"] and not metadata["identity"]:
                    raise ControllerError(f"{db.name}: subscriber-only column {name}.{column} cannot accept replicated rows")
            if needs_identity:
                if publisher["replica_identity"] == "n":
                    raise ControllerError(f"{db.name}: {name} has no publisher replica identity")
                if publisher["replica_identity"] != "f" and not publisher["identity_columns"]:
                    raise ControllerError(f"{db.name}: {name} has no usable publisher key")
                if publisher["replica_identity"] != "f" and not set(publisher["identity_columns"]) <= set(sub_columns):
                    raise ControllerError(f"{db.name}: subscriber lacks replica identity columns for {name}")
                if publisher["replica_identity"] != "f" and subscriber["replica_identity"] == "n":
                    raise ControllerError(f"{db.name}: subscriber has no replica identity for {name}")
                if (
                    publisher["replica_identity"] != "f"
                    and subscriber["replica_identity"] != "f"
                    and not set(subscriber["identity_columns"]) <= set(publisher["identity_columns"])
                ):
                    raise ControllerError(f"{db.name}: subscriber replica identity is incompatible for {name}")
        return fingerprint(publisher_snapshot), fingerprint(subscriber_snapshot)

    def _check_schema(self, db: Database, published_rows: list[dict[str, Any]]) -> tuple[str, str]:
        # Reverse direction is NEW publisher -> OLD subscriber. Return values are
        # stored in SOURCE, TARGET order for later drift comparisons.
        target_fp, source_fp = self._check_direction(db, published_rows, self.target, self.source)
        return source_fp, target_fp

    def _publication_ddl(self, db: Database, rows: list[dict[str, Any]], publication: dict[str, Any]) -> str:
        actions = ", ".join(sorted(db.expected_publish))
        via_root = "true" if publication.get("pubviaroot") else "false"
        statements = [
            "BEGIN;",
            f"CREATE PUBLICATION {quote_ident(db.reverse_publication)} "
            f"WITH (publish = {quote_literal(actions)}, publish_via_partition_root = {via_root});"
        ]
        for row in rows:
            columns = row.get("attnames") or []
            column_sql = " (" + ", ".join(quote_ident(value) for value in columns) + ")" if columns else ""
            filter_sql = f" WHERE ({row['rowfilter']})" if row.get("rowfilter") else ""
            statements.append(
                f"ALTER PUBLICATION {quote_ident(db.reverse_publication)} ADD TABLE "
                f"{qualified(row['schemaname'], row['tablename'])}{column_sql}{filter_sql};"
            )
        statements.append("COMMIT;")
        return "\n".join(statements)

    def prepare_reverse(self, dry_run: bool, resume: bool) -> None:
        self.state.require_all("SEQUENCES_SYNCED")
        self._validate_batch_identities()
        if self.state.data["writer"] != "none":
            raise ControllerError("reverse protection must be prepared while both sides are quiescent")
        self._assert_quiescent(self.source, "SOURCE")
        self._assert_quiescent(self.target, "TARGET")
        invalid = [
            db.name for db in self.config.databases
            if self.state.database(db.name)["phase"] not in {"SEQUENCES_SYNCED", "REVERSE_PREPARED"}
        ]
        if invalid:
            raise ControllerError(f"prepare-reverse is no longer valid for phase in: {', '.join(invalid)}")
        for db in self.config.databases:
            problems = self._forward_disabled_problems(db)
            if problems:
                raise ControllerError(f"{db.name}: forward replication is not safely disabled: " + "; ".join(problems))
        capacity = self._capacity()
        if capacity["issues"]:
            raise ControllerError("; ".join(capacity["issues"]))
        forward: dict[str, dict[str, Any]] = {}
        for db in self.config.databases:
            source = self._source_info(db, self._forward_slot(db))
            if self._table_set(source["tables"]) != self._manifest(db):
                raise ControllerError(f"{db.name}: forward publication drifted from manifest")
            old_fp, new_fp = self._check_schema(db, source["tables"])
            forward[db.name] = {"info": source, "old_fp": old_fp, "new_fp": new_fp}

        def prepare(db: Database) -> None:
            self._identity_line("target", db.name, self.target)
            self._identity_line("source", db.name, self.source)
            source = forward[db.name]["info"]
            existing_pub = self.target.file(db.name, "precheck_source.sql", {
                "publication": db.reverse_publication, "slot": db.reverse_slot,
            })
            if existing_pub.get("publication"):
                if self._table_set(existing_pub["tables"]) != self._manifest(db):
                    raise ControllerError("existing reverse publication table set differs")
                if publication_actions(existing_pub["publication"]) != set(db.expected_publish):
                    raise ControllerError("existing reverse publication actions differ")
                expected_rules = sorted((r["schemaname"], r["tablename"], r.get("attnames"), r.get("rowfilter")) for r in source["tables"])
                actual_rules = sorted((r["schemaname"], r["tablename"], r.get("attnames"), r.get("rowfilter")) for r in existing_pub["tables"])
                if actual_rules != expected_rules or bool(existing_pub["publication"].get("pubviaroot")) != bool(source["publication"].get("pubviaroot")):
                    raise ControllerError("existing reverse publication column, filter, or partition rules differ")
            else:
                self.target.run(db.name, self._publication_ddl(db, source["tables"], source["publication"]), read_only=False)
            conninfo = self.target.reverse_conninfo(db.name)
            existing_sub = self._target_info(db, reverse=True).get("subscription")
            if existing_sub:
                if existing_sub.get("subenabled"):
                    raise ControllerError("existing reverse subscription must be disabled during preparation")
                if existing_sub.get("subslotname") != db.reverse_slot or existing_sub.get("suborigin") != "none":
                    raise ControllerError("existing reverse subscription options differ")
                check = (
                    "SELECT json_build_object('matches', subconninfo = " + quote_literal(conninfo) + ")::text "
                    "FROM pg_subscription WHERE subdbid = (SELECT oid FROM pg_database WHERE datname=current_database()) "
                    f"AND subname={quote_literal(db.reverse_subscription)};"
                )
                if not self.source.json(db.name, check, contains_secret=True).get("matches"):
                    raise ControllerError("existing reverse subscription connection differs")
            else:
                sql = (
                    f"CREATE SUBSCRIPTION {quote_ident(db.reverse_subscription)} CONNECTION {quote_literal(conninfo)} "
                    f"PUBLICATION {quote_ident(db.reverse_publication)} WITH (copy_data=false, enabled=false, "
                    f"create_slot=true, slot_name={quote_literal(db.reverse_slot)}, origin=none, binary=false, "
                    "streaming=on, disable_on_error=true);"
                )
                self.source.run(db.name, sql, read_only=False, contains_secret=True)
            problems, _, _ = self._reverse_status(db, require_active=False)
            if problems:
                raise ControllerError("reverse preparation validation failed: " + "; ".join(problems))
            self.state.set_phase(
                db.name, "REVERSE_PREPARED",
                source_schema_fingerprint=forward[db.name]["old_fp"],
                target_schema_fingerprint=forward[db.name]["new_fp"],
            )

        self._batch("prepare-reverse", resume, dry_run, prepare)

    def enable_reverse(self, dry_run: bool, resume: bool) -> None:
        self.state.require_all("REVERSE_PREPARED")
        self._validate_batch_identities()
        if self.state.data["writer"] != "none":
            raise ControllerError("reverse subscriptions must be enabled before TARGET writes begin")
        self._assert_quiescent(self.source, "SOURCE")
        self._assert_quiescent(self.target, "TARGET")
        invalid = [
            db.name for db in self.config.databases
            if self.state.database(db.name)["phase"] not in {"REVERSE_PREPARED", "REVERSE_ACTIVE"}
        ]
        if invalid:
            raise ControllerError(f"enable-reverse is no longer valid for phase in: {', '.join(invalid)}")

        def enable(db: Database) -> None:
            self._identity_line("source", db.name, self.source)
            sub = self._target_info(db, reverse=True).get("subscription")
            if not sub:
                raise ControllerError("reverse subscription missing")
            if not sub.get("subenabled"):
                self.source.run(db.name, f"ALTER SUBSCRIPTION {quote_ident(db.reverse_subscription)} ENABLE;", read_only=False)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                current = self._target_info(db, reverse=True)
                slot = self.target.file(db.name, "precheck_source.sql", {
                    "publication": db.reverse_publication, "slot": db.reverse_slot,
                }).get("slot") or {}
                if self._main_worker(current) and slot.get("active"):
                    problems, _, _ = self._reverse_status(db, require_active=True)
                    if problems:
                        raise ControllerError("reverse activation validation failed: " + "; ".join(problems))
                    self.state.set_phase(db.name, "REVERSE_ACTIVE")
                    return
                time.sleep(1)
            raise ControllerError("reverse worker or slot did not become active within 60 seconds")

        self._batch("enable-reverse", resume, dry_run, enable)

    def confirm_cutover(self, confirmed: bool) -> None:
        if not confirmed:
            raise ControllerError("confirm-cutover requires --confirm-target-only-writable")
        self._require_phases("confirm-cutover", {"REVERSE_ACTIVE", "CUTOVER_COMPLETE"})
        if self.state.data["writer"] not in {"none", "target"}:
            raise ControllerError("state does not show a frozen or TARGET writer")
        self._validate_batch_identities()
        for db in self.config.databases:
            if self._schema_drift(db):
                raise ControllerError(f"{db.name}: schema drift detected before cutover acceptance")
            problems, _, _ = self._reverse_status(db, require_active=True)
            if problems:
                raise ControllerError(f"{db.name}: reverse validation failed: {'; '.join(problems)}")
        for db in self.config.databases:
            if self.state.database(db.name)["phase"] != "CUTOVER_COMPLETE":
                self.state.set_phase(db.name, "CUTOVER_COMPLETE", confirmed_by=os.environ.get("USER", "unknown"))
        self.state.data["writer"] = "target"
        self.state.save()
        print("TARGET is recorded as the only application writer; reverse protection is active.")

    def _resolve_test_table(self, db: Database, value: str | None) -> tuple[str, str] | None:
        if value:
            parts = value.split(".", 1)
            if len(parts) != 2 or not all(parts):
                raise ControllerError("--test-table must be schema.table")
            return parts[0], parts[1]
        return db.test_table

    def _wait_probe_count(self, runner: Psql, db: Database, qname: str, probe_id: str, expected: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            count = runner.run(
                db.name,
                f"SELECT count(*) FROM {qname} WHERE cutover_test_id={quote_literal(probe_id)}::uuid;",
            )
            if count == expected:
                return True
            time.sleep(1)
        return False

    def _probe(self, db: Database, value: str | None, timeout: float, quiet: bool = False) -> None:
        relation = self._resolve_test_table(db, value)
        if not relation or relation not in self._manifest(db):
            raise ControllerError(f"{db.name}: probe table must be in expected_tables")
        qname = qualified(*relation)
        contract = self.target.json(db.name, (
            "SELECT json_build_object('comment', obj_description(" + quote_literal(qname) + "::regclass, 'pg_class'), "
            "'columns', (SELECT json_agg(attname ORDER BY attnum) FROM pg_attribute WHERE attrelid=" +
            quote_literal(qname) + "::regclass AND attnum>0 AND NOT attisdropped))::text;"
        ))
        if contract.get("comment") != "cutover-controller replication probe v1" or contract.get("columns") != ["cutover_test_id", "created_at", "marker"]:
            raise ControllerError(f"{db.name}: test table does not match the documented probe contract")
        inventory = schema_map(self._schema_inventory(self.target, db))
        if relation not in inventory:
            raise ControllerError(f"{db.name}: probe table is missing from schema inventory")
        metadata = inventory[relation]["columns"]
        expected_types = {
            "cutover_test_id": "uuid",
            "created_at": "timestamp with time zone",
            "marker": "text",
        }
        if {column["name"]: column["type"] for column in metadata} != expected_types:
            raise ControllerError(f"{db.name}: test table column types do not match the probe contract")
        if not next(c for c in metadata if c["name"] == "created_at")["has_default"]:
            raise ControllerError(f"{db.name}: probe created_at requires a default")
        active = self.state.database(db.name)["checkpoints"].get("active_probe")
        if active:
            old_relation = tuple(active["table"])
            if old_relation != relation:
                raise ControllerError(f"{db.name}: unfinished probe references another table")
            old_id = active["id"]
            self.target.run(
                db.name,
                f"DELETE FROM {qname} WHERE cutover_test_id={quote_literal(old_id)}::uuid;",
                read_only=False,
            )
            if not self._wait_probe_count(self.source, db, qname, old_id, "0", timeout):
                raise ControllerError(f"{db.name}: could not clean up unfinished probe {old_id}")
            del self.state.database(db.name)["checkpoints"]["active_probe"]
            self.state.save()
        probe_id = str(uuid.uuid4())
        marker = f"cutover-controller:{probe_id}"
        self.state.database(db.name)["checkpoints"]["active_probe"] = {"id": probe_id, "table": list(relation), "started_at": now()}
        self.state.save()
        self.target.run(db.name, (
            f"INSERT INTO {qname} (cutover_test_id, marker) VALUES "
            f"({quote_literal(probe_id)}::uuid, {quote_literal(marker)});"
        ), read_only=False)
        inserted = self._wait_probe_count(self.source, db, qname, probe_id, "1", timeout)
        self.target.run(db.name, f"DELETE FROM {qname} WHERE cutover_test_id={quote_literal(probe_id)}::uuid;", read_only=False)
        cleaned = self._wait_probe_count(self.source, db, qname, probe_id, "0", timeout)
        if cleaned:
            del self.state.database(db.name)["checkpoints"]["active_probe"]
            self.state.save()
        if not inserted:
            raise ControllerError(f"{db.name}: probe insert did not replicate (publisher row was cleaned up)")
        if not cleaned:
            raise ControllerError(f"{db.name}: probe delete did not replicate")
        if not quiet:
            print(f"{db.name}: replication probe insert and delete verified")

    def verify_reverse(self, test_table: str | None, run_test: bool, timeout: float, as_json: bool) -> None:
        if timeout <= 0:
            raise ControllerError("probe timeout must be positive")
        self._require_phases("verify-reverse", {
            "REVERSE_ACTIVE", "CUTOVER_COMPLETE", "ROLLBACK_WRITES_FROZEN",
            "ROLLBACK_LSN_CAPTURED", "ROLLBACK_CAUGHT_UP", "ROLLBACK_SEQUENCES_SYNCED",
        })
        self._validate_batch_identities()
        details = []
        rows = []
        healthy = True
        for db in self.config.databases:
            if self._schema_drift(db):
                raise ControllerError(f"{db.name}: schema drift detected during rollback window")
            problems, publisher, subscriber = self._reverse_status(db, require_active=True)
            slot = publisher.get("slot") or {}
            stats = subscriber.get("stats") or {}
            if slot.get("lag_bytes") is not None and slot["lag_bytes"] > self.config.healthy_lag_bytes:
                problems.append(f"lag exceeds {self.config.healthy_lag_bytes} bytes")
            ok = not problems
            healthy &= ok
            rows.append([
                db.name, str(bool(slot.get("active"))).lower(), bytes_text(slot.get("lag_bytes")),
                stats.get("apply_error_count", 0), stats.get("sync_error_count", 0),
                "HEALTHY" if ok else "UNHEALTHY",
            ])
            details.append({
                "database": db.name, "slot": slot, "subscription": subscriber,
                "problems": problems, "healthy": ok,
            })
            if run_test or test_table:
                self._probe(db, test_table, timeout, quiet=as_json)
        if as_json:
            emit({"healthy": healthy, "databases": details}, True)
        else:
            table(["DATABASE", "SLOT_ACTIVE", "LAG", "APPLY_ERRORS", "SYNC_ERRORS", "STATUS"], rows)
            for item in details:
                if item["problems"]:
                    print(f"{item['database']}: " + "; ".join(item["problems"]))
        if not healthy:
            raise ControllerError("reverse verification failed")

    def rollback_precheck(self, confirmed: bool, interval: float, timeout: float | None, resume: bool) -> None:
        if not confirmed:
            raise ControllerError("rollback-precheck requires --confirm-target-writes-frozen")
        if interval <= 0 or (timeout is not None and timeout < 0):
            raise ControllerError("rollback interval must be positive and timeout cannot be negative")
        self._require_phases("rollback-precheck", {
            "CUTOVER_COMPLETE", "ROLLBACK_WRITES_FROZEN", "ROLLBACK_LSN_CAPTURED",
            "ROLLBACK_CAUGHT_UP", "ROLLBACK_SEQUENCES_SYNCED",
        })
        if self.state.data["writer"] not in {"target", "none"}:
            raise ControllerError("state does not designate TARGET as the current writer")
        self._validate_batch_identities()
        self._assert_quiescent(self.target, "TARGET")
        for db in self.config.databases:
            if self._schema_drift(db):
                raise ControllerError(f"{db.name}: schema drift detected; rollback is blocked")
        self.state.data["writer"] = "none"
        self.state.save()
        self.state.prepare_batch("rollback-precheck", resume)
        for db in self.config.databases:
            entry = self.state.database(db.name)
            if "ROLLBACK_LSN_CAPTURED" not in entry["checkpoints"]:
                try:
                    self.state.set_phase(db.name, "ROLLBACK_WRITES_FROZEN", confirmed_by=os.environ.get("USER", "unknown"))
                    lsn = self.target.run(db.name, "SELECT pg_current_wal_lsn()::text;")
                    if not lsn_int(lsn):
                        raise ControllerError(f"{db.name}: failed to capture a valid TARGET LSN")
                    self.state.set_phase(db.name, "ROLLBACK_LSN_CAPTURED", final_target_lsn=lsn, captured_at=now())
                except Exception as exc:
                    self.state.operation(db.name, "rollback-precheck", "FAILED", str(exc))
                    table(["DATABASE", "rollback-precheck"], self.state.operation_rows("rollback-precheck"))
                    print("Resume with: ./cutover rollback-precheck --confirm-target-writes-frozen --resume")
                    raise PartialFailure(f"rollback-precheck capture failed for {db.name}: {exc}") from exc
        started = time.monotonic()
        while True:
            all_ready = True
            rows = []
            for db in self.config.databases:
                final = self.state.database(db.name)["checkpoints"]["ROLLBACK_LSN_CAPTURED"]["final_target_lsn"]
                ready, remaining, latest = self._progress(db, True, final)
                all_ready &= ready
                rows.append([db.name, final, latest or "unknown", "READY" if ready else f"{bytes_text(remaining)} remaining"])
            table(["DATABASE", "FINAL_TARGET", "LATEST", "STATUS"], rows)
            if all_ready:
                break
            if timeout is not None and time.monotonic() - started >= timeout:
                raise ControllerError("rollback catchup timed out")
            time.sleep(interval)
        self._assert_quiescent(self.source, "SOURCE")
        self._assert_quiescent(self.target, "TARGET")
        for db in self.config.databases:
            try:
                self.state.operation(db.name, "rollback-precheck", "RUNNING")
                self.state.set_phase(db.name, "ROLLBACK_CAUGHT_UP")
                result = self._sync_sequences_db(db, self.target, self.source)
                self.state.set_phase(db.name, "ROLLBACK_SEQUENCES_SYNCED", **result)
                self.state.operation(db.name, "rollback-precheck", "COMPLETE")
            except Exception as exc:
                self.state.operation(db.name, "rollback-precheck", "FAILED", str(exc))
                table(["DATABASE", "rollback-precheck"], self.state.operation_rows("rollback-precheck"))
                print("Resume with: ./cutover rollback-precheck --confirm-target-writes-frozen --resume")
                raise PartialFailure(f"rollback-precheck sequence sync failed for {db.name}: {exc}") from exc
        table(["DATABASE", "rollback-precheck"], self.state.operation_rows("rollback-precheck"))
        print("Rollback precheck complete: reverse replication is caught up and OLD sequences are synchronized.")

    def rollback(self, dry_run: bool, resume: bool) -> None:
        self._require_phases("rollback", {"ROLLBACK_SEQUENCES_SYNCED", "ROLLED_BACK"})
        if self.state.data["writer"] != "none":
            raise ControllerError("rollback requires TARGET writes to remain frozen")
        self._validate_batch_identities()
        self._assert_quiescent(self.source, "SOURCE")
        self._assert_quiescent(self.target, "TARGET")
        for db in self.config.databases:
            phase = self.state.database(db.name)["phase"]
            if phase == "ROLLBACK_SEQUENCES_SYNCED":
                if self._schema_drift(db):
                    raise ControllerError(f"{db.name}: schema drift detected before rollback")
                final = self.state.database(db.name)["checkpoints"]["ROLLBACK_LSN_CAPTURED"]["final_target_lsn"]
                ready, _, _ = self._progress(db, True, final)
                if not ready:
                    raise ControllerError(f"{db.name}: reverse replication has not reached the final TARGET LSN")
            else:
                problems, _, _ = self._reverse_status(db, require_active=False)
                if problems:
                    raise ControllerError(
                        f"{db.name}: rolled-back topology is unsafe: " + "; ".join(problems)
                    )

        def disable(db: Database) -> None:
            self._identity_line("source", db.name, self.source)
            info = self._target_info(db, reverse=True)
            if not info.get("subscription"):
                raise ControllerError("reverse subscription missing")
            if info["subscription"].get("subenabled"):
                self.source.run(
                    db.name,
                    f"ALTER SUBSCRIPTION {quote_ident(db.reverse_subscription)} DISABLE;",
                    read_only=False,
                )
            verified = self._target_info(db, reverse=True)
            if not verified.get("subscription"):
                raise ControllerError("reverse subscription disappeared while it was being disabled")
            if verified["subscription"].get("subenabled") or self._main_worker(verified):
                raise ControllerError("reverse subscription is still enabled or its worker is present")
            slot = self.target.file(db.name, "precheck_source.sql", {
                "publication": db.reverse_publication, "slot": db.reverse_slot,
            }).get("slot") or {}
            if slot.get("active"):
                raise ControllerError("reverse slot is still active; wait briefly and resume")
            problems, _, _ = self._reverse_status(db, require_active=False)
            if problems:
                raise ControllerError("reverse disabled-state validation failed: " + "; ".join(problems))
            self.state.set_phase(db.name, "ROLLED_BACK")

        self._batch("rollback", resume, dry_run, disable)
        if not dry_run:
            self.state.data["writer"] = "source"
            self.state.save()
            print("Reverse subscriptions are disabled. Application connections may now be moved back to OLD.")

    def _cleanup_preflight(self, db: Database, *, allow_detached: bool) -> dict[str, Any]:
        forward_subscriber = self._target_info(db)
        reverse_subscriber = self._target_info(db, reverse=True)
        forward_sub = forward_subscriber.get("subscription")
        reverse_sub = reverse_subscriber.get("subscription")
        forward_slot = self._forward_slot(db, {"subscription": forward_sub})
        forward_publisher = self._source_info(db, forward_slot)
        reverse_publisher = self.target.file(db.name, "precheck_source.sql", {
            "publication": db.reverse_publication, "slot": db.reverse_slot,
        })
        checks = (
            ("forward", forward_sub, forward_subscriber, forward_publisher,
             db.forward_publication, forward_slot),
            ("reverse", reverse_sub, reverse_subscriber, reverse_publisher,
             db.reverse_publication, db.reverse_slot),
        )
        problems: list[str] = []
        if self.state.database(db.name)["phase"] != "FINALIZED" and self._schema_drift(db):
            problems.append("schema drift detected")
        for label, subscription, subscriber, publisher, publication_name, slot_name in checks:
            publication = publisher.get("publication")
            slot = publisher.get("slot")
            if subscription:
                if set(subscription.get("subpublications", [])) != {publication_name}:
                    problems.append(f"{label} subscription publication differs")
                actual_slot = subscription.get("subslotname")
                if actual_slot != slot_name and not (allow_detached and actual_slot is None):
                    problems.append(f"{label} subscription slot differs")
                if self._table_set(subscriber.get("relations", [])) != self._manifest(db):
                    problems.append(f"{label} subscription table manifest differs")
                if not publication:
                    problems.append(f"{label} publication missing while subscription exists")
            if publication:
                if publication_actions(publication) != set(db.expected_publish):
                    problems.append(f"{label} publication actions differ")
                if self._table_set(publisher.get("tables", [])) != self._manifest(db):
                    problems.append(f"{label} publication table manifest differs")
            if slot:
                if slot.get("slot_type") != "logical" or slot.get("plugin") != "pgoutput":
                    problems.append(f"{label} slot is not logical pgoutput")
                if slot.get("database") != db.name:
                    problems.append(f"{label} slot database differs")
            elif subscription and subscription.get("subslotname") is not None:
                problems.append(f"{label} subscription slot is missing")
        if forward_publisher.get("publication") and reverse_publisher.get("publication"):
            if publication_rules(forward_publisher.get("tables", [])) != publication_rules(reverse_publisher.get("tables", [])):
                problems.append("forward and reverse publication rules differ")
            if bool(forward_publisher["publication"].get("pubviaroot")) != bool(reverse_publisher["publication"].get("pubviaroot")):
                problems.append("forward and reverse partition-root rules differ")
        if problems:
            raise ControllerError(f"{db.name}: cleanup preflight failed: " + "; ".join(problems))
        return {
            "forward": forward_sub, "reverse": reverse_sub,
            "forward_slot": forward_slot,
        }

    def finalize_plan(self, as_json: bool = False) -> dict[str, Any]:
        plan = {"warning": "Executing this plan permanently removes rollback replication.", "databases": []}
        for db in self.config.databases:
            self.source.validate_identity(db.name)
            self.target.validate_identity(db.name)
            forward = self._target_info(db).get("subscription")
            reverse = self._target_info(db, reverse=True).get("subscription")
            plan["databases"].append({
                "database": db.name,
                "disable": [db.reverse_subscription] if reverse and reverse.get("subenabled") else [],
                "drop_subscriptions": [name for name, value in ((db.forward_subscription, forward), (db.reverse_subscription, reverse)) if value],
                "drop_publications": [db.forward_publication, db.reverse_publication],
                "associated_slots": [self._forward_slot(db, {"subscription": forward}), db.reverse_slot],
            })
        if as_json:
            emit(plan, True)
        else:
            print("WARNING: FINALIZATION PERMANENTLY REMOVES ROLLBACK CAPABILITY")
            for item in plan["databases"]:
                print(f"{item['database']}: disable={item['disable']} drop_subscriptions={item['drop_subscriptions']} drop_publications={item['drop_publications']} slots={item['associated_slots']}")
        return plan

    def finalize_execute(self, confirmed: bool, resume: bool, dry_run: bool) -> None:
        if not confirmed:
            raise ControllerError("finalize --execute requires --confirm-no-rollback")
        if self.state.data["writer"] != "target":
            raise ControllerError("finalization is allowed only for an accepted cutover with TARGET as writer")
        self._require_phases("finalize", {"CUTOVER_COMPLETE", "FINALIZED"})
        self._validate_batch_identities()
        self.finalize_plan()
        for db in self.config.databases:
            self._cleanup_preflight(
                db, allow_detached=resume or self.state.database(db.name)["phase"] == "FINALIZED"
            )

        def disable_all(db: Database) -> None:
            self._identity_line("source", db.name, self.source)
            self._identity_line("target", db.name, self.target)
            objects = self._cleanup_preflight(db, allow_detached=resume)
            reverse = objects["reverse"]
            forward = objects["forward"]
            if reverse and reverse.get("subenabled"):
                self.source.run(
                    db.name, f"ALTER SUBSCRIPTION {quote_ident(db.reverse_subscription)} DISABLE;",
                    read_only=False,
                )
            if forward and forward.get("subenabled"):
                raise ControllerError("forward subscription unexpectedly enabled")
            current = self._target_info(db, reverse=True).get("subscription")
            if current and current.get("subenabled"):
                raise ControllerError("reverse subscription did not disable")

        self._batch("finalize-disable", resume, dry_run, disable_all)
        if dry_run:
            return
        for db in self.config.databases:
            forward = self._target_info(db).get("subscription")
            reverse = self._target_info(db, reverse=True).get("subscription")
            if (forward and forward.get("subenabled")) or (reverse and reverse.get("subenabled")):
                raise ControllerError(f"{db.name}: all subscriptions must be disabled before cleanup")

        def cleanup(db: Database) -> None:
            self._identity_line("source", db.name, self.source)
            self._identity_line("target", db.name, self.target)
            objects = self._cleanup_preflight(db, allow_detached=resume)
            subscriptions = (
                (self.source, db.reverse_subscription, objects["reverse"]),
                (self.target, db.forward_subscription, objects["forward"]),
            )
            for runner, name, subscription in subscriptions:
                if not subscription:
                    continue
                if subscription.get("subslotname") is not None:
                    runner.run(
                        db.name,
                        f"ALTER SUBSCRIPTION {quote_ident(name)} SET (slot_name = NONE);",
                        read_only=False,
                    )
                    reverse = name == db.reverse_subscription
                    detached = self._target_info(db, reverse=reverse).get("subscription")
                    if not detached or detached.get("subslotname") is not None:
                        raise ControllerError(f"subscription {name} did not detach from its slot")
                runner.run(db.name, f"DROP SUBSCRIPTION {quote_ident(name)};", read_only=False)
            slots = ((self.target, db.reverse_slot, db.reverse_publication),
                     (self.source, objects["forward_slot"], db.forward_publication))
            for runner, slot_name, publication_name in slots:
                if not slot_name:
                    continue
                slot = runner.file(db.name, "precheck_source.sql", {
                    "publication": publication_name, "slot": slot_name,
                }).get("slot")
                if slot:
                    if (slot.get("active") or slot.get("slot_type") != "logical"
                            or slot.get("database") != db.name or slot.get("plugin") != "pgoutput"):
                        raise ControllerError(f"refusing to drop mismatched or active orphan slot {slot_name}")
                    runner.run(
                        db.name, f"SELECT pg_drop_replication_slot({quote_literal(slot_name)});",
                        read_only=False,
                    )
            self.source.run(
                db.name, f"DROP PUBLICATION IF EXISTS {quote_ident(db.forward_publication)};",
                read_only=False,
            )
            self.target.run(
                db.name, f"DROP PUBLICATION IF EXISTS {quote_ident(db.reverse_publication)};",
                read_only=False,
            )
            self.state.set_phase(db.name, "FINALIZED")

        self._batch("finalize", resume, dry_run, cleanup)
