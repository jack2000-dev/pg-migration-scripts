# Cutover lab runbook

This directory rehearses the existing `../cutover-controller` against two
databases in one PostgreSQL 17 -> 18 cluster pair. Read `PLAN.md` before using
it. Nothing here provisions or destroys cloud infrastructure.

## 1. Prerequisites

- Existing DigitalOcean Managed PostgreSQL 17 source and OpenStack PostgreSQL
  18 target.
- Bidirectional TLS connectivity between the database services and controller
  connectivity to both.
- `bash`, `psql`, `pgbench`, `uv`, and pgAdmin 4.
- A mode-0600 `.pgpass` covering the app and validation accounts. The reverse
  replication password remains in the environment required by the controller.
- DigitalOcean and target CA certificates. The controller does not have a
  YAML `sslrootcert` field, so install its CA in libpq's default certificate
  location or system trust store before using `sslmode: verify-full`.

Copy the runtime configuration and protect it:

```bash
cd labs
cp config.example.env config.env
cp cutover.example.yaml cutover.yaml
chmod 600 config.env cutover.yaml
```

Fill both files, then run the local checks:

```bash
./appctl check
../cutover-controller/.venv/bin/python - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path('../cutover-controller').resolve()))
from lib.config import Config
Config.load('cutover.yaml')
print('cutover config OK')
PY
```

## 2. Database capability gate

Run `sql/capabilities.sql` in pgAdmin Query Tool on one database in each
cluster. Record the output. The following are hard blockers:

- `system_identifier_access` is false;
- source `create_subscription_member` is false;
- either publisher has `wal_level` other than `logical`;
- projected slot, sender, logical-worker, or worker-process use has no reserve.

Also prove target -> source and source -> target connections using the exact
replication users, TLS mode, and firewall/trusted-source rules. Do not proceed
if the managed PostgreSQL 17 source cannot create the reverse subscription.

## 3. Roles, databases, and schema

In `sql/roles.sql`, run the common statements plus only the block labelled for
that cluster. `doadmin` and `migration_admin` match the example controller
configuration; change those grant targets if the actual controller users have
different names. Set passwords outside the saved SQL.

Create `lab_db1` and `lab_db2` on both clusters. For each of the four
databases, apply the schema with `psql` as the relevant admin:

```bash
psql "service=source dbname=lab_db1" -X -v ON_ERROR_STOP=1 -f sql/schema.sql
psql "service=source dbname=lab_db2" -X -v ON_ERROR_STOP=1 -f sql/schema.sql
psql "service=target dbname=lab_db1" -X -v ON_ERROR_STOP=1 -f sql/schema.sql
psql "service=target dbname=lab_db2" -X -v ON_ERROR_STOP=1 -f sql/schema.sql
```

Uncomment and execute the labelled replication-role grants from `schema.sql`
on the applicable cluster. Seed only the source:

```bash
psql "service=source dbname=lab_db1" -X -v ON_ERROR_STOP=1 \
  -v account_count=10000 -f sql/seed.sql
psql "service=source dbname=lab_db2" -X -v ON_ERROR_STOP=1 \
  -v account_count=10000 -f sql/seed.sql
```

## 4. Forward logical replication

Open `sql/forward-replication.sql` in pgAdmin Query Tool. Replace connection
placeholders in a temporary editor buffer, then execute one labelled block at
a time against the database and cluster named in its heading. Do not execute
the whole file on one connection.

Verify the target subscriptions without exposing connection strings:

```sql
SELECT subname, subenabled, subslotname, subpublications
FROM pg_subscription ORDER BY subname;

SELECT srsubstate, count(*)
FROM pg_subscription_rel GROUP BY srsubstate ORDER BY srsubstate;
```

Prepare a shell helper for the existing controller. Its global options must
come before each subcommand:

```bash
cutover_lab() {
  (cd ../cutover-controller && uv run ./cutover \
    --config ../labs/cutover.yaml \
    --state-file ../labs/.state/cutover-state.yaml \
    --logs-dir ../labs/logs "$@")
}
```

Start concurrent traffic and wait for initial copy:

```bash
./appctl point source
./appctl start
./appctl status
cutover_lab precheck
```

`precheck` must show both databases ready. Re-run it after resolving any lag,
table-state, schema, identity, slot, or capacity error.

## 5. Cutover

Freeze routine DDL on both clusters with `sql/freeze-ddl.sql`. Then perform the
cooperative application freeze:

```bash
./appctl stop
./appctl status
cutover_lab precheck
./validate
```

Both workers must be `STOPPED`, precheck must pass, and validation must print
`MATCH` for both databases. Capture each database's final WAL boundary and
wait for both subscriptions:

```bash
cutover_lab capture-lsn --confirm-source-writes-frozen
cutover_lab wait-catchup --timeout 1800
./validate
```

Do not continue on a mismatch. With both sides still quiet:

```bash
cutover_lab disable-forward --dry-run
cutover_lab disable-forward
cutover_lab sync-sequences --dry-run
cutover_lab sync-sequences
cutover_lab prepare-reverse --dry-run
cutover_lab prepare-reverse
cutover_lab enable-reverse --dry-run
cutover_lab enable-reverse
```

Only after reverse workers and slots are active may traffic move:

```bash
./appctl point target
./appctl start
cutover_lab confirm-cutover --confirm-target-only-writable
cutover_lab verify-reverse --test
cutover_lab status
```

Let the generator run, then stop it and wait until reverse replication is
caught up (`cutover_lab status` reports both databases healthy) before using
`./validate` as an exact comparison.

## 6. Defensive database freeze drill

This drill proves safety when the application does not cooperate:

1. Point to source and start the generator.
2. Run `sql/freeze-source.sql` on source as `doadmin`.
3. Confirm `remaining_app_sessions` is zero and the app logs show repeated
   authentication failures under `.state/logs/`.
4. Sample `pg_current_wal_lsn()` twice while no other workload runs; it must
   not advance because of the lab application.
5. Run the controller capture only after its transaction check is clear.

The supervisor intentionally retries `pgbench`; it does not silently turn an
enforced database freeze into an apparent application stop.

## 7. Rollback

Freeze target traffic and let the controller establish the reverse boundary:

```bash
./appctl stop
cutover_lab rollback-precheck --confirm-target-writes-frozen --timeout 1800
cutover_lab rollback --dry-run
cutover_lab rollback
```

If the defensive drill disabled source login, first ensure target `lab_app`
cannot write, then run `ALTER ROLE lab_app LOGIN` on source. Redirect and
verify source operation:

```bash
./appctl point source
./appctl start
./appctl status
```

Stop once more and compare both sides after any required catch-up. Confirm the
first post-rollback `transfer_id` is unique and beyond the prior source value.

## 8. Local checks

These checks require no database:

```bash
bash -n appctl validate tests/test_appctl.sh
./tests/test_appctl.sh
(cd ../cutover-controller && uv run python -m unittest discover -s tests -v)
```

Use a fresh initialized lab to rehearse irreversible `finalize`; do not run it
on the state used for the rollback exercise.
