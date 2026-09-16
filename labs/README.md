# PostgreSQL cutover lab

This lab rehearses a PostgreSQL 17 to 18 logical-replication cutover and
rollback across multiple databases:

```text
appctl/pgbench -> DigitalOcean PostgreSQL 17 -> OpenStack PostgreSQL 18
```

All operational steps use terminal `psql`. pgAdmin is optional and should be
used only for read-only inspection. This lab does not provision cloud
infrastructure. Read `PLAN.md` before running a cutover.

## Prerequisites

- A DigitalOcean PostgreSQL 17 source and OpenStack PostgreSQL 18 target.
- The same database names created on both clusters.
- Logical replication enabled with enough slots, WAL senders, and workers.
- Network access in both directions: target to source for forward replication,
  and source to target for rollback replication.
- TLS CA certificates available to each connection initiator.
- `bash`, `psql`, a working `pgbench`, and `uv`.
- Provider/admin accounts that can create roles, publications, subscriptions,
  and replication slots.

## Installation

From the repository root:

```bash
cd cutover-controller
uv sync --locked
cd ../labs

psql --version
pgbench --version
../cutover-controller/.venv/bin/python --version
```

`pgbench --version` must succeed. Some operating-system client packages install
only a wrapper; install the package containing the real binary if it fails.

Create protected runtime configuration:

```bash
mkdir -p local
chmod 700 local
cp config.example.env local/config.env
cp cutover.example.yaml local/cutover.yaml
chmod 600 local/config.env local/cutover.yaml
```

`local/`, `.state/`, and logs are ignored by Git.

## Configuration

Edit `local/config.env`:

- `LAB_DATABASES`: comma-separated database names.
- `LAB_APP_USER`: simulated application login.
- `SOURCE_VALIDATION_USER` and `TARGET_VALIDATION_USER`: controller/admin
  logins used by the terminal helpers.
- `SOURCE_REPLICATION_USER` and `TARGET_REPLICATION_USER`: forward and reverse
  logical-replication logins.
- `SOURCE_*` and `TARGET_*`: host, port, `sslmode`, and local CA path.
- `LAB_ACCOUNT_COUNT`, `LAB_CLIENTS`, and `LAB_TPS`: workload size.

If `local/config.env` already existed before the psql conversion, add:

```bash
SOURCE_REPLICATION_USER=lab_forward_repl
TARGET_REPLICATION_USER=lab_reverse_repl
```

Edit `local/cutover.yaml`:

- Set endpoints, users, SSL modes, and system identifiers.
- Add one `databases` entry per database.
- Give every publication, subscription, and slot a unique name.
- Keep the four sample `expected_tables` for the supplied lab schema.
- Set password environment-variable names; never put passwords in YAML.
- Make `target.replication_user` match `TARGET_REPLICATION_USER`.

`LAB_DATABASES` must exactly match every `databases[].name`. `source` and
`target` are endpoint roles, not database names. Every database must exist with
the same name on both clusters.

The YAML `&lab_tables` and `*lab_tables` values only reuse the table list.

### Passwords and TLS

Use `~/.pgpass` for application, validation, and controller connections:

```text
hostname:port:database:username:password
```

Add an entry for each endpoint, database, and user, then run:

```bash
chmod 600 ~/.pgpass
```

Before controller commands, export every password variable named in
`local/cutover.yaml`. Prompt silently so secrets do not enter shell history:

```bash
read -rsp 'Target replication password: ' CUTOVER_TARGET_REPLICATION_PASSWORD
export CUTOVER_TARGET_REPLICATION_PASSWORD
printf '\n'
```

For controller connections using `sslmode: verify-full`, install both CAs in
the system/libpq trust store or point `PGSSLROOTCERT` to a combined CA file.
For subscription connections, CA paths are resolved on the PostgreSQL
subscriber host, not this controller machine.

## Quickstart

