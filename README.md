# PostgreSQL cutover tooling

This repository contains a PostgreSQL logical-replication cutover controller
and a lab for rehearsing the same workflow before production.

Use the lab first. The controller does not provision PostgreSQL, freeze
application writes, or route application traffic for you.

## What is here

- [`cutover-controller/`](cutover-controller/README.md): Python CLI that
  validates, checkpoints, and coordinates cutover and rollback.
- [`labs/`](labs/README.md): simulator, SQL setup, workload generator, and
  complete operator runbook.
- [`labs/PLAN.md`](labs/PLAN.md): lab topology, failure drills, and safety
  boundaries.

## Prerequisites

- PostgreSQL 17 source and PostgreSQL 18 target, with the same lab databases
  created on both clusters.
- Logical replication enabled with spare slots, WAL senders, and workers.
- Network and TLS connectivity in both directions: source → target for
  forward replication and target → source for rollback replication.
- Provider/admin, validation, application, and replication roles with the
  privileges described in [`labs/README.md`](labs/README.md).
- Operator workstation with `bash`, `psql`, `pgbench`, `uv`, and Python 3.10+.
- TLS CA files and credentials in a mode-0600 `~/.pgpass` or environment
  variables. Never put passwords in YAML, SQL, commands, or shell history.

## Install once

From the repository root, verify the tools and create the locked Python
environment:

```bash
command -v uv psql pgbench
cd cutover-controller
uv sync --locked
cd ../labs
../cutover-controller/.venv/bin/python --version
psql --version
pgbench --version
```

## Configure each lab run

Follow the database setup and replication instructions in
[`labs/README.md`](labs/README.md), then create protected local configuration:

```bash
cd labs
mkdir -p local
chmod 700 local
cp config.example.env local/config.env
cp cutover.example.yaml local/cutover.yaml
chmod 600 local/config.env local/cutover.yaml
```

Edit both files with the real endpoints, users, database names, TLS paths,
system identifiers, and password environment-variable names. `local/` is
ignored by Git. Keep the controller's state in `.state/` and logs in `logs/`.

## Run the rehearsal

Run these commands from `labs/`. Define the controller shortcut once per
shell:

```bash
cutover() {
  ../cutover-controller/.venv/bin/python ../cutover-controller/cutover \
    --config local/cutover.yaml \
    --state-file .state/cutover-state.yaml \
    --logs-dir logs "$@"
}
```

Start source traffic and wait for the initial copy:

```bash
./appctl check
./appctl point source
./appctl start
./appctl status
cutover inventory
cutover precheck
```

Do not continue until every configured database and subscription relation is
healthy.

## Cut over to the target

Freeze writes externally, then keep them frozen through reverse replication:

```bash
./appctl stop
./validate
cutover precheck
cutover capture-lsn --confirm-source-writes-frozen
cutover wait-catchup --timeout 1800
cutover disable-forward
cutover sync-sequences
cutover prepare-reverse
cutover enable-reverse
./appctl point target
./appctl start
cutover confirm-cutover --confirm-target-only-writable
cutover verify-reverse --test
```

Only release target writes after `enable-reverse` succeeds. The controller
records checkpoints but cannot make the multi-database operation atomic.

## Roll back to the source

If rollback is required, stop target writes and run:

```bash
./appctl stop
cutover rollback-precheck --confirm-target-writes-frozen --timeout 1800
cutover rollback
./appctl point source
./appctl start
./appctl status
```

Validate the source and first post-rollback transfer before resuming normal
traffic. For irreversible cleanup, read the finalization procedure in
[`cutover-controller/README.md`](cutover-controller/README.md) and do not run
it until the rollback window is explicitly abandoned.

## If a command fails

Keep writes frozen. Correct the reported database or replication issue, then
rerun the failed operation with `--resume` when the controller suggests it.
Do not delete the state file during an active rehearsal; it contains the
checkpoints needed to recover safely.

## Local checks

These checks do not contact PostgreSQL:

```bash
(cd labs && bash -n appctl validate tests/test_appctl.sh && ./tests/test_appctl.sh)
(cd cutover-controller && .venv/bin/python -m unittest discover -s tests -v)
```
