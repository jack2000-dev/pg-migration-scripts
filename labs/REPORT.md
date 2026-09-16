# PostgreSQL logical-replication cutover lab report

## 1. Executive summary

The single-database cutover and rollback rehearsal completed successfully on
2026-09-16. Only the authorized, disposable `poc_migration` database was
used. The target started with the same schema and no application data; its
initial 10,000 account rows arrived through PostgreSQL logical replication.

The rehearsal proved the following path:

```text
pgbench -> source -> forward logical replication -> target

freeze source writes
-> capture final source LSN
-> wait for target
-> disable forward replication
-> synchronize target sequences
-> establish reverse replication
-> move pgbench writes to target

freeze target writes
-> capture final target LSN
-> wait for source
-> synchronize source sequences
-> disable reverse replication
-> move pgbench writes back to source
```

At cutover, source and target matched exactly with 10,000 accounts, 423
transfers, and 423 events. Reverse replication was active before target writes
were accepted. The controller's insert/delete replication probe passed, and
the complete validation result matched on both sides with no missing or orphan
rows.

Rollback also passed. The source transfer sequence was synchronized to 425,
and the first post-rollback source transfer used ID 426. This proved that the
rollback did not introduce a sequence collision.

The final controller state is `ROLLED_BACK / HEALTHY`. The workload is
stopped and routed to the source. Both logical subscriptions are disabled and
both lab replication slots are inactive.

The result is a pass for the tested single-database workflow, subject to the
limitations and risks in this report.

## 2. Scope and authorization

The operator explicitly authorized the following actions:

- use only `poc_migration` on both PostgreSQL instances;
- treat `poc_migration` as disposable;
- create the lab roles, schema, publications, subscriptions, and slots;
- seed the source and generate application-like transactions;
- execute cutover, reverse-replication, and rollback tests;
- temporarily use `sslmode=disable` for the target lab connection.

The test did not query or mutate application tables in any other database.
One unrelated pre-existing source replication slot was observed during
capacity checks and was not modified.

The following were deliberately not performed:

- controller `finalize` or destructive topology cleanup;
- cloud infrastructure provisioning or deletion;
- changes to databases other than `poc_migration`;
- a PostgreSQL 17 to PostgreSQL 18 upgrade test;
- simultaneous cutover of multiple databases;
- the database-enforced `NOLOGIN` freeze drill;
- production TLS verification on the target.

## 3. Environment

| Item | Source | Target |
|---|---|---|
| Provider | DigitalOcean managed PostgreSQL | OpenStack-hosted PostgreSQL |
| PostgreSQL version observed | 18.6 | 18.6 |
| Database | `poc_migration` | `poc_migration` |
| Role in forward flow | Publisher | Subscriber |
| Role after cutover | Reverse subscriber | Reverse publisher |
| TLS during this test | Enabled | Disabled with explicit lab authorization |
| Recovery state | Primary | Primary |
| `wal_level` | `logical` | `logical` |

Both server identities were read with `pg_control_system()`, pinned in the
runtime controller configuration, and confirmed as distinct. Hostnames,
addresses, passwords, and connection strings are intentionally omitted from
this report.

The workload simulator was the repository's `appctl` wrapper around
`pgbench`. pgEdge Load Generator was not required for this test: the
existing four-table transaction supplied inserts, updates, foreign-key use,
sequence use, and stable business invariants without adding another
dependency.

## 4. Lab schema and access model

The same empty schema was installed on both sides:

- `public.accounts`;
- `public.transfers`;
- `public.event_log`;
- `cutover_control.replication_probe`.

The source alone was seeded with 10,000 accounts. Before logical replication
was enabled, the observed row counts were:

| Side | Accounts | Transfers | Events |
|---|---:|---:|---:|
| Source | 10,000 | 0 | 0 |
| Target | 0 | 0 | 0 |

This proves the target was schema-only and was not manually seeded. After the
initial copy, both sides reported `10000|0|0`.

The lab used separate owner, deployment, application, and replication roles:

- `lab_owner`;
- `lab_deployer`;
- `lab_app`;
- `lab_forward_repl`;
- `lab_reverse_repl`.

Passwords were supplied through protected local files and `.pgpass`; no
password was written to checked-in YAML, SQL, this report, or command output.

## 5. Capability and capacity checks

Both instances passed the required capability checks:

- controller access to `pg_control_system()`;
- permission to create subscriptions;
- logical WAL enabled;
- available logical replication slots and WAL senders;
- available logical replication workers;
- distinct system identifiers;
- authentication for both replication roles;
- controller connectivity to both instances.

