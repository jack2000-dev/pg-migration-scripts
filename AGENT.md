# Project agent guide

## Scope

This repository contains:

- `cutover-controller/`: Python 3.10+ CLI for PostgreSQL logical-replication
  cutover, rollback, validation, and state management.
- `labs/`: disposable rehearsal lab, operator runbook, SQL helpers, workload
  wrapper, and validation scripts.

Read `README.md`, `cutover-controller/README.md`, `labs/README.md`, and
`labs/PLAN.md` before changing migration behavior.

## Safety

- Treat database operations as production-sensitive. Use only an explicitly
  authorized disposable database for rehearsals.
- Never commit passwords, connection strings containing passwords, local
  runtime configuration, state files, or logs.
- Keep credentials in protected `.pgpass` entries or environment variables.
- Do not run `finalize`, drop objects, or alter infrastructure without explicit
  authorization.
- Do not change source code unless the task explicitly asks for an
  implementation change. Preserve unrelated working-tree changes.

## Development and checks

```bash
cd cutover-controller
uv sync --locked
python3 -m unittest discover -s tests -v

cd ..
bash labs/tests/test_appctl.sh
git diff --check
```

The controller talks to PostgreSQL through the installed `psql` client. Lab
runtime files belong under `labs/local/` and are ignored by Git.

## Commits

Use Conventional Commits, for example:

```text
feat(controller): tighten cutover topology checks
fix(lab): validate configurable database names
docs: record rehearsal results
```

Keep commits focused, do not rewrite existing user commits, and verify the
staged diff before committing.
