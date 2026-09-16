# PostgreSQL logical-replication cutover lab report — retest

## 1. Result

The clean second rehearsal completed successfully on 2026-09-16 using only
the authorized, disposable `poc_migration` database.

The v2 result is **PASS**:

- controller, administrator, application, and replication-role connections
  succeeded;
- forward server-to-server subscription creation completed in 1 second;
- reverse server-to-server preparation completed in 5 seconds;
- neither direction timed out or required a firewall change or retry;
- the target began schema-only with zero application rows;
- initial copy, source traffic, cutover, target traffic, reverse replication,
  exact validation, rollback, and post-rollback sequence use all passed;
- the final controller state is `ROLLED_BACK / HEALTHY`;
- the workload is stopped and routed to the source;
- both subscriptions are disabled and both lab slots are inactive.

The retest confirms that the cross-cloud connection rules fixed during v1
remain effective. It also reproduced one non-connection finding: a
zero-byte lag threshold can report a few kilobytes of transient WAL while
writes or a verification probe are still settling. Every quiet gate reached
zero bytes and passed.

## 2. Scope and safety

The v2 rehearsal retained the same boundaries as v1:

- only `poc_migration` was used on both instances;
- no other database's application tables or replication objects were
  modified;
- the existing unrelated source replication slot was not touched;
- no database was dropped;
- no cloud infrastructure was created or deleted;
- no controller `finalize` operation was run;
- credentials, connection strings, hostnames, and IP addresses are omitted
  from this report.

The v1 audit artifacts were preserved:

- `labs/REPORT.md`;
- `labs/.state/cutover-state.yaml`.

The retest used a separate controller state file:
`labs/.state/cutover-state-v2.yaml`.

## 3. Clean reset

Before v2, the exact v1 lab topology was verified:

- `poc_migration_forward_sub` was disabled;
- `poc_migration_reverse_sub` was disabled;
- `poc_migration_forward_slot` was inactive;
- `poc_migration_reverse_slot` was inactive;
- both slots were logical `pgoutput` slots belonging to
  `poc_migration`.

Only those named subscriptions, slots, and publications were removed. The
four existing lab tables were then truncated with identity restart on both
sides:

- `public.accounts`;
- `public.transfers`;
- `public.event_log`;
- `cutover_control.replication_probe`.

The source alone was reseeded. The resulting clean baseline was:

| Side | Accounts | Transfers | Events |
|---|---:|---:|---:|
| Source | 10,000 | 0 | 0 |
| Target | 0 | 0 | 0 |

This reconfirmed that the target was schema-only and did not receive a manual
seed.

## 4. Environment

| Item | Source | Target |
|---|---|---|
| Provider | DigitalOcean managed PostgreSQL | OpenStack-hosted PostgreSQL |
| PostgreSQL version | 18.6 | 18.6 |
| Database | `poc_migration` | `poc_migration` |
| Forward role | Publisher | Subscriber |
| Cutover role | Reverse subscriber | Reverse publisher |
| TLS | Enabled | Disabled with explicit lab authorization |
| Recovery state | Primary | Primary |
| `wal_level` | `logical` | `logical` |

The controller confirmed the configured source and target system identifiers
against the servers. They were distinct and unchanged from v1. The schema
fingerprints calculated during reverse preparation matched exactly.

This remains a same-major-version PostgreSQL 18.6 rehearsal. It is not
evidence for PostgreSQL 17-to-18 compatibility.

## 5. Connection-test results

The new root README checklist was exercised before cutover.

### Controller-to-database connections

Both administrator connections succeeded with the expected:

- database name: `poc_migration`;
- administrator identity;
- PostgreSQL 18.6 server version;
- primary state, not recovery.

### Replication-role authentication

SQL authentication succeeded for:

- `lab_forward_repl` on the source;
- `lab_reverse_repl` on the target.

Passwords were supplied from protected local runtime files and were not
printed.

### Target-to-source server path

The target created `poc_migration_forward_sub`, and the source created
`poc_migration_forward_slot`. Subscription creation completed in 1 second.
All four relations reached ready state:

```text
4/4 ready
```

Initial copy then produced:

| Side | Accounts | Transfers | Events |
|---|---:|---:|---:|
| Source | 10,000 | 0 | 0 |
| Target | 10,000 | 0 | 0 |

This proves the target database server could authenticate to and stream from
the source. A successful operator-machine connection alone would not prove
this path.

### Source-to-target server path

After forward catch-up and sequence synchronization, the controller created
the target reverse publication, source reverse subscription, and target
reverse slot. Reverse preparation completed in 5 seconds with no timeout,
cleanup, firewall adjustment, or `--resume`.

The reverse subscription was activated successfully, and the controller
verified the source apply worker and active target slot before target writes
were released.

This proves the source database server could authenticate to and stream from
the target.

### Connection comparison with v1