The successful precheck reported:

| Resource | Source used/visible | Source limit | Target used/visible | Target limit |
|---|---:|---:|---:|---:|
| Active replication origins | 0 | 10 | 1 | 10 |
| Logical replication workers | 0 | 4 | 0 | 20 |
| Replication slots | 2 | 20 | 0 | 20 |
| WAL senders | 2 | 20 | 0 | 20 |

The controller displayed 13 visible non-client source processes against
`max_worker_processes=8`. This was not used as the admission calculation:
the actual capacity gate uses visible logical replication workers plus the
projected subscription workers. That gate passed. The display is potentially
confusing and should be reviewed separately.

DigitalOcean's administrator could not read
`pg_replication_origin_status`, even though it had
`pg_read_all_stats`. The controller was changed to use the readable
`pg_replication_origin` catalog for origin existence and conservative
capacity counting. This avoided granting the broad `pg_monitor` role.

All 34 controller unit tests passed after this compatibility change, and the
patched live precheck returned `READY=YES`.

## 6. Forward replication and initial copy

The following exact forward objects were created:

| Object | Name | Location |
|---|---|---|
| Publication | `poc_migration_forward_pub` | Source |
| Subscription | `poc_migration_forward_sub` | Target |
| Slot | `poc_migration_forward_slot` | Source |

The publication contained all four expected tables. The subscription created
its source slot successfully and reached `4/4` ready table states. The slot
was logical, used `pgoutput`, belonged to `poc_migration`, and was active
with reserved WAL status.

The initial controller precheck observed:

- slot active;
- lag: 0 bytes;
- all tables ready;
- apply errors: 0;
- synchronization errors: 0;
- overall status: ready.

The source-side generator then produced 222 transfers and 222 events. After
the generator stopped, source and target both reported:

| Metric | Source | Target |
|---|---:|---:|
| Accounts | 10,000 | 10,000 |
| Transfers | 222 | 222 |
| Events | 222 | 222 |

The validation query returned `MATCH`, with a total account balance of
1,000,000,000 and no missing or orphan transfer/event rows.

## 7. Cutover execution

The cooperative application freeze was used: the lab generator was stopped
and reported `STOPPED`. A final controller precheck and exact validation
both passed before the LSN was captured.

The controller recorded these checkpoints:

| Checkpoint | UTC time | Result |
|---|---|---|
| Source writes frozen | 10:34:31 | Complete |
| Final source LSN captured | 10:34:31 | `8B/6B1695E8` |
| Forward replication caught up | 10:34:32 | Complete |
| Forward subscription disabled | 10:34:48 | Complete |
| Target sequences synchronized | 10:35:02 | Complete |
| Reverse topology prepared | 10:44:19 | Complete |
| Reverse subscription active | 10:44:33 | Complete |
| Target recorded as sole writer | 10:45:10 | Complete |

Forward replication was disabled only after the target reached the captured
source LSN. A second stable validation returned `MATCH`.

Sequence synchronization changed both target sequences:

- accounts sequence: last value 10,000;
- transfers sequence: last value 224.

The transfer sequence was ahead of the maximum committed transfer ID 222
because PostgreSQL sequences can have gaps after unsuccessful or interrupted
transactions. Preserving that higher sequence boundary was correct.

## 8. Reverse replication and target write test

The following reverse objects were prepared:

| Object | Name | Location |
|---|---|---|
| Publication | `poc_migration_reverse_pub` | Target |
| Subscription | `poc_migration_reverse_sub` | Source |
| Slot | `poc_migration_reverse_slot` | Target |

The reverse subscription was created disabled with `copy_data=false` and
`origin=none`, then enabled before target writes began. The controller
confirmed both the source apply worker and the target slot were active.

The generator was pointed to the target, and the controller recorded the
target as the sole writer. Its replication-probe insert and delete were both
observed on the source.

The first probe was run while the target generator was still active. The
probe itself passed, but the controller reported a transient 3.4 KB lag and
therefore returned unhealthy because `healthy_lag_bytes` was configured as
zero. After stopping target writes, the same probe was rerun and passed with:

- slot active;
- lag: 0 bytes;
- apply errors: 0;
- synchronization errors: 0;
- overall status: healthy.

At the stable cutover validation point, both sides reported:

| Metric | Source | Target |
|---|---:|---:|
| Accounts | 10,000 | 10,000 |
| Transfers | 423 | 423 |
| Events | 423 | 423 |
| Transfer ID range | 1–425 | 1–425 |
| Transfer amount total | 20,849 | 20,849 |
| Account balance total | 1,000,000,000 | 1,000,000,000 |
| Missing events | 0 | 0 |
| Orphan events | 0 | 0 |
| Orphan transfers | 0 | 0 |

