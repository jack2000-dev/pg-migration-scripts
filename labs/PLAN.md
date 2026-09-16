# PostgreSQL logical-replication cutover lab

## Goal

Rehearse a controlled cutover and rollback across two databases using one
DigitalOcean Managed PostgreSQL 17 source and one OpenStack PostgreSQL 18
target. The lab drives both databases concurrently, but deliberately keeps
the existing `cutover-controller` unchanged.

Success means:

- both initial table copies reach `ready` and forward lag reaches the final
  per-database LSN;
- source and target counts and business invariants match at the freeze;
- sequences are advanced before target writes start;
- only one cluster accepts application writes at a time;
- PostgreSQL 18 -> 17 reverse replication is active before target writes;
- new target writes reach the source and a complete rollback is proven;
- a failure in the second database blocks the batch and `--resume` recovers it.

Infrastructure provisioning is out of scope. The two clusters must already
exist. Parameterized `psql` scripts create the reviewed initial-replication
objects; Bash, `psql`, and `pgbench` provide the lab harness.

## Topology and lab data

```text
                                before cutover
  appctl/pgbench -> DO PG17 database A -> OpenStack PG18 database A
                 -> DO PG17 database B -> OpenStack PG18 database B

                                after cutover
  DO PG17 database A <- OpenStack PG18 database A <- appctl/pgbench
  DO PG17 database B <- OpenStack PG18 database B <- appctl/pgbench
```

Each database contains `accounts`, `transfers`, `event_log`, and the
controller's `cutover_control.replication_probe`. A transaction moves an
amount between two accounts and records one transfer and one event. This
provides stable validation invariants while exercising inserts, updates,
sequences, foreign keys, and replica identities.

Roles are separated:

- `lab_owner` owns the schema and cannot log in;
- `lab_deployer` is the normal DDL login and can assume `lab_owner`;
- `lab_app` can run only the simulated workload;
- `lab_forward_repl` reads the PostgreSQL 17 publication;
- `lab_reverse_repl` reads the PostgreSQL 18 reverse publication;
- provider/admin accounts run the controller and subscription DDL.

Passwords are set outside checked-in SQL and stored in `.pgpass` or exported
environment variables. TLS and network rules must permit target -> source for
forward replication and source -> target for reverse replication.

## Go/no-go checks

Run `sql/capabilities.sql` on both clusters before loading data. Stop if:

- the controller account cannot execute `pg_control_system()`;
- the DigitalOcean account cannot create a subscription on PostgreSQL 17;
- `wal_level`, slot, WAL-sender, logical-worker, worker-process, or origin
  capacity is insufficient for two subscriptions and copy workers;
- either server cannot initiate its required cross-cloud connection;
- the target cannot verify the DigitalOcean TLS certificate or the source
  cannot authenticate to the target.

PostgreSQL 17 requires `pg_create_subscription` and database `CREATE` to
create a subscription. DigitalOcean does not expose unrestricted superuser
access, so reverse-subscription capability is the largest topology risk. If
that gate fails, the requested reverse-replication exercise requires a
self-managed PostgreSQL 17 source; the lab must not claim rollback protection.

## Rehearsal procedure

1. Create the five lab roles on each relevant cluster and set passwords
   outside the repository.
2. Create every configured database on both clusters, apply `sql/schema.sql`
   on both sides, then apply `sql/seed.sql` on each source database.
3. Use `sql/create-publication.sql` and `sql/create-subscription.sql` with
   `psql` to create one forward pair per database with unique slots.
4. Copy `config.example.env` to `local/config.env` and
   `cutover.example.yaml` to `local/cutover.yaml`. Fill endpoints,
   system identifiers, and password environment names without embedding
   passwords.
5. Run `appctl point source`, `appctl start`, and `appctl status`. Wait for
   initial copy while both database workers produce traffic.
6. Run controller `precheck`. Initial copy is complete only when all
   subscription relations are ready and both databases report healthy.
7. Freeze ordinary DDL by setting `lab_deployer NOLOGIN` on both clusters.
   Stop the generator, verify no app processes remain, and run `precheck`
   again.