| Test | v1 | v2 |
|---|---|---|
| Controller -> source | Pass | Pass |
| Controller -> target | Pass after target access was fixed | Pass first attempt |
| Target -> source | Initially timed out | Pass first attempt, 1 second |
| Source -> target | Initially timed out | Pass first attempt, 5 seconds |
| Replication-role authentication | Pass after credentials were completed | Pass first attempt |
| Stuck subscription backend | One exact backend canceled during v1 | None |
| Partial topology recovery | `--resume` required in v1 | Not required |

## 6. Forward replication and source workload

The v2 forward topology used:

| Object | Name | Location |
|---|---|---|
| Publication | `poc_migration_forward_pub` | Source |
| Subscription | `poc_migration_forward_sub` | Target |
| Slot | `poc_migration_forward_slot` | Source |

The initial quiet precheck returned:

- slot active;
- lag: 0 bytes;
- table states: ready;
- apply errors: 0;
- synchronization errors: 0;
- `READY=YES`.

The source workload then produced 275 transfers and 275 events. A precheck
sampled during continuous writes saw 3.5 KB of in-flight lag and correctly
failed the configured `healthy_lag_bytes=0` threshold. The guarded command
stopped the generator. This was expected threshold behavior, not a connection
or replication error.

The required quiet precheck then passed with zero lag and zero errors.
Exact validation produced:

| Metric | Source | Target |
|---|---:|---:|
| Accounts | 10,000 | 10,000 |
| Transfers | 275 | 275 |
| Events | 275 | 275 |
| Transfer amount total | 13,400 | 13,400 |
| Account balance total | 1,000,000,000 | 1,000,000,000 |
| Missing events | 0 | 0 |
| Orphan events | 0 | 0 |
| Orphan transfers | 0 | 0 |

Validation returned `MATCH`.

## 7. Cutover checkpoints

The v2 controller recorded:

| Checkpoint | UTC time | Result |
|---|---|---|
| State created | 11:20:24 | Complete |
| Source writes frozen | 11:21:37 | Complete |
| Final source LSN captured | 11:21:37 | `8B/75000088` |
| Forward replication caught up | 11:21:38 | Complete |
| Forward subscription disabled | 11:21:41 | Complete |
| Target sequences synchronized | 11:21:45 | Complete |
| Reverse topology prepared | 11:22:11 | Complete |
| Reverse subscription active | 11:22:15 | Complete |
| Target recorded as sole writer | 11:22:35 | Complete |

The target reached the exact captured source LSN before forward replication
was disabled. A stable source/target validation passed immediately before the
disable.

Target sequence synchronization changed:

- accounts sequence to 10,000;
- transfers sequence to 275.

No target application write was accepted before reverse protection was
active.

## 8. Target workload and reverse validation

The target workload ran only after:

- forward replication was caught up and disabled;
- target sequences were synchronized;
- reverse publication and subscription existed;
- reverse slot and apply worker were active;
- the controller recorded the target as the only writer.

After target traffic stopped, the first immediate reverse probe verified its
insert and delete but reported 3.4 KB of trailing WAL. Because the accepted
threshold is zero bytes, the command returned unhealthy.

After the quiet status reached zero lag, the probe was rerun and passed:

- replication probe insert reached the source;
- replication probe delete reached the source;
- reverse slot active;
- lag: 0 bytes;
- apply errors: 0;
- synchronization errors: 0;
- status: healthy.

Stable cutover validation produced:

| Metric | Source | Target |
|---|---:|---:|
| Accounts | 10,000 | 10,000 |
| Transfers | 602 | 602 |
| Events | 602 | 602 |
| Transfer ID range | 1–602 | 1–602 |
| Transfer amount total | 30,056 | 30,056 |
| Account balance total | 1,000,000,000 | 1,000,000,000 |
| Missing events | 0 | 0 |
| Orphan events | 0 | 0 |
| Orphan transfers | 0 | 0 |

Validation returned `MATCH`, and the controller reported
`CUTOVER_COMPLETE / HEALTHY`.

## 9. Rollback

The target generator was stopped before rollback. The controller recorded:

| Checkpoint | UTC time | Result |
|---|---|---|
| Target writes frozen | 11:23:47 | Complete |
| Final target LSN captured | 11:23:47 | `0/25CF110` |
| Reverse replication caught up | 11:23:48 | Complete |
| Source sequences synchronized | 11:23:50 | Complete |
| Reverse subscription disabled | 11:23:57 | Complete |

The source accounts sequence was already equal to or ahead and was skipped.
The source transfers sequence was changed to 602.

The application simulator was moved back to the source and run briefly. The
post-rollback result was:

| Measurement | Value |
|---|---:|
| First new source transfer ID | 603 |
| Source transfer count | 770 |
| Source maximum transfer ID | 770 |
| Frozen target transfer count | 602 |
| Frozen target maximum transfer ID | 602 |

The first source ID was exactly one above the rollback boundary, proving that
the sequence repair prevented a duplicate-key collision. The target remained
unchanged because both replication directions were disabled after rollback,
which is the expected terminal state.

## 10. Acceptance matrix