The gaps in the ID range are normal sequence behavior. Complete validation
returned `MATCH`.

## 9. Rollback execution

Target writes were stopped before rollback. The controller captured the
target boundary and waited until reverse replication reached it:

| Checkpoint | UTC time | Result |
|---|---|---|
| Target writes frozen | 10:46:09 | Complete |
| Final target LSN captured | 10:46:09 | `0/2438130` |
| Reverse replication caught up | 10:46:10 | Complete |
| Source sequences synchronized | 10:46:11 | Complete |
| Reverse subscription disabled | 10:46:22 | Complete |

During rollback sequence synchronization:

- the source accounts sequence was already equal to or ahead and was skipped;
- the source transfers sequence was advanced to 425.

After rollback, the generator was pointed back to the source and run briefly.
The observed result was:

| Measurement | Value |
|---|---:|
| First new transfer ID after rollback | 426 |
| Source transfer count after test | 610 |
| Source maximum transfer ID | 612 |
| Frozen target transfer count | 423 |
| Frozen target maximum transfer ID | 425 |

The first source ID was exactly beyond the rollback boundary, proving the
sequence repair prevented a duplicate-key collision.

The target remained unchanged after rollback because the reverse subscription
was disabled and the forward subscription was not automatically re-enabled.
This divergence is expected for the terminal rollback state.

## 10. Problems encountered and resolutions

### Target transport security unavailable

The target did not support SSL at the time of testing. The operator explicitly
authorized `sslmode=disable` for this disposable lab. Testing proceeded over
an unencrypted target connection. This is not acceptable for production or
for sensitive test data.

### Cross-cloud firewall rules incomplete

The first forward-subscription attempt showed that the target could not reach
the source. After the source trusted-source rule was corrected, the forward
subscription was created successfully.

The first reverse-preparation attempt then showed that the source could not
reach the target. The operation had a 50-second safety ceiling. On timeout,
only the exact matching `poc_migration_reverse_sub` creation backend was
canceled. Verification showed:

- no stuck backend;
- no source reverse subscription;
- no target reverse slot;
- the intended target reverse publication remained.

After the target ingress rule was corrected, `prepare-reverse --resume`
completed successfully. The controller's resumable state and exact object
names prevented cleanup from affecting unrelated workloads.

### Managed-service catalog permission

DigitalOcean blocked `SELECT` on
`pg_replication_origin_status`. The controller now reads
`pg_replication_origin`, which provides the origin existence information
actually used by the controller and a conservative count for capacity
planning. The live precheck and reverse validation then passed without a
broader role grant.

### Zero-lag threshold during active writes

The first reverse verification returned unhealthy at 3.4 KB lag even though
the insert/delete probe completed. With a zero-byte threshold, any concurrent
transaction can cause this result. The correct operational response was used:
stop writes, wait for catch-up, and rerun. The quiet verification returned
zero lag and healthy.

### Local command-runner process lifetime

Background workers launched in one isolated automation command were reaped
when that command session ended. The final workload checks therefore started,
observed, and stopped `appctl` within one controlled command session. This
does not affect normal use from an interactive terminal, but automation
should supervise the generator in a persistent process/session manager.

## 11. Acceptance results

| Requirement | Result | Evidence |
|---|---|---|
| Target starts schema-only | Pass | Target counts were `0|0|0` before initial copy |
| Source-only seed | Pass | Source started with 10,000 accounts |
| Forward logical replication | Pass | Four tables ready, active slot, zero errors |
| Initial data copy | Pass | Target reached `10000|0|0` via subscription |
| Application workload simulation | Pass | pgbench produced balanced transfers and events |
| Cooperative source freeze | Pass | Generator stopped before final source LSN |
| Data validation before cutover | Pass | Exact source/target validation matched |
| Forward disable after catch-up | Pass | Final source LSN reached before disable |
| Target sequence repair | Pass | Both sequences advanced safely |
| Reverse protection before target writes | Pass | Reverse worker and slot active first |
| Target write redirect | Pass | Target transactions replicated to source |
| Reverse probe | Pass | Insert and delete observed at source |
| Rollback catch-up | Pass | Source reached final target LSN |
| Source sequence repair | Pass | First new source ID was 426 |
| Rollback | Pass | Controller reached `ROLLED_BACK / HEALTHY` |
| PostgreSQL 17 to 18 compatibility | Not tested | Both actual servers were PostgreSQL 18.6 |
| Concurrent multiple databases | Not tested | Authorization limited the run to `poc_migration` |
| Database-enforced freeze drill | Not tested | Cooperative `appctl stop` path used |
| Target certificate verification | Not tested | Target used authorized `sslmode=disable` |
| Finalization/teardown | Not run | Rollback objects intentionally retained |

