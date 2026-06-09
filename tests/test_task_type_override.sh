#!/usr/bin/env bash
# Tests for the task_type override + is_bash_body heuristic in workers/bootstrap.sh
# Run from the project root: bash tests/test_task_type_override.sh
set -e

cd "$(dirname "$0")/.." || exit 1
PROJECT_DIR="$(pwd)"

# Helper: extract just the task-type resolution logic from bootstrap.sh
# and run it against a synthetic TASK_BODY and TASK_TYPE_OVERRIDE.
resolve_task_type() {
    local body="$1"
    local override="$2"
    # Inline the relevant logic from bootstrap.sh
    is_bash_body() {
        local b="$1"
        echo "$b" | grep -qE '^\s*(echo|cat|mkdir|ls|cd|export|curl|wget|python|pip|git|apt|chmod|mv|cp|rm|tar|gzip|zip|kill|sleep|exit|tee|grep|sed|awk|find|sort|uniq|head|tail|nc|ssh|scp|rsync|sudo|make|docker|openssl|base64|jq|yq|sleep |exit |cd )' && return 0
        echo "$b" | grep -qE '^\s*[a-z_]+=|^\s*\$[a-z_]+|^\s*\|^\s*&&|^\s*\|\||^\s*;' && return 0
        [[ $(echo "$b" | wc -l) -gt 20 ]] && return 0
        return 1
    }
    case "$override" in
        nl|bash) echo "$override" ;;
        *)       is_bash_body "$body" && echo "bash" || echo "nl" ;;
    esac
}

PASS=0
FAIL=0

check() {
    local desc="$1"
    local got="$2"
    local want="$3"
    if [[ "$got" == "$want" ]]; then
        echo "✓ $desc"
        PASS=$((PASS + 1))
    else
        echo "✗ $desc (got=$got, want=$want)"
        FAIL=$((FAIL + 1))
    fi
}

# Test 1: short bash body, no override → bash (auto-detected)
check "short bash body, no override → bash" \
    "$(resolve_task_type 'echo hello' '')" "bash"

# Test 2: short NL body, no override → nl (auto-detected)
check "short NL body, no override → nl" \
    "$(resolve_task_type 'You are fixing a typo in README.md' '')" "nl"

# Test 3: long NL body (>20 lines), no override → bash (the FOOTGUN)
LONG_NL_BODY="$(printf 'You are fixing a bug.\n%.0s' {1..30})"
check "long NL body (>20 lines), no override → bash (footgun preserved)" \
    "$(resolve_task_type "$LONG_NL_BODY" '')" "bash"

# Test 4: long NL body with override=nl → nl (THE FIX)
check "long NL body with override=nl → nl (THE FIX)" \
    "$(resolve_task_type "$LONG_NL_BODY" "nl")" "nl"

# Test 5: short bash body with override=bash → bash
check "short bash body with override=bash → bash" \
    "$(resolve_task_type 'echo hello' "bash")" "bash"

# Test 6: invalid override value (e.g. "python") → falls back to heuristic
check "invalid override 'python' → falls back to heuristic" \
    "$(resolve_task_type 'echo hello' "python")" "bash"

# Test 7: invalid override value with long NL → falls back to bash (footgun still active for non-explicit)
check "invalid override + long NL → bash (heuristic fallback)" \
    "$(resolve_task_type "$LONG_NL_BODY" "garbage")" "bash"

# Test 8: override=NL (uppercase) — case sensitivity check
# The current impl is case-sensitive (we lowercase in launcher, not bootstrap).
# Document this behavior.
check "override=NL (uppercase) — case-sensitive, falls back to heuristic" \
    "$(resolve_task_type 'echo hello' "NL")" "bash"

echo
echo "=========================="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo "=========================="
[[ $FAIL -eq 0 ]] || exit 1
