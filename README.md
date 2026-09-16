# PostgreSQL cutover tooling

This repository contains a PostgreSQL logical-replication cutover controller
and its rehearsal lab.

- [`cutover-controller/`](cutover-controller/README.md) is the Python CLI for
  a controlled cutover and rollback.
- [`labs/`](labs/README.md) is the environment simulator and operator runbook.
  [`labs/PLAN.md`](labs/PLAN.md) records its topology, failure drills, and
  safety boundaries.

Run the controller from its directory:

```bash
cd cutover-controller
uv sync --locked
uv run ./cutover --help
