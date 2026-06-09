#!/usr/bin/env bash
# Tmux Worker Bootstrap — SOUL-aware, LLM-capable version
# Usage: bootstrap.sh <task_id> <workspace> <session_name>
#
# Modes:
#   NL task  → tmux-worker profile chat (SOUL baked into profile identity)
#   Bash    → runs task body, then quality gates
set -e

TASK_ID="$1"
WORKSPACE="$2"
SESSION="$3"

export TASK_ID WORKSPACE SESSION

HERMES_HOME="${HERMES_HOME:-/home/dmccarty/.hermes}"
PROJECT_DIR="$HERMES_HOME/PROJECTS/tmux-workers"
QUEUE_DIR="$PROJECT_DIR/queue"
WEBHOOK_URL="http://localhost:9876/webhook/completion"
SOUL_PATH="$PROJECT_DIR/GENERIC_worker_SOUL.md"

# -- Validate ---------------------------------------------------------
[[ ! -f "$QUEUE_DIR/$TASK_ID.json" ]] && { echo "FATAL: $QUEUE_DIR/$TASK_ID.json not found"; exit 1; }

# -- Parse task (OUTSIDE pipeline so vars survive subshell) -----------
TASK_BODY=$(python3 -c "import json,sys; d=json.load(open('$QUEUE_DIR/$TASK_ID.json')); print(d.get('body',''))")
TASK_TITLE=$(python3 -c "import json,sys; d=json.load(open('$QUEUE_DIR/$TASK_ID.json')); print(d.get('title',''))")
STORY_ID=$(python3 -c "import json,sys; d=json.load(open('$QUEUE_DIR/$TASK_ID.json')); print(d.get('story_id',''))" 2>/dev/null || echo "")
GOAL=$(python3 -c "import json,sys; d=json.load(open('$QUEUE_DIR/$TASK_ID.json')); print(d.get('goal',''))" 2>/dev/null || echo "")
GOAL_MAX_TURNS=$(python3 -c "import json,sys; d=json.load(open('$QUEUE_DIR/$TASK_ID.json')); print(d.get('goal_max_turns',5))" 2>/dev/null || echo "5")
# Optional explicit task_type override ("nl" or "bash"); empty = auto-detect.
# Use this to bypass is_bash_body()'s 20-line heuristic for long NL prompts.
TASK_TYPE_OVERRIDE=$(python3 -c "import json,sys; d=json.load(open('$QUEUE_DIR/$TASK_ID.json')); print(d.get('task_type',''))" 2>/dev/null || echo "")

# -- Write workspace files ---------------------------------------------
echo "task_id=$TASK_ID" >> "$WORKSPACE/.meta"
echo "story_id=$STORY_ID" >> "$WORKSPACE/.meta"
echo "$(date -Iseconds) started" > "$WORKSPACE/.started"
echo "$TASK_BODY" > "$WORKSPACE/task.md"
cp "$SOUL_PATH" "$WORKSPACE/WORKER_SOUL.md"

# -- Detect task type (outside subshell) ------------------------------
is_bash_body() {
    local b="$1"
    echo "$b" | grep -qE '^\s*(echo|cat|mkdir|ls|cd|export|curl|wget|python|pip|git|apt|chmod|mv|cp|rm|tar|gzip|zip|kill|sleep|exit|tee|grep|sed|awk|find|sort|uniq|head|tail|nc|ssh|scp|rsync|sudo|make|docker|openssl|base64|jq|yq|sleep |exit |cd )' && return 0
    echo "$b" | grep -qE '^\s*[a-z_]+=|^\s*\$[a-z_]+|^\s*\|^\s*&&|^\s*\|\||^\s*;' && return 0
    [[ $(echo "$b" | wc -l) -gt 20 ]] && return 0
    return 1
}
# Resolve task type: explicit override wins, else heuristic
case "$TASK_TYPE_OVERRIDE" in
    nl|bash) TASK_TYPE="$TASK_TYPE_OVERRIDE" ;;
    *)       is_bash_body "$TASK_BODY" && TASK_TYPE="bash" || TASK_TYPE="nl" ;;
esac
# Log the resolution path for debugging
if [[ -n "$TASK_TYPE_OVERRIDE" ]]; then
    echo "[$TASK_ID] task-type: $TASK_TYPE (override)" >> "$WORKSPACE/worker.log"
