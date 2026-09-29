#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TEST_DIR=$(mktemp -d)
trap 'rm -rf -- "$TEST_DIR"' EXIT

mkdir -p "$TEST_DIR/labs/local" "$TEST_DIR/labs/.state" "$TEST_DIR/cutover-controller/.venv/bin"
cp "$ROOT/cleanup" "$TEST_DIR/labs/cleanup"
chmod +x "$TEST_DIR/labs/cleanup"

cat > "$TEST_DIR/labs/local/config.env" <<'EOF'
LAB_DATABASES=alpha,beta
CUTOVER_STATE_FILE=.state/test-state.yaml
EOF
cat > "$TEST_DIR/labs/local/secrets.env" <<'EOF'
TEST_SECRET=not-printed
EOF
cat > "$TEST_DIR/labs/local/cutover.yaml" <<'EOF'
databases: []
EOF
: > "$TEST_DIR/labs/.state/test-state.yaml"
chmod 600 "$TEST_DIR/labs/local/secrets.env" "$TEST_DIR/labs/.state/test-state.yaml"

cat > "$TEST_DIR/labs/appctl" <<'EOF'
#!/usr/bin/env bash
printf 'route: source\n'
printf 'alpha        %s\n' "${APP_STATUS:-STOPPED}"
printf 'beta         STOPPED\n'
EOF
cat > "$TEST_DIR/cutover-controller/.venv/bin/python" <<'EOF'
#!/usr/bin/env bash
shift
printf '%s\n' "$*" >> "$TEST_LOG"
printf 'mock controller: %s\n' "$*"
EOF
: > "$TEST_DIR/cutover-controller/cutover"
chmod +x "$TEST_DIR/labs/appctl" "$TEST_DIR/cutover-controller/.venv/bin/python"

export TEST_LOG="$TEST_DIR/controller.log"
cleanup_command="$TEST_DIR/labs/cleanup"

export APP_STATUS=RUNNING
if "$cleanup_command" >"$TEST_DIR/output" 2>&1; then
    printf 'cleanup unexpectedly accepted a running generator\n' >&2
    exit 1
fi
[[ ! -s "$TEST_LOG" ]]

export APP_STATUS=STOPPED
if "$cleanup_command" </dev/null >"$TEST_DIR/output" 2>&1; then
    printf 'cleanup unexpectedly accepted non-interactive execution\n' >&2
    exit 1
fi
[[ $(wc -l < "$TEST_LOG") -eq 2 ]]
[[ $(< "$TEST_DIR/output") != *not-printed* ]]

: > "$TEST_LOG"
if printf 'wrong\n' | script -qec "$cleanup_command" /dev/null >"$TEST_DIR/output" 2>&1; then
    printf 'cleanup unexpectedly accepted the wrong confirmation\n' >&2
    exit 1
fi
[[ $(wc -l < "$TEST_LOG") -eq 2 ]]

: > "$TEST_LOG"
printf 'alpha,beta\n' | script -qec "$cleanup_command" /dev/null >"$TEST_DIR/output" 2>&1
mapfile -t calls < "$TEST_LOG"
[[ ${#calls[@]} -eq 4 ]]
[[ "${calls[0]}" == *"finalize --plan"* ]]
[[ "${calls[1]}" == *"finalize --execute --confirm-cleanup --dry-run"* ]]
[[ "${calls[2]}" == *"finalize --execute --confirm-cleanup" ]]
[[ "${calls[2]}" != *"--dry-run"* ]]
[[ "${calls[3]}" == *" status" ]]

: > "$TEST_LOG"
printf 'alpha,beta\n' | script -qec "$cleanup_command --resume" /dev/null >"$TEST_DIR/output" 2>&1
mapfile -t calls < "$TEST_LOG"
[[ "${calls[1]}" == *"--dry-run --resume"* ]]
[[ "${calls[2]}" == *"--confirm-cleanup --resume"* ]]

sed -i 's#CUTOVER_STATE_FILE=.*#CUTOVER_STATE_FILE=../outside.yaml#' "$TEST_DIR/labs/local/config.env"
: > "$TEST_DIR/outside.yaml"
if "$cleanup_command" >"$TEST_DIR/output" 2>&1; then
    printf 'cleanup unexpectedly accepted a state file outside .state\n' >&2
    exit 1
fi

printf 'cleanup self-check passed\n'