| Requirement | Result | Evidence |
|---|---|---|
| Clean source-only seed | Pass | Source `10000|0|0`, target `0|0|0` |
| Controller SQL connections | Pass | Both expected primaries and identities returned |
| Replication-role authentication | Pass | Both exact roles connected |
| Target -> source network path | Pass | Forward subscription created in 1 second |
| Source -> target network path | Pass | Reverse preparation completed in 5 seconds |
| Initial logical copy | Pass | Four relations ready; target reached 10,000 accounts |
| Source workload replication | Pass | 275 transfers/events matched |
| Quiet zero-lag precheck | Pass | Zero bytes and no errors |
| Final-LSN catch-up | Pass | Target reached `8B/75000088` |
| Forward disable | Pass | Disabled only after catch-up |
| Target sequence repair | Pass | Accounts 10,000; transfers 275 |
| Reverse active before target writes | Pass | Worker and slot verified |
| Target workload replication | Pass | 602 transfers/events matched |
| Reverse insert/delete probe | Pass | Both operations observed on source |
| Exact cutover validation | Pass | Invariants matched; no orphan rows |
| Rollback boundary catch-up | Pass | Source reached `0/25CF110` |
| Source sequence repair | Pass | First new ID was 603 |
| Rollback | Pass | `ROLLED_BACK / HEALTHY` |
| Connection issue from v1 reproduced | No | Both directions passed first attempt |
| PostgreSQL 17 -> 18 | Not tested | Both servers are PostgreSQL 18.6 |
| Multiple databases concurrently | Not tested | Only `poc_migration` was authorized |
| Target TLS verification | Not tested | Target still uses `sslmode=disable` |
| Database-enforced freeze | Not tested | Cooperative workload stop used |

## 11. Final state

| Component | Final v2 state |
|---|---|
| Controller phase | `ROLLED_BACK` |
| Controller health | `HEALTHY` |
| Recorded writer | Source |
| `appctl` route | Source |
| Workload | Stopped |
| Forward subscription | Present, disabled |
| Forward source slot | Present, inactive |
| Reverse subscription | Present, disabled |
| Reverse target slot | Present, inactive |
| Source transfers | 770, maximum ID 770 |
| Target transfers | 602, maximum ID 602 |

The source/target data difference is intentional: it was created by the final
post-rollback source-only write test after both replication directions were
disabled.

## 12. Findings and risks

### Network readiness improved

The v1 cloud allowlist problems did not recur. Both real server-to-server
subscription operations completed promptly. This is stronger evidence than
`pg_isready` or an operator-machine `psql` connection.

### Zero-byte lag is a freeze gate

The 3.5 KB live-precheck result and 3.4 KB immediate-probe result show that a
zero-byte threshold should be evaluated only after writes stop and WAL has
settled. The threshold correctly fails closed. It should not be weakened just
to make an active-workload precheck green.

### Inactive slots retain WAL

Both lab slots remain present and inactive. They can retain publisher WAL and
must be monitored or removed through a reviewed cleanup before they threaten
storage.

### Target TLS remains the largest connection-security gap

The target still uses the explicitly authorized lab-only
`sslmode=disable`. Network reachability is now proven, but transport
confidentiality and certificate identity are not. Production readiness
requires `verify-full` with the correct target hostname and trusted CA.

### Version and batch scope remain incomplete

This was another PostgreSQL 18.6-to-18.6, single-database run. It does not
prove PostgreSQL 17-to-18 behavior, reverse compatibility to PostgreSQL 17,
or all-or-nothing gates across multiple databases.

## 13. README connection checklist

The repository root `README.md` now includes a pre-cutover connection
checklist covering:

1. controller DNS/TCP checks;
2. authenticated SQL, identity, recovery, and TLS checks;
3. logical-replication capacity settings;
4. exact replication-role authentication;
5. real disposable forward and reverse subscription tests;
6. server egress allowlists rather than only the operator's IP;
7. final controller `precheck` as a mandatory go/no-go gate.

The checklist deliberately distinguishes controller connectivity from
database-server-to-database-server connectivity.

## 14. Recommended next actions

1. Enable target TLS and repeat the connection checklist with
   `sslmode=verify-full`.
2. Remove or actively monitor the two inactive lab slots; do not leave them
   retaining WAL indefinitely.
3. Repeat with the intended PostgreSQL 17 source.
4. Add a second disposable database and test concurrent batch failure plus
   `--resume`.
5. Run the database-enforced freeze drill instead of relying only on
   cooperative process shutdown.
6. Preserve the zero-lag cutover criterion, but perform the decisive check
   only after the writer is frozen.

## 15. Conclusion

The v2 rehearsal completed the full forward-copy, cutover,
reverse-protection, target-write, validation, rollback, and source-resume
cycle without either of the v1 connection failures. Both database-server
directions were proven through real logical-replication objects, not just
client connections.

The tested single-database orchestration is repeatable under the current
network rules. Production approval still requires target TLS, the intended
PostgreSQL 17 source, multi-database testing, and an enforced write-freeze
exercise.