fi

# -- Logging — tee once; each line written exactly once to worker.log
{
echo "[$TASK_ID] $(date -Iseconds) bootstrap.sh START"
echo "[$TASK_ID] task: $TASK_TITLE"
echo "[$TASK_ID] SOUL copied to workspace"
echo "[$TASK_ID] task-type: $TASK_TYPE"

# -- Heartbeat --------------------------------------------------------
(
    while true; do
        echo "$(date -Iseconds) heartbeat" >> "$WORKSPACE/.heartbeat"
        sleep 60
    done
) &
HEARTBEAT_PID=$!
trap "kill $HEARTBEAT_PID 2>/dev/null || true" EXIT
} | tee -a "$WORKSPACE/worker.log"

# ═══════════════════════════════════════════════════════════════════════
#  MODE A: Natural Language — tmux-worker profile with optional goal loop
# ═══════════════════════════════════════════════════════════════════════
if [[ "$TASK_TYPE" == "nl" ]]; then
    echo "[$TASK_ID] MODE A: invoking LLM agent..."
    if [[ -n "$GOAL" ]]; then
        echo "[$TASK_ID] goal loop enabled: max $GOAL_MAX_TURNS turns"
    fi

    export HERMES_WORKSPACE="$WORKSPACE"
    TURN=0
    GOAL_DONE=false

    while [[ $TURN -lt $GOAL_MAX_TURNS && "$GOAL_DONE" != "true" ]]; do
        TURN=$((TURN + 1))
        echo "[$TASK_ID] ── turn $TURN/$GOAL_MAX_TURNS ──"

        if [[ $TURN -eq 1 ]]; then
            PROMPT="=== TASK ===
$(cat "$WORKSPACE/task.md")

=== WORKSPACE ===
Work inside: $WORKSPACE
Run: cd \$HERMES_WORKSPACE before touching files.

=== COMPLETION PROTOCOL ===
1. Do the work
2. Write \$HERMES_WORKSPACE/result.json  (schema in SOUL §5)
3. Write \$HERMES_WORKSPACE/checkpoint.json
4. Run: curl -s -o /dev/null -w \"%{http_code}\" -X POST \"$WEBHOOK_URL\" -H \"Content-Type: application/json\" -d @\"\$HERMES_WORKSPACE/result.json\"
5. Exit 0 if done, exit 1 if blocked/failed"
        else
            PROMPT="[Goal loop — turn $TURN/$GOAL_MAX_TURNS]
Goal: $GOAL