Run everything below from `labs/`.

### 1. Load configuration and helpers

```bash
./appctl check

set -a
source local/config.env
set +a
: "${SOURCE_REPLICATION_USER:?add SOURCE_REPLICATION_USER to local/config.env}"
: "${TARGET_REPLICATION_USER:?add TARGET_REPLICATION_USER to local/config.env}"
IFS=, read -r -a databases <<< "$LAB_DATABASES"

source_psql() {
  PGHOST="$SOURCE_HOST" PGPORT="$SOURCE_PORT" \
  PGUSER="$SOURCE_VALIDATION_USER" PGSSLMODE="$SOURCE_SSLMODE" \
  PGSSLROOTCERT="$SOURCE_SSLROOTCERT" \
    psql -X -v ON_ERROR_STOP=1 "$@"
}

target_psql() {
  PGHOST="$TARGET_HOST" PGPORT="$TARGET_PORT" \
  PGUSER="$TARGET_VALIDATION_USER" PGSSLMODE="$TARGET_SSLMODE" \
  PGSSLROOTCERT="$TARGET_SSLROOTCERT" \
    psql -X -v ON_ERROR_STOP=1 "$@"
}

cutover_lab() {
  ../cutover-controller/.venv/bin/python ../cutover-controller/cutover \
    --config local/cutover.yaml \
    --state-file .state/cutover-state.yaml \
    --logs-dir logs "$@"
}
```

These functions last for the current shell. Reload them after opening a new
terminal.

Validate the controller YAML without connecting:

```bash
../cutover-controller/.venv/bin/python - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, str(Path('../cutover-controller').resolve()))
from lib.config import Config
Config.load('local/cutover.yaml')
print('cutover config OK')
PY
```

### 2. Check capabilities and create roles

Create every configured database on both clusters using the provider tools.
Use the first database for cluster-level checks:

```bash
admin_database=${databases[0]}
source_psql --dbname "$admin_database" -f sql/capabilities.sql
target_psql --dbname "$admin_database" -f sql/capabilities.sql
```

Record both system identifiers in `local/cutover.yaml`. Stop if logical
replication, reverse-subscription permission, network access, or capacity is
insufficient.

Create lab roles once per cluster:

```bash
source_psql --dbname "$admin_database" \
  -v admin_role="$SOURCE_VALIDATION_USER" \
  -v replication_role="$SOURCE_REPLICATION_USER" \
  -v create_replication_role=true -f sql/roles.sql

target_psql --dbname "$admin_database" \
  -v admin_role="$TARGET_VALIDATION_USER" \
  -v replication_role="$TARGET_REPLICATION_USER" \
  -v create_replication_role=true -f sql/roles.sql
```

If DigitalOcean supplies the source replication identity and rejects custom
`REPLICATION` roles, set `SOURCE_REPLICATION_USER` to that identity and run the
source command with `create_replication_role=false`. Set login passwords
outside saved SQL.

### 3. Initialize schema and source data

```bash
for database in "${databases[@]}"; do
  source_psql --dbname "$database" \
    -v replication_role="$SOURCE_REPLICATION_USER" -f sql/schema.sql

  target_psql --dbname "$database" \
    -v replication_role="$TARGET_REPLICATION_USER" -f sql/schema.sql

  source_psql --dbname "$database" \
    -v account_count="$LAB_ACCOUNT_COUNT" -f sql/seed.sql
done
```

### 4. Create forward replication

Repeat this block for each `databases` entry in `local/cutover.yaml`. Paste the
four object names from the same entry:

```bash
read -rp 'Database name: ' database
read -rp 'Forward publication: ' publication
read -rp 'Forward subscription: ' subscription
read -rp 'Forward slot: ' slot

source_psql --dbname "$database" -v publication="$publication" \
  -f sql/create-publication.sql

read -rsp 'Forward source conninfo: ' FORWARD_SOURCE_CONNINFO
export FORWARD_SOURCE_CONNINFO
printf '\n'
target_psql --dbname "$database" \
  -v subscription="$subscription" -v publication="$publication" \
  -v slot="$slot" -f sql/create-subscription.sql
unset FORWARD_SOURCE_CONNINFO
```