8. Run `sql/validate.sql` on every source and target database. Compare the
   complete one-line results for each database.
9. Run `capture-lsn --confirm-source-writes-frozen`, then `wait-catchup`.
   Repeat validation against the now-stable source and caught-up target.
10. Run `disable-forward`, `sync-sequences`, `prepare-reverse`, and
    `enable-reverse` in that order while both sides remain quiescent.
11. Run `appctl point target`, verify the target system identifier, start the
    generator, and run `confirm-cutover --confirm-target-only-writable`.
12. Run `verify-reverse --test`; then validate that new target transfers and
    events arrive on source and all invariants remain true.
13. For rollback, stop target traffic, run
    `rollback-precheck --confirm-target-writes-frozen`, run `rollback`, point
    the generator to source, restore the source app login if it was disabled,
    and start it. Verify new IDs do not collide.

Reverse replication is enabled before target writes resume. Enabling it after
the redirect would leave target commits outside the rollback stream.

## Failure drills

1. **Application ignores the stop:** leave `appctl` running, execute
   `sql/freeze-source.sql`, and verify the app reconnects fail, its sessions
   are gone, and the source LSN stops advancing before confirming the freeze.
2. **Second database unhealthy:** disable or break only the second configured
   database, verify the controller fails closed, repair it, and rerun the
   reported command with `--resume`.
3. **Schema drift:** add an incompatible test column on one target table and
   verify precheck or reverse preparation blocks; remove it through a reviewed
   repair rather than forcing the controller.
4. **Lag and network loss:** interrupt the replication path, prove catch-up
   times out without disabling forward replication, restore it, and resume.
5. **Sequence boundary:** record the largest source transfer ID, sync
   sequences, then prove the first target and post-rollback source inserts
   are unique and greater than the appropriate prior boundary.
6. **Reverse path loss:** prevent source -> target connectivity and prove
   target writes are not released because `enable-reverse` cannot complete.

## Risks and controls

| Risk | Control |
|---|---|
| False freeze attestation | Never pass the confirmation flag until the generator is stopped or `NOLOGIN` plus session termination is verified. The controller's transaction check is a point-in-time gate, not a write lock. |
| Split-brain writes | `appctl point` refuses to run while a worker is alive. Keep the inactive cluster's app login disabled during the defensive drill. |
| No atomic operation across databases | Keep all traffic frozen until both databases complete every gate; use controller checkpoints and `--resume` after partial failure. |
| DDL is not logically replicated | Apply identical schema first, disable the normal deploy login during the rollback window, and treat schema-fingerprint drift as blocking. |
| Counts can miss offsetting corruption | Counts and invariants match this lab's chosen validation level; add deterministic row digests before using the procedure for higher-assurance production data. |
| Sequences are not replicated | Keep both sides quiet during sequence sync and test the first insert on each new writer. |
| Slot loss or retained-WAL exhaustion | Monitor slot state, `wal_status`, `safe_wal_size`, disk use, and inactive reverse slots; stop on a lost or unreserved slot. |
| PostgreSQL 18 -> 17 incompatibility | Use text-mode reverse replication and avoid PostgreSQL 18-only table features, particularly generated-column behavior. |
| Wrong endpoint or database | Use the checked-in `psql` helpers with `ON_ERROR_STOP`, then verify database, publication, subscription, slot, and system identifier. |
| Credential or endpoint exposure | Use TLS verification, provider trusted-source/firewall allowlists, mode-0600 `.pgpass`/state files, and environment variables. Never put secrets in YAML or SQL. |

## Defaults and boundaries

- Two databases in one cluster pair; multi-pair orchestration is deferred.
- 10,000 accounts, four clients, and about 20 transactions/second per
  database by default; all are adjustable in `local/config.env`.
- The cooperative path uses `appctl stop`; database-enforced `NOLOGIN` is a
  required failure drill and fallback.
- Finalization is rehearsed only in a fresh successful-cutover run, after the
  rollback exercise has been completed and the rollback window is explicitly
  abandoned.
