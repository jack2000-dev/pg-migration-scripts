# PostgreSQL logical-replication cutover controller

This CLI coordinates a one-writer cutover across several databases in two PostgreSQL clusters. It verifies OLD → NEW subscriptions, records final WAL positions, stops forward replication, synchronizes sequences, and establishes NEW → OLD rollback protection.

The controller does not route applications, stop application writes, apply DDL, or change server parameters. Those remain operator actions. It records explicit freeze and routing attestations so dangerous commands cannot run out of order.

> **Production warning:** rehearse the entire procedure in a representative non-production environment and keep independent backups. Finalization permanently removes the replication path used for rollback.

## 1. Architecture

The executable is Python 3 with PyYAML for validated configuration and the installed psql client for PostgreSQL access. SQL uses:

~~~text
psql -X --no-psqlrc --quiet --no-align --tuples-only --set ON_ERROR_STOP=1
~~~

Read operations also set default_transaction_read_only. Identifiers and literals are quoted centrally. Passwords pass through libpq environment variables or SQL stdin and never appear in process arguments or logs.

The state file is written atomically with mode 0600. A lock prevents concurrent controller processes. Mutations run sequentially in configuration order because PostgreSQL cannot make one transaction span databases:

~~~text
db01  disable-forward  COMPLETE
db02  disable-forward  FAILED
db03  disable-forward  NOT_STARTED
~~~

After correcting the failure, rerun the same command with --resume. Completed databases stay in place and live preconditions are checked again.

Per-database states are:

~~~text
FORWARD_REPLICATING
  -> WRITES_FROZEN
  -> FINAL_LSN_CAPTURED
  -> FORWARD_CAUGHT_UP
  -> FORWARD_DISABLED
  -> SEQUENCES_SYNCED
  -> REVERSE_PREPARED
  -> REVERSE_ACTIVE
  -> CUTOVER_COMPLETE

CUTOVER_COMPLETE
  -> ROLLBACK_WRITES_FROZEN
  -> ROLLBACK_LSN_CAPTURED
  -> ROLLBACK_CAUGHT_UP
  -> ROLLBACK_SEQUENCES_SYNCED
  -> ROLLED_BACK

CUTOVER_COMPLETE -> FINALIZED
~~~

The reverse slot is created before NEW accepts writes. Creating it afterward would omit transactions committed before the slot creation point.

## 2. Assumptions

- OLD and NEW are different PostgreSQL clusters. All configured databases on a side use that side's system identifier.
- PostgreSQL 17 and 18 are supported. Use a recent psql client compatible with both.
- Forward logical replication is initialized and running.
- Configured publications and subscriptions are dedicated to this migration.
- The table manifest is an exact, reviewed declaration of replication scope.
- Application writes can be frozen externally, and only one side receives application writes.
- OLD contains the reverse baseline, so reverse subscriptions use copy_data=false.
- DDL is frozen during the rollback window. Relevant table or sequence schema drift blocks rollback.
- Large objects require a separate migration procedure.

## 3. Requirements