The hidden conninfo prompt expects the complete target-to-source connection:

```text
host=SOURCE_HOST port=SOURCE_PORT dbname=DATABASE user=REPLICATION_USER password=PASSWORD sslmode=SSLMODE
```

Add `sslrootcert=/path/on/target/host/ca.crt` for verified TLS. The secret is
passed through the environment, not shell history or command arguments, but
PostgreSQL stores it in the protected subscription catalog.

Verify each target database:

```bash
target_psql --dbname "$database" --command \
  'SELECT subname, subenabled, subslotname, subpublications FROM pg_subscription ORDER BY subname'
target_psql --dbname "$database" --command \
  'SELECT srsubstate, count(*) FROM pg_subscription_rel GROUP BY srsubstate ORDER BY srsubstate'
```

### 5. Start traffic and wait for initial copy

```bash
./appctl point source
./appctl start
./appctl status
cutover_lab inventory
cutover_lab precheck
```

Do not continue until every configured database and subscription table is
healthy.

## Cutover rehearsal

Run `sql/freeze-ddl.sql` through `source_psql` and `target_psql`, then stop app
writes:

```bash
source_psql --dbname "$admin_database" -f sql/freeze-ddl.sql
target_psql --dbname "$admin_database" -f sql/freeze-ddl.sql
./appctl stop
./appctl status
cutover_lab precheck
./validate
```

Every worker must be `STOPPED`, `precheck` must pass, and every database must
print `MATCH`. Then:

```bash
cutover_lab capture-lsn --confirm-source-writes-frozen
cutover_lab wait-catchup --timeout 1800
./validate

cutover_lab disable-forward --dry-run
cutover_lab disable-forward
cutover_lab sync-sequences --dry-run
cutover_lab sync-sequences
cutover_lab prepare-reverse --dry-run
cutover_lab prepare-reverse
cutover_lab enable-reverse --dry-run
cutover_lab enable-reverse
```

Redirect only after reverse replication is active:

```bash
./appctl point target
./appctl start
cutover_lab confirm-cutover --confirm-target-only-writable
cutover_lab verify-reverse --test
cutover_lab status
```

## Rollback rehearsal

```bash
./appctl stop
cutover_lab rollback-precheck --confirm-target-writes-frozen --timeout 1800
cutover_lab rollback --dry-run
cutover_lab rollback
./appctl point source
./appctl start
./appctl status
```

If the defensive freeze disabled `lab_app`, ensure target writes are stopped
before running `ALTER ROLE lab_app LOGIN` on source. Stop traffic after the
test, run `./validate`, and confirm the first new `transfer_id` does not
collide.

## Optional defensive freeze drill

With `appctl` writing to source, run:

```bash
source_psql --dbname "$admin_database" -f sql/freeze-source.sql
```

Confirm `remaining_app_sessions` is zero, `.state/logs/` shows failed
reconnects, and source WAL stops advancing before acknowledging the freeze.

## What to do with pgAdmin

pgAdmin is not required. Keep it only as an optional read-only viewer for
schemas, sessions, replication slots, and status views. Do not use it for lab
setup, cutover, rollback, or saved SQL containing credentials.

## Local tests

These checks do not connect to a database:

```bash
bash -n appctl validate tests/test_appctl.sh
./tests/test_appctl.sh
(cd ../cutover-controller && uv run python -m unittest discover -s tests -v)
```

Generator logs are in `.state/logs/`, controller logs are in `logs/`, and
controller state is `.state/cutover-state.yaml`. Keep the state file for
`--resume`; use a fresh lab and state file for irreversible `finalize` drills.