Previous result: $JUDGE_FEEDBACK
Continue working. Fix what's missing or incomplete. Update \$HERMES_WORKSPACE/result.json when truly done."
        fi

        tmux-worker chat \
            -t "terminal,file,web" \
            -Q \
            --pass-session-id \
            --max-turns 90 \
            -q "$PROMPT" 2>&1 | grep -v '^$' | tee -a "$WORKSPACE/worker.log"

        # Judge: evaluate if goal is met (uses LLM, not heuristic)
        if [[ -n "$GOAL" && -f "$WORKSPACE/result.json" ]]; then
            echo "[$TASK_ID] judging goal (LLM)..."
            JUDGE_RESULT=$(python3 -c "
import json, os, subprocess, sys

ws = '$WORKSPACE'
goal = '''$GOAL'''

# Read result + artifacts
try:
    result = json.load(open(os.path.join(ws, 'result.json')))
    summary = result.get('summary', '')[:3000]
    status = result.get('status', 'unknown')
    artifacts = result.get('artifacts', [])
    gates_p = result.get('gates_passed', [])
    gates_f = result.get('gates_failed', [])

    # Read artifact contents (first 500 chars each)
    artifact_contents = ''
    for a in artifacts[:5]:
        try:
            with open(a, 'r') as f:
                artifact_contents += f'\\n--- {a} ---\\n' + f.read()[:500]
        except: pass
except Exception as e:
    print(f'SKIP: result read error — {e}')
    sys.exit(0)

judge_prompt = f'''Goal:
{goal[:2000]}

Agent result summary:
{summary[:3000]}

Artifact contents:
{artifact_contents[:3000]}

Decision: Is the goal fully satisfied? Reply ONLY with a JSON object on one line:
{{\"done\": <true|false>, \"reason\": \"<one-sentence rationale>\"}}'''

# Call judge model (OpenRouter fallback since local GPUs busy)
try:
    import urllib.request
    api_key = os.environ.get('OPENROUTER_API_KEY', '')
    if not api_key:
        # Try reading from default .env
        try:
            with open(os.path.expanduser('~/.hermes/.env')) as f:
                for line in f:
                    if line.startswith('OPENROUTER_API_KEY='):
                        api_key = line.split('=',1)[1].strip().strip('\"').strip(\"'\")
                        break
        except: pass
    
    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps({
            'model': 'google/gemini-2.5-flash-lite',
            'messages': [
                {'role': 'system', 'content': 'You are a strict judge. Evaluate whether the goal was achieved. Reply ONLY with JSON: {\"done\": true/false, \"reason\": \"...\"}'},
                {'role': 'user', 'content': judge_prompt}
            ],
            'temperature': 0,
            'max_tokens': 200
        }).encode(),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
            'HTTP-Referer': 'http://localhost:9876',
            'X-Title': 'tmux-worker-goal-judge'
        }
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    raw = resp['choices'][0]['message']['content'].strip()
    
    # Parse JSON from response
    import re
    match = re.search(r'\{[^}]+\}', raw)
    if match:
        verdict = json.loads(match.group())
        if verdict.get('done'):
            print(f'DONE: {verdict.get(\"reason\", \"goal met\")}')
        else:
            print(f'CONTINUE: {verdict.get(\"reason\", \"not done\")}')
    else:
        print(f'CONTINUE: unparseable judge response: {raw[:100]}')
except Exception as e:
    print(f'SKIP: judge error — {e}')
" 2>&1)
            echo "[$TASK_ID] judge: $JUDGE_RESULT"
            
            if [[ "$JUDGE_RESULT" == DONE* ]]; then
                GOAL_DONE=true
                echo "[$TASK_ID] ✓ goal achieved on turn $TURN"
            elif [[ "$JUDGE_RESULT" == DONE_FAILED* ]]; then
                GOAL_DONE=true
                echo "[$TASK_ID] ✗ goal failed on turn $TURN"
            else
                JUDGE_FEEDBACK="$JUDGE_RESULT"
                echo "[$TASK_ID] ↻ continuing (judge: $JUDGE_FEEDBACK)"
            fi
        else
            GOAL_DONE=true  # no goal = one-shot
        fi
    done

    # Fallback: if LLM didn't write result.json, create failed result
    if [[ ! -f "$WORKSPACE/result.json" ]]; then
        python3 << PYEOF
import json
with open("$WORKSPACE/result.json", "w") as f:
    json.dump({
        "status": "failed",
        "task_id": "$TASK_ID",
        "summary": "LLM agent did not write result.json before exiting",
        "artifacts": [],
        "gates_passed": [],
        "gates_failed": ["llm_execution"],
        "blocked_reason": None,
        "failed_reason": "result.json not found after hermes chat run",
        "checkpoint": {"step": "llm-no-result", "next": "investigate"}
    }, f, indent=2)
PYEOF
    fi

# ═══════════════════════════════════════════════════════════════════════
#  MODE B: Bash task — run raw bash
# ═══════════════════════════════════════════════════════════════════════
else
    echo "[$TASK_ID] MODE B: executing bash task..."
    TASK_EXIT=0
    set +e
    bash -c "$TASK_BODY" >> "$WORKSPACE/worker.log" 2>&1
    TASK_EXIT=$?
    set -e
    echo "[$TASK_ID] bash exited: code=$TASK_EXIT"
fi

# ═══════════════════════════════════════════════════════════════════════
#  SHARED: Quality Gates + result.json + webhook + exit
# ═══════════════════════════════════════════════════════════════════════

# Pass TASK_EXIT via temp file (mktemp returns /tmp/tmp.XXXXXX — pass directly to Python)
TASK_EXIT_TMP=$(mktemp)
echo "${TASK_EXIT:-0}" > "$TASK_EXIT_TMP"

echo "[$TASK_ID] running quality gates..."
export HERMES_WORKSPACE="$WORKSPACE"
python3 << 'PYEOF'
import json, os, subprocess

workspace = os.environ["HERMES_WORKSPACE"]

def gate(name, passed, output):
    with open(os.path.join(workspace, f".gate_{name}.json"), "w") as f:
        json.dump({"passed": passed, "output": output[-200:]}, f)
    print(f"GATE[{name}] {'PASSED' if passed else 'FAILED'}")

def run_cmd(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120, cwd=workspace)
    return r.returncode, (r.stdout + r.stderr)[-500:]

# -- GATE 1: Tests -----------------------------------------------------
tests_dir = os.path.join(workspace, "tests")
has_tests = os.path.isdir(tests_dir) or any(f.startswith("test_") for f in os.listdir(workspace)) if os.path.isdir(workspace) else False
if has_tests:
    rc, out = run_cmd(f"cd {workspace} && python3 -m pytest tests/ test_*.py -v --tb=short 2>&1; echo EXIT_RC:$?")
    # Extract RC from "EXIT_RC:N" appended by bash subshell
    try:
        test_rc = int(out.strip().split("EXIT_RC:")[-1])
    except:
        test_rc = rc
    gate("tests", test_rc == 0, out)
else:
    gate("tests", True, "SKIPPED: no tests found")

# -- GATE 2: Linting --------------------------------------------------
linter_cmd = None
if os.path.exists(os.path.join(workspace, "ruff.toml")) or os.path.exists(os.path.join(workspace, "pyproject.toml")):
    linter_cmd = f"python3 -m ruff check {workspace} --output-format=text 2>&1; echo EXIT_RC:$?"
elif os.path.exists(os.path.join(workspace, "Makefile")) and "lint" in open(os.path.join(workspace, "Makefile")).read():
    linter_cmd = f"make -C {workspace} lint 2>&1; echo EXIT_RC:$?"
else:
    # py_compile fallback for Python projects
    pyfiles = [os.path.join(r, f) for r, ds, fs in os.walk(workspace) for f in fs if f.endswith(".py")]
    if pyfiles and len(pyfiles) <= 100:
        linter_cmd = f"python3 -m py_compile {' '.join(pyfiles)} 2>&1; echo EXIT_RC:$?"
if linter_cmd:
    rc, out = run_cmd(f"cd {workspace} && {linter_cmd}")
    try:
        lint_rc = int(out.strip().split("EXIT_RC:")[-1])
    except:
        lint_rc = rc
    gate("linting", lint_rc == 0, out)
else:
    gate("linting", True, "SKIPPED: no linter configured")

# -- GATE 3: Secrets ---------------------------------------------------
rc, out = run_cmd(
    f"grep -rPn '(AKIA|ghp_|sk-|BEGIN.*PRIVATE KEY|password\\s*=\\s*[\\\"\\047])' {workspace} "
    f"--include='*.py' --include='*.sh' --include='*.json' --include='*.yaml' "
    f"--exclude-dir=.venv --exclude-dir=__pycache__ -l 2>&1; echo EXIT_RC:$?"
)
try:
    sec_rc = int(out.strip().split("EXIT_RC:")[-1])
except:
    sec_rc = rc
gate("secrets", sec_rc == 1, out)

# -- GATE 4: Code quality ---------------------------------------------
cqc = os.path.join(workspace, ".gate_code_quality.json")
if os.path.exists(cqc):
    data = json.load(open(cqc))
    gate("code_quality", data.get("passed", True), data.get("output", ""))
else:
    gate("code_quality", True, "SKIPPED: no code_quality gate result")

# -- GATE 5: Commit ----------------------------------------------------
if os.path.exists(os.path.join(workspace, ".git")):
    rc, out = run_cmd(f"git -C {workspace} status --porcelain 2>&1 | head -5; echo EXIT_RC:$?")
    try:
        git_rc = int(out.strip().split("EXIT_RC:")[-1])
    except:
        git_rc = rc
    gate("commit", git_rc == 0, out)
else:
    gate("commit", True, "SKIPPED: not a git repo")

print("ALL_GATES_DONE")
PYEOF

# ═══════════════════════════════════════════════════════════════════════
#  SHARED: result.json + webhook + exit
# ═══════════════════════════════════════════════════════════════════════

export WORKSPACE TASK_ID SESSION STORY_ID

# Aggregate gate results
python3 << PYEOF
import json, os, subprocess

workspace = os.environ.get("WORKSPACE", os.getcwd())
task_id   = os.environ.get("TASK_ID",   "")
session   = os.environ.get("SESSION",   "")
story_id  = os.environ.get("STORY_ID",  "")

gates = ["tests", "linting", "secrets", "code_quality", "commit"]
passed, failed = [], []
for g in gates:
    try:
        data = json.load(open(os.path.join(workspace, f".gate_{g}.json")))
        (passed if data.get("passed") else failed).append(g)
    except:
        passed.append(g)  # missing → skip

with open(os.path.join(workspace, ".gate_summary.json"), "w") as f:
    json.dump({"passed_gates": passed, "failed_gates": failed, "all_passed": len(failed)==0}, f, indent=2)

task_exit = int(open("$TASK_EXIT_TMP").read().strip()) if os.path.exists("$TASK_EXIT_TMP") else 0
log = open(os.path.join(workspace, "worker.log")).read()[-500:].strip()

if task_exit != 0:
    status = "failed"
elif not os.path.exists(os.path.join(workspace, "result.json")):
    status = "done" if len(failed)==0 else "blocked"
else:
    # result.json exists, preserve LLM's status if it made one
    existing = json.load(open(os.path.join(workspace, "result.json")))
    status = existing.get("status", "done" if len(failed)==0 else "blocked")

artifacts = [f for f in os.listdir(workspace)
             if f not in ("task.md","WORKER_SOUL.md","result.json",".meta",".heartbeat",".started")
             and not f.startswith(".gate_") and f != "worker.log"]
commit_sha = subprocess.getoutput(f"git -C {workspace} rev-parse HEAD 2>/dev/null") if os.path.exists(os.path.join(workspace,".git")) else ""

result = {
    "status": status,
    "task_id": task_id,
    "session": session,
    "story_id": story_id,
    "summary": log,
    "artifacts": artifacts,
    "gates_passed": passed,
    "gates_failed": failed,
    "tests_run": "tests" in passed,
    "tests_passed": "tests" not in failed,
    "linter_passed": "linting" not in failed,
    "secrets_passed": "secrets" not in failed,
    "code_quality_passed": "code_quality" not in failed,
    "commit_sha": commit_sha,
    "exit_code": task_exit,
    "blocked_reason": None if status != "blocked" else "gates failed: " + ", ".join(failed),
    "failed_reason": None if status != "failed" else f"exit code {task_exit}",
    "checkpoint": {"step": f"status={status}", "next": "N/A"}
}
with open(os.path.join(workspace, "result.json"), "w") as f:
    json.dump(result, f, indent=2)
print(f"STATUS:{status} GATES:{len(passed)}p/{len(failed)}f")
PYEOF
rm -f "$TASK_EXIT_TMP"
RESULT_STATUS=$(python3 -c "
import json
d=json.load(open('$WORKSPACE/result.json'))
print(d.get('status','unknown'))
")
echo "[$TASK_ID] result.json status: $RESULT_STATUS"

# -- Post webhook -----------------------------------------------------
echo "[$TASK_ID] posting to $WEBHOOK_URL..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d @"$WORKSPACE/result.json")
echo "[$TASK_ID] webhook HTTP $HTTP_CODE"

# -- Completion (keep session alive for reattach) -----------------------
echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  TASK COMPLETE — status: $RESULT_STATUS"
echo "  Session stays alive for reattach/inspection."
echo "  To reattach:  tmux attach -t $SESSION"
echo "  To kill:      tmux kill-session -t $SESSION"
echo "══════════════════════════════════════════════════════════════"
echo ""
# Update heartbeat to reflect completion
echo "$(date -Iseconds) completed ($RESULT_STATUS)" >> "$WORKSPACE/.heartbeat"

# Keep session alive for reattach — drop to interactive shell
if [[ "$RESULT_STATUS" == "done" ]]; then
    echo "Type 'exit' or press Ctrl-D to close this session."
    exec bash --rcfile <(echo "PS1='[$TASK_ID ✅] \w \$ '; cd '$WORKSPACE'")
else
    echo "Type 'exit' or press Ctrl-D to close this session."
    exec bash --rcfile <(echo "PS1='[$TASK_ID ❌] \w \$ '; cd '$WORKSPACE'")
fi