- [uv](https://docs.astral.sh/uv/) with Python 3.10 or newer
- psql
- Controller network access to both clusters
- Network access from the OLD database service to NEW for reverse replication

Create the locked environment:

~~~bash
uv sync --locked
~~~

`uv` installs PyYAML into the project-local `.venv`. Run the controller as
`uv run ./cutover`; the remaining examples omit the prefix for readability.

The controller role needs CONNECT; catalog and monitoring visibility, normally through pg_monitor; ownership or authority to alter subscriptions; CREATE and publication privileges; pg_create_subscription where required; and SELECT/UPDATE on user sequences.

The NEW replication role used by OLD needs LOGIN, REPLICATION, and SELECT on published tables. Managed services may provide equivalent roles. Access to pg_control_system() is required for identity pinning.

## 4. Authentication setup

Never put passwords in YAML. A plaintext password key is rejected.

For controller connections, prefer a mode-0600 ~/.pgpass:

~~~text
old-db.example.com:5432:db01:migration_admin:SOURCE_PASSWORD
new-db.example.com:5432:db01:migration_admin:TARGET_PASSWORD
~~~

~~~bash
chmod 0600 ~/.pgpass
~~~

Side-specific environment variables are also supported:

~~~yaml
source:
  password_env: CUTOVER_SOURCE_PASSWORD
target:
  password_env: CUTOVER_TARGET_PASSWORD
~~~

~~~bash
read -rsp 'SOURCE password: ' CUTOVER_SOURCE_PASSWORD; export CUTOVER_SOURCE_PASSWORD
read -rsp 'TARGET password: ' CUTOVER_TARGET_PASSWORD; export CUTOVER_TARGET_PASSWORD
~~~

The reverse worker runs inside OLD, so the controller's .pgpass cannot authenticate it to NEW. Configure a passwordless managed-service mechanism or:

~~~yaml
target:
  replication_user: logical_replication
  replication_password_env: CUTOVER_TARGET_REPLICATION_PASSWORD
~~~

PostgreSQL can store a password-bearing connection string in protected pg_subscription.subconninfo. Restrict catalog and backup access. The controller never prints this value.

## 5. Configuration

Copy and protect the example:

~~~bash
cp config.example.yaml config.yaml
chmod 0600 config.yaml
~~~

Obtain each cluster identity with a privileged read-only connection:

~~~sql
SELECT system_identifier FROM pg_control_system();
~~~

Set expected_system_identifier for each endpoint. Every mutation validates it, database name, server version, user, and recovery state.

Each database needs an exact table manifest:

~~~yaml
expected_tables:
  - schema: public
    table: customers
  - schema: audit
    table: events
~~~

Generate a starting list, then review it:

~~~bash
./cutover --config config.yaml inventory
~~~

A missing table in both live publication and subscription metadata cannot be inferred automatically.

healthy_lag_bytes defaults to zero. Raise it only for repeated health monitoring under a documented threshold. Cutover and rollback barriers always require reaching the captured LSN.

## 6. Full migration workflow

~~~text
PHASE 1

OLD                              NEW
Application -> OLD
OLD pub -----------------------> NEW sub


PHASE 2 - freeze

Application writes stopped

OLD pub -------- catch up ------> NEW sub


PHASE 3 - stop forward and prepare fallback

OLD forward publisher            NEW forward subscriber disabled
OLD reverse subscriber disabled  NEW reverse publisher + slot
Application writes still stopped


PHASE 4 - enable reverse, then cut over

OLD                              NEW
OLD reverse sub <--------------- NEW reverse pub
                                  ^
                                  |
                              Application


PHASE 5 - rollback if needed

Freeze NEW
NEW -> OLD catches up
Sync sequences
Disable reverse
Application -> OLD
~~~

Normal command sequence:

~~~bash
./cutover precheck

# Freeze SOURCE writes externally.
./cutover capture-lsn --confirm-source-writes-frozen
./cutover wait-catchup
./cutover disable-forward
./cutover sync-sequences
./cutover prepare-reverse
./cutover enable-reverse

# Point the application to NEW and release writes.
./cutover confirm-cutover --confirm-target-only-writable
./cutover verify-reverse
~~~

Examples assume the cutover-controller directory. Global --config, --state-file, and --logs-dir options precede the command.

## 7. Pre-cutover procedure

1. Validate backups, change ownership, and the application freeze runbook.
2. Confirm OLD-to-NEW schema deployment is complete.
3. Run ./cutover precheck.
4. Resolve every READY=NO database.
5. Inspect existing and projected capacity for max_replication_slots, max_logical_replication_workers, max_sync_workers_per_subscription, max_worker_processes, max_wal_senders, and PostgreSQL 18 max_active_replication_origins.
6. Run watch -n 5 './cutover status' in another terminal.

Precheck samples error counters twice over ten seconds. Historical counts are reported; only increases indicate a current failure. It requires a logical pgoutput slot in reserved or extended WAL state, an apply worker, ready subscription tables, expected publication actions, exact manifests, compatible schemas, and a replication origin.

## 8. Cutover procedure

Freeze application writes on OLD externally. The controller does not change database or application read-only settings.

capture-lsn blocks when it sees another open client transaction or prepared transaction. This reduces the chance of a transaction committing after capture, but the external freeze remains authoritative.

~~~bash
./cutover capture-lsn --confirm-source-writes-frozen
./cutover wait-catchup --interval 5
~~~

There is no default catchup timeout. An optional deadline is:

~~~bash
./cutover wait-catchup --timeout 1800
~~~

Readiness requires both the subscriber apply worker's latest_end_lsn and publisher slot's confirmed_flush_lsn to reach the stored LSN.

~~~bash
./cutover disable-forward --dry-run
./cutover disable-forward
./cutover sync-sequences --dry-run
./cutover sync-sequences
~~~

Forward objects remain present. Already-disabled subscriptions are accepted. Missing subscriptions fail unless --ignore-missing is deliberately supplied.

For every non-extension user sequence, the controller reads:

~~~sql
SELECT last_value, is_called FROM "schema"."sequence";
~~~

When the destination is behind it executes the equivalent of:

~~~sql
SELECT setval('"schema"."sequence"'::regclass, source_last_value, source_is_called);
~~~

This preserves unused versus called state, handles negative increments, and never moves the destination backward. Serial, identity-owned, and standalone sequences are included. Different cyclic states are blocked because they have no safe total ordering.

setval is not transactional. Every changed or skipped sequence is logged, and reruns re-read current state. Cached sequences can retain gaps; gaps are safer than reused values.

## 9. Reverse replication procedure

While both sides remain quiescent:

~~~bash
./cutover prepare-reverse --dry-run
./cutover prepare-reverse
./cutover enable-reverse
~~~

Preparation checks table existence, published columns, safe type direction, subscriber-only required columns, and replica identities. It mirrors effective forward tables, column lists, row filters, actions, and partition-root behavior.

Reverse subscriptions use:

~~~sql
WITH (
  copy_data = false,
  enabled = false,
  create_slot = true,
  origin = none,
  binary = false,
  streaming = on,
  disable_on_error = true
)
~~~

The disabled subscription and NEW slot are verified before checkpointing. enable-reverse waits up to 60 seconds for each OLD worker and NEW active slot. Only then point applications at NEW:

~~~bash
./cutover confirm-cutover --confirm-target-only-writable
./cutover verify-reverse
./cutover verify-reverse --json
~~~

For an explicit insert/delete probe, create the same dedicated table on both sides and add it to the manifest and forward publication:

~~~sql
CREATE SCHEMA IF NOT EXISTS cutover_control;
CREATE TABLE cutover_control.replication_probe (
    cutover_test_id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    marker text NOT NULL
);
COMMENT ON TABLE cutover_control.replication_probe
    IS 'cutover-controller replication probe v1';
~~~

Use the configured table or name it:

~~~bash
./cutover verify-reverse --test
./cutover verify-reverse --test-table cutover_control.replication_probe
~~~

The controller validates the comment and exact column contract, inserts a UUID on NEW, verifies it on OLD, deletes it on NEW, and verifies deletion on OLD. Interrupted probe identifiers are saved for cleanup on the next test.

## 10. Rollback procedure

Freeze NEW application writes, then:

~~~bash
./cutover rollback-precheck --confirm-target-writes-frozen
~~~

This blocks on open/prepared transactions and schema drift, captures final NEW LSNs once, waits without a default timeout for OLD to acknowledge them, and synchronizes sequences NEW → OLD.

If sequence synchronization fails partway, correct the problem and use:

~~~bash
./cutover rollback-precheck --confirm-target-writes-frozen --resume
~~~

~~~bash
./cutover rollback --dry-run
./cutover rollback
~~~

Success means reverse subscriptions are disabled and connection strings may be moved back to OLD. The controller does not modify application configuration.

## 11. Final cleanup procedure

Finalization is available only after an accepted cutover with TARGET recorded as writer.

~~~bash
./cutover finalize --plan
~~~

After formal rollback-window closure:

~~~bash
./cutover finalize --execute --confirm-no-rollback
~~~

Execution preflights every named publication, subscription, relation manifest, and slot before deletion, then disables and verifies all subscriptions. It detaches each subscription with `slot_name = NONE`, drops the local subscription, explicitly verifies and drops only the expected inactive logical `pgoutput` slot on the expected database, and finally drops the dedicated publications. This keeps cleanup resumable even when the remote publisher is unavailable. For partial cleanup:

~~~bash
./cutover finalize --execute --confirm-no-rollback --resume
~~~

Never delete from PostgreSQL system catalogs or manually alter replication-origin rows.

## 12. Failure and recovery scenarios

- **Batch stops on db02:** fix that database and rerun the same command with --resume.
- **Configuration changed:** restore the reviewed config. Use a separate state file only for a deliberately new migration.
- **Wrong endpoint or cluster failover:** independently verify the new system identifier before changing configuration.
- **Slot is unreserved or lost:** stop and recover logical replication through a separately reviewed PostgreSQL procedure.
- **Historical errors are nonzero:** inspect logs. Stable counters alone do not block precheck; increasing counters do.
- **Reverse preparation partially completes:** keep writes frozen. The inactive created slot retains WAL; fix the issue and use prepare-reverse --resume.
- **Reverse enable partially completes:** keep writes frozen and use enable-reverse --resume after correcting the failure.
- **Schema drift appears:** rollback is blocked. Native logical replication does not copy DDL. Stage compatible DDL on OLD before NEW through a separate reviewed procedure.
- **A new table received rows before reverse setup:** do not treat copy_data=false as a backfill. Plan a separate validated data copy.
- **Probe is interrupted:** rerun the same test. Its saved UUID is cleaned before a new probe.
- **Controller is interrupted:** inspect the timestamped log and preserved atomic state, then use the reported resume command.

## 13. Example output

~~~text
DATABASE  SLOT      LAG       TABLES_READY  APPLY_ERR  SYNC_ERR  READY
db01      active    0 bytes   yes           2 (+0)     0 (+0)    YES
db02      active    15.0 MB   yes           0 (+0)     0 (+0)    NO
db02: lag exceeds 0 bytes
~~~

~~~text
DATABASE  FINAL          LATEST         STATUS
db01      202/C87D1234   202/C87D1234   READY
db02      88/A1230000    88/A1200000    192.0 KB remaining
~~~

~~~text
DATABASE  SLOT_ACTIVE  LAG      APPLY_ERRORS  SYNC_ERRORS  STATUS
db01      true         0 bytes  0             0            HEALTHY
~~~

JSON output:

~~~bash
./cutover status --json
~~~

Exit codes:

~~~text
0    healthy or requested step completed
1    unexpected internal failure; inspect the private controller log
2    blocked or unhealthy
3    invalid configuration or input
4    PostgreSQL or connection failure
5    partially completed multi-database mutation
130  interrupted
~~~

## 14. Production safety warnings

- Keep writes frozen from final SOURCE LSN capture until reverse subscriptions are active and the application is switched.
- Do not prepare reverse replication after releasing NEW writes.
- Do not reset subscription statistics during an observation window.
- Freeze relevant DDL throughout the rollback window.
- Watch retained WAL and safe_wal_size, especially while a reverse slot is inactive.
- Treat --ignore-missing, --force, and --confirm-no-rollback as exceptional actions and record why they were used.
- Back up the mode-0600 state file and logs with the change record.
- Read the complete finalize plan. Cleanup cannot be globally atomic across databases.

## Development checks

~~~bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q cutover lib tests
./cutover --help
~~~

If `uv` is installed, the same commands may be run through `uv run`. The unit suite covers config rejection, SQL quoting, password handling, identity pinning, LSN math, lossless type direction, strict state validation, private atomic state, config pinning, dry runs, and partial-batch resume. Rehearse the complete workflow against disposable PostgreSQL 17 and 18 clusters before production.