## 12. Final state

The final verified state is:

| Component | State |
|---|---|
| Controller phase | `ROLLED_BACK` |
| Controller health | `HEALTHY` |
| Recorded writer | Source |
| `appctl` route | Source |
| Workload process | Stopped |
| Forward subscription | Present, disabled |
| Forward source slot | Present, inactive |
| Reverse subscription | Present, disabled |
| Reverse target slot | Present, inactive |
| Source data | Contains post-rollback test writes |
| Target data | Frozen at the rollback boundary |

No active lab replication worker remains. The source and target are
intentionally no longer equal because the final source-only write test ran
after both directions were disabled.

## 13. Remaining risks

### Inactive slots retain WAL

Both lab slots remain present and inactive. Their publishers may retain WAL
until the slots advance or are removed. This can consume storage and
eventually threaten the instance. Monitor retained WAL and disk use; do not
leave the lab idle indefinitely.

### This was not a major-version migration test

Both instances reported PostgreSQL 18.6. The rehearsal validates orchestration,
logical replication, cutover, sequence handling, reverse protection, and
rollback, but it does not validate PostgreSQL 17 publisher behavior or
18-to-17 reverse compatibility.

### This was not a multi-database batch test

Only `poc_migration` was allowed. The controller's batch stop/resume behavior,
capacity under multiple workers, and the requirement that all databases pass
each gate still need a separate two-or-more-database rehearsal.

### Target traffic was unencrypted

`sslmode=disable` exposes credentials and data to network interception.
Before any non-disposable or production-like exercise, install a valid target
certificate and use `verify-full` with a trusted CA.

### A cooperative freeze is not an enforcement boundary

Stopping `appctl` proved the intended application path, but another client
could still write. Production cutover must combine application shutdown with
connection/role enforcement and session verification. The repository's
`freeze-source.sql` drill remains untested in this run.

### Reverse subscription credentials remain protected catalog data

PostgreSQL stores subscription connection strings in `pg_subscription`.
Restrict catalog access, protect backups, rotate lab credentials after use,
and avoid copying catalog contents into reports or logs.

## 14. Recommended next actions

1. Decide promptly whether to preserve or remove the lab topology. If it is
   preserved, monitor inactive-slot WAL retention. Use a separately reviewed
   cleanup/finalization procedure when the rollback window is intentionally
   abandoned.
2. Enable target TLS and rerun with `sslmode=verify-full`.
3. Repeat with an actual PostgreSQL 17 source and PostgreSQL 18 target.
4. Add at least a second disposable database and rehearse concurrent batch
   failure plus `--resume`.
5. Run the database-enforced freeze drill and prove unauthorized writers
   cannot advance application data after the boundary.
6. Keep the zero-lag cutover gate, but run the final health check only after
   application writes are stopped.
7. Resolve the controller's confusing non-client worker display before using
   that displayed value operationally; retain the existing logical-worker
   admission check.

## 15. Relevant artifacts

- `labs/PLAN.md`: intended topology, gates, risks, and drills;
- `labs/README.md`: operator runbook;
- `labs/appctl`: source/target workload supervisor;
- `labs/validate`: exact source/target validation wrapper;
- `labs/sql/create-publication.sql`: publication creation helper;
- `labs/sql/create-subscription.sql`: subscription creation helper;
- `cutover-controller/sql/capacity.sql`: managed-service-compatible capacity
  query;
- `cutover-controller/sql/precheck_target.sql`: managed-service-compatible
  replication-origin check;
- `labs/.state/cutover-state.yaml`: local controller audit state; protected
  runtime artifact, not for source control.

## 16. Conclusion

The tested single-database procedure successfully preserved data integrity
through initial replication, cutover, reverse-protected target writes, and
rollback. The controller failed safely during both network and lag
conditions, resumed from a partial reverse-preparation attempt, and prevented
writes from being accepted on the target before reverse protection was
active.

The workflow is suitable for further lab development, but it is not yet
evidence for a production PostgreSQL 17-to-18, multi-database migration.
Target TLS, an actual PostgreSQL 17 source, concurrent database testing,
enforced write freeze, and inactive-slot lifecycle management remain required
before production approval.
