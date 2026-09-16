from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.config import Config, ConfigError, Database, Endpoint
from lib.controller import Controller, PartialFailure, _type_lossless, lsn_int
from lib.pg import PostgreSQLError, Psql, libpq_value, qualified, quote_ident, quote_literal
from lib.state import StateError, StateStore


def config_text(databases: int = 2) -> str:
    entries = []
    for number in range(1, databases + 1):
        name = f"db{number:02d}"
        entries.append(
            f"""
  - name: {name}
    forward_publication: pub_{name}
    forward_subscription: sub_{name}
    reverse_publication: pub_rev_{name}
    reverse_subscription: sub_rev_{name}
    expected_tables:
      - schema: public
        table: things
"""
        )
    return (
        """
source:
  host: old.example
  user: admin
  expected_system_identifier: "111"
target:
  host: new.example
  user: admin
  expected_system_identifier: "222"
databases:
"""
        + "".join(entries)
    )


class ConfigTests(unittest.TestCase):
    def load(self, text: str) -> Config:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return Config.load(path)

    def test_valid_config_and_defaults(self) -> None:
        config = self.load(config_text())
        self.assertEqual([db.name for db in config.databases], ["db01", "db02"])
        self.assertEqual(config.databases[0].reverse_slot, "sub_rev_db01")
        self.assertEqual(config.databases[0].expected_publish, {"insert", "update", "delete", "truncate"})

    def test_plaintext_password_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "password is forbidden"):
            self.load(config_text().replace("user: admin", "user: admin\n  password: secret", 1))

    def test_same_cluster_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must be different"):
            self.load(config_text().replace('"222"', '"111"'))

    def test_table_manifest_is_required(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(config_text().replace("    expected_tables:", "    missing_tables:", 1))

    def test_reverse_slots_must_be_cluster_wide_unique(self) -> None:
        text = config_text().replace("reverse_subscription: sub_rev_db02", "reverse_subscription: sub_rev_db01")
        with self.assertRaisesRegex(ConfigError, "cluster-wide unique"):
            self.load(text)

    def test_non_finite_poll_interval_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "poll interval"):
            self.load(config_text() + "\nsettings:\n  poll_interval_seconds: .nan\n")


class QuotingTests(unittest.TestCase):
    def test_identifier_quoting(self) -> None:
        self.assertEqual(quote_ident('odd"name'), '"odd""name"')
        self.assertEqual(qualified("odd.schema", 't"x'), '"odd.schema"."t""x"')

    def test_literal_quoting_handles_quotes_and_backslashes(self) -> None:
        self.assertEqual(quote_literal("a'b\\c"), "E'a''b\\\\c'")

    def test_libpq_value_escaping(self) -> None:
        self.assertEqual(libpq_value("p'a\\b"), "'p\\'a\\\\b'")

    def test_nul_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            quote_ident("bad\x00name")


class LsnAndTypeTests(unittest.TestCase):
    def test_lsn_math(self) -> None:
        self.assertEqual(lsn_int("1/0"), 1 << 32)
        self.assertEqual(lsn_int("0/10"), 16)
        self.assertIsNone(lsn_int(None))

    def test_only_lossless_integer_widening_is_accepted(self) -> None:
        self.assertTrue(_type_lossless({"type": "integer"}, {"type": "bigint"}))
        self.assertFalse(_type_lossless({"type": "bigint"}, {"type": "integer"}))
        self.assertTrue(_type_lossless({"type": "text"}, {"type": "text"}))
        self.assertFalse(_type_lossless({"type": "text"}, {"type": "varchar"}))


class PsqlBoundaryTests(unittest.TestCase):
    def test_password_is_environment_only(self) -> None:
        endpoint = Endpoint(
            name="source", host="old", port=5432, user="admin", sslmode="require",
            system_identifier="1", password_env="TEST_CUTOVER_PASSWORD",
            replication_user=None, replication_password_env=None, connect_timeout=10,
        )
        logger = logging.getLogger("test-psql")
        with patch.dict(os.environ, {"TEST_CUTOVER_PASSWORD": "do-not-leak"}, clear=False):
            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "ok\n", "")
                output = Psql(endpoint, ROOT / "sql", logger).run("db", "SELECT 1;")
        self.assertEqual(output, "ok")
        args = run.call_args.args[0]
        self.assertNotIn("do-not-leak", " ".join(args))
        self.assertEqual(run.call_args.kwargs["env"]["PGPASSWORD"], "do-not-leak")
        self.assertIn("default_transaction_read_only", run.call_args.kwargs["input"])

    def test_secret_error_does_not_echo_server_message(self) -> None:
        endpoint = Endpoint("source", "old", 5432, "admin", "require", "1", None, None, None, 10)
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "", "password=secret")
            with self.assertRaisesRegex(Exception, "secret-bearing"):
                Psql(endpoint, ROOT / "sql", logging.getLogger("test")).run(
                    "db", "CREATE SUBSCRIPTION", read_only=False, contains_secret=True
                )

    def test_identity_rejects_wrong_database_and_recovery(self) -> None:
        endpoint = Endpoint("source", "old", 5432, "admin", "require", "1", None, None, None, 10)
        psql = Psql(endpoint, ROOT / "sql", logging.getLogger("test"))
        identity = {
            "database": "other", "system_identifier": "1", "server_version_num": 170000,
            "server_version": "17", "in_recovery": False,
        }
        with patch.object(psql, "identity", return_value=identity):
            with self.assertRaisesRegex(PostgreSQLError, "unexpected database"):
                psql.validate_identity("db")
        identity["database"] = "db"
        identity["in_recovery"] = True
        with patch.object(psql, "identity", return_value=identity):
            with self.assertRaisesRegex(PostgreSQLError, "in recovery"):
                psql.validate_identity("db")


class StateAndBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config_path = Path(self.directory.name) / "config.yaml"
        config_path.write_text(config_text(), encoding="utf-8")
        self.config = Config.load(config_path)
        self.state = StateStore(Path(self.directory.name) / "state.yaml", self.config)
        self.state.load()
        self.controller = Controller(self.config, self.state, ROOT / "sql", logging.getLogger("test"))

    def test_atomic_state_has_private_permissions(self) -> None:
        self.state.save()
        mode = stat.S_IMODE(self.state.path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_config_digest_change_blocks_reuse(self) -> None:
        self.state.save()
        other_path = Path(self.directory.name) / "other.yaml"
        other_path.write_text(config_text().replace("old.example", "other.example"), encoding="utf-8")
        with self.assertRaisesRegex(StateError, "configuration changed"):
            StateStore(self.state.path, Config.load(other_path)).load()

    def test_partial_batch_stops_and_resume_continues(self) -> None:
        calls: list[str] = []

        def first(database: Database) -> None:
            calls.append(database.name)
            if database.name == "db02":
                raise RuntimeError("injected failure")

        with self.assertRaises(PartialFailure):
            self.controller._batch("example", False, False, first)
        self.assertEqual(self.state.operation_rows("example"), [("db01", "COMPLETE"), ("db02", "FAILED")])
        with self.assertRaises(StateError):
            self.controller._batch("example", False, False, lambda db: None)
        self.controller._batch("example", True, False, lambda db: calls.append(f"resume:{db.name}"))
        self.assertEqual(self.state.operation_rows("example"), [("db01", "COMPLETE"), ("db02", "COMPLETE")])
        self.assertIn("resume:db01", calls)

    def test_dry_run_does_not_write_operation_state(self) -> None:
        self.controller._batch("dry", False, True, lambda db: self.fail("must not run"))
        self.assertNotIn("dry", self.state.database("db01")["operations"])

    def test_terminal_phases_do_not_satisfy_forward_or_cutover_gates(self) -> None:
        self.state.set_phase("db01", "ROLLED_BACK")
        self.assertFalse(self.state.phase_at_least("db01", "REVERSE_ACTIVE"))
        self.assertTrue(self.state.phase_at_least("db01", "ROLLBACK_CAUGHT_UP"))
        self.state.set_phase("db01", "FINALIZED")
        self.assertFalse(self.state.phase_at_least("db01", "CUTOVER_COMPLETE"))

    def test_invalid_persisted_writer_is_rejected(self) -> None:
        self.state.data["writer"] = "both"
        self.state.save()
        with self.assertRaisesRegex(StateError, "writer is invalid"):
            StateStore(self.state.path, self.config).load()

    def test_missing_published_manifest_is_a_controlled_error(self) -> None:
        table_row = {
            "schema": "public", "table": "things", "columns": [],
            "replica_identity": "f", "identity_columns": [],
        }
        snapshot = {"tables": [table_row], "sequences": []}
        with patch.object(self.controller, "_schema_snapshot", return_value=snapshot):
            with self.assertRaisesRegex(Exception, "published table manifest differs"):
                self.controller._check_direction(
                    self.config.databases[0], [], self.controller.source, self.controller.target
                )

    def test_reverse_status_requires_exact_safe_options(self) -> None:
        db = self.config.databases[0]
        publication = {
            "pubinsert": True, "pubupdate": True, "pubdelete": True,
            "pubtruncate": True, "pubviaroot": False,
        }
        tables = [{
            "schemaname": "public", "tablename": "things",
            "attnames": ["id"], "rowfilter": None,
        }]
        publisher = {
            "publication": publication, "tables": tables,
            "slot": {
                "slot_type": "logical", "plugin": "pgoutput", "database": db.name,
                "active": True, "wal_status": "reserved",
            },
        }
        subscriber = {
            "subscription": {
                "subpublications": [db.reverse_publication], "subslotname": db.reverse_slot,
                "suborigin": "none", "subbinary": False, "substream": "t",
                "subtwophasestate": "d", "subdisableonerr": True, "subenabled": True,
            },
            "relations": [{"schemaname": "public", "tablename": "things"}],
            "not_ready_count": 0,
            "workers": [{"worker_type": "apply", "relid": None}],
        }
        with (
            patch.object(self.controller, "_forward_slot", return_value="forward_slot"),
            patch.object(self.controller, "_source_info", return_value={"publication": publication, "tables": tables}),
            patch.object(self.controller.target, "file", return_value=publisher),
            patch.object(self.controller, "_target_info", return_value=subscriber),
        ):
            problems, _, _ = self.controller._reverse_status(db, require_active=True)
            self.assertEqual(problems, [])
            subscriber["subscription"]["subpublications"] = [db.reverse_publication, "unexpected"]
            problems, _, _ = self.controller._reverse_status(db, require_active=True)
            self.assertIn("reverse subscription publication differs", problems)


    def test_finalize_detaches_slots_before_dropping_subscriptions(self) -> None:
        for db in self.config.databases:
            self.state.set_phase(db.name, "CUTOVER_COMPLETE")
        self.state.data["writer"] = "target"
        subscriptions: dict[tuple[str, bool], dict[str, object] | None] = {}
        for db in self.config.databases:
            subscriptions[(db.name, False)] = {
                "subenabled": False, "subslotname": db.forward_subscription,
            }
            subscriptions[(db.name, True)] = {
                "subenabled": True, "subslotname": db.reverse_slot,
            }

        def target_info(db: Database, reverse: bool = False) -> dict[str, object]:
            return {"subscription": subscriptions[(db.name, reverse)]}

        source = MagicMock()
        target = MagicMock()

        def source_run(database: str, sql: str, **_: object) -> str:
            db = self.config.database(database)
            if " DISABLE" in sql:
                subscriptions[(database, True)]["subenabled"] = False  # type: ignore[index]
            elif "slot_name = NONE" in sql:
                subscriptions[(database, True)]["subslotname"] = None  # type: ignore[index]
            elif sql.startswith("DROP SUBSCRIPTION"):
                subscriptions[(database, True)] = None
            return ""

        def target_run(database: str, sql: str, **_: object) -> str:
            if "slot_name = NONE" in sql:
                subscriptions[(database, False)]["subslotname"] = None  # type: ignore[index]
            elif sql.startswith("DROP SUBSCRIPTION"):
                subscriptions[(database, False)] = None
            return ""

        source.run.side_effect = source_run
        target.run.side_effect = target_run
        slot = {"active": False, "slot_type": "logical", "plugin": "pgoutput"}
        source.file.side_effect = lambda database, *_args, **_kwargs: {
            "slot": {**slot, "database": database}
        }
        target.file.side_effect = source.file.side_effect

        def cleanup_objects(db: Database, **_: object) -> dict[str, object]:
            return {
                "forward": subscriptions[(db.name, False)],
                "reverse": subscriptions[(db.name, True)],
                "forward_slot": db.forward_subscription,
            }

        self.controller.source = source
        self.controller.target = target
        with (
            patch.object(self.controller, "_validate_batch_identities"),
            patch.object(self.controller, "_identity_line"),
            patch.object(self.controller, "_target_info", side_effect=target_info),
            patch.object(self.controller, "finalize_plan"),
            patch.object(self.controller, "_cleanup_preflight", side_effect=cleanup_objects),
        ):
            self.controller.finalize_execute(True, False, False)

        source_sql = [call.args[1] for call in source.run.call_args_list if call.args[0] == "db01"]
        target_sql = [call.args[1] for call in target.run.call_args_list if call.args[0] == "db01"]
        self.assertLess(
            next(i for i, sql in enumerate(source_sql) if "slot_name = NONE" in sql),
            next(i for i, sql in enumerate(source_sql) if sql.startswith("DROP SUBSCRIPTION")),
        )
        self.assertLess(
            next(i for i, sql in enumerate(target_sql) if "slot_name = NONE" in sql),
            next(i for i, sql in enumerate(target_sql) if sql.startswith("DROP SUBSCRIPTION")),
        )


class SqlAssetTests(unittest.TestCase):
    def test_required_sql_assets_exist(self) -> None:
        for name in (
            "identity.sql", "precheck_source.sql", "precheck_target.sql",
            "capacity.sql", "schema_inventory.sql", "sequence_inventory.sql",
            "active_transactions.sql",
        ):
            self.assertTrue((ROOT / "sql" / name).is_file(), name)

    def test_no_catalog_manipulation(self) -> None:
        text = "\n".join(path.read_text(encoding="utf-8").lower() for path in (ROOT / "sql").glob("*.sql"))
        self.assertNotIn("delete from pg_", text)
        self.assertNotIn("update pg_", text)



class EntrypointTests(unittest.TestCase):
    def test_help_runs_as_documented(self) -> None:
        completed = subprocess.run(
            [str(ROOT / "cutover"), "--help"], text=True, capture_output=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("wait-catchup", completed.stdout)

if __name__ == "__main__":
    unittest.main()
