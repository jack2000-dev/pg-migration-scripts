#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TEST_DIR=$(mktemp -d)

cleanup() {
    LAB_CONFIG="$TEST_DIR/config.env" LAB_STATE_DIR="$TEST_DIR/state" \
        PGBENCH_BIN="$TEST_DIR/fake-pgbench" PSQL_BIN="$TEST_DIR/fake-psql" \
        "$ROOT/appctl" stop >/dev/null 2>&1 || true
    rm -rf -- "$TEST_DIR"
}
trap cleanup EXIT

cat > "$TEST_DIR/config.env" <<'EOF'
LAB_DATABASES=alpha,beta
LAB_APP_USER=lab_app
SOURCE_VALIDATION_USER=source_validator
TARGET_VALIDATION_USER=target_validator
LAB_ACCOUNT_COUNT=10
LAB_CLIENTS=1
LAB_THREADS=1
LAB_TPS=1
LAB_BATCH_SECONDS=60
LAB_RETRY_SECONDS=1
SOURCE_HOST=source.invalid
SOURCE_PORT=5432
SOURCE_SSLMODE=disable
SOURCE_SSLROOTCERT=
TARGET_HOST=target.invalid
TARGET_PORT=5432
TARGET_SSLMODE=disable
TARGET_SSLROOTCERT=
EOF

cat > "$TEST_DIR/fake-psql" <<'EOF'
#!/usr/bin/env bash
if [[ ${1:-} == --version ]]; then
    printf 'psql (fake) 1\n'
else
    printf '{"database": "%s", "valid": true}\n' "${PGDATABASE:-unknown}"
fi
exit 0
EOF

cat > "$TEST_DIR/fake-pgbench" <<'EOF'
#!/usr/bin/env bash
if [[ ${1:-} == --version ]]; then
    printf 'pgbench (fake) 1\n'
    exit 0
fi
child=''
trap '[[ -z "$child" ]] || kill "$child" 2>/dev/null || true; exit 0' TERM INT
sleep 60 &
child=$!
wait "$child"
EOF
chmod +x "$TEST_DIR/fake-psql" "$TEST_DIR/fake-pgbench"

run_appctl() {
    LAB_CONFIG="$TEST_DIR/config.env" LAB_STATE_DIR="$TEST_DIR/state" \
        PGBENCH_BIN="$TEST_DIR/fake-pgbench" PSQL_BIN="$TEST_DIR/fake-psql" \
        "$ROOT/appctl" "$@"
}

check_output=$(run_appctl check 2>&1)
[[ "$check_output" == *"configuration OK: databases=alpha beta"* ]]
[[ "$check_output" != *"warning:"* ]]
run_appctl point source >/dev/null
run_appctl start >/dev/null
status=$(run_appctl status)
[[ "$status" == *"alpha        RUNNING"* ]]
if run_appctl point target >/dev/null 2>&1; then
    printf 'point target unexpectedly succeeded while workers were running\n' >&2
    exit 1
fi
run_appctl stop >/dev/null
run_appctl point target >/dev/null
[[ $(< "$TEST_DIR/state/route") == target ]]
LAB_CONFIG="$TEST_DIR/config.env" PSQL_BIN="$TEST_DIR/fake-psql" \
    "$ROOT/validate" >/dev/null
printf 'appctl self-check passed\n'
