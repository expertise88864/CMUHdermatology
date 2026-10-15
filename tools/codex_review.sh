#!/usr/bin/env bash
# tools/codex_review.sh — 本 repo 唯一的外部 code-review 入口(Codex CLI, read-only)。
# 仿 -morning-report-main/tools/codex_review.sh;新增 CODEX_REVIEW_TIP 支援「已合併歷史範圍」審計。
#
# 用法:
#   tools/codex_review.sh <mode> <base-ref> [task-context-file]
#   CODEX_REVIEW_TIP=<commit> tools/codex_review.sh <mode> <base-ref> [ctx]   # 審 base..TIP 歷史範圍
#   tools/codex_review.sh resume [session-id]
#
# mode:
#   diff      低風險/局部(文案、註解、CSS、tests-only)     xhigh / 額外檔 3 / findings 3
#   targeted  一般非 trivial 實作(預設)                     xhigh / 額外檔 12 / findings 5
#   deep      醫療劑量、醫令自動化、資安、資料完整、併發、大重構 xhigh / 額外檔 30 / findings 8
#
# 模型與 effort(使用者 2026-10-14 定案):任何 diff review 一律 gpt-6.1-sol / xhigh,
# 不 fallback 到其他模型或較低 effort。模式只差範圍。實際模型/effort/唯讀沙箱由
# tools/codex_review_identity.py 對 session 紀錄驗證,證明不了就不算一輪(exit 4)。
# CLI:CODEX_REVIEW_CLI 明示,否則 `codex_review_identity.py which` 挑本機版本最高者
# (npm 0.155.1 不支援 gpt-6.1-sol,Codex 桌面版內附的 0.162 alpha 可以)。
#   resume    後續輪(第 2、3、…;僅限 confirmed findings 修正後);沿用第一輪 session
#
# 設計原則(勿改):
#   * 絕不把完整 diff 放進 prompt 或 argv;Codex 在 repo 內自行跑 git。
#   * --ignore-user-config 隔離 ~/.codex/config.toml(不載 plugins/apps/browser/notify)。
#   * --sandbox read-only:Codex 不得寫檔、commit、跑 tests/build/lint/probe。
#   * [2026-07-13 使用者定案] 無輪數上限:每輪修正 confirmed findings 後 resume 同一
#     session 續審,直到 APPROVE 才 push(不得重建 session、不得 --last)。
#   * 結果只讀「最後一則訊息」(-o),不掃整份輸出。
set -uo pipefail

MODEL="gpt-6.1-sol"
HARDEN="${CODEX_REVIEW_HARDEN:-1}"
STRICT="${CODEX_REVIEW_STRICT:-1}"
TIP="${CODEX_REVIEW_TIP:-HEAD}"

die() { echo "[codex-review] ERROR: $*" >&2; exit 64; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || die "必須在 git repository 內執行。"
REPO_NAME="$(basename "$REPO_ROOT")"
if [ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]; then PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"
elif command -v python >/dev/null 2>&1; then PYTHON="python"
elif command -v python3 >/dev/null 2>&1; then PYTHON="python3"
else die "找不到 Python,無法驗證審查身分。"; fi
STATE_DIR="$REPO_ROOT/.codex-review"
mkdir -p "$STATE_DIR"
USAGE_TSV="$STATE_DIR/usage.tsv"
SESSION_FILE="$STATE_DIR/last_session_id"
PASS_FILE="$STATE_DIR/last_pass"
LAST_MSG="$STATE_DIR/last_message.txt"
RAW_LOG="$STATE_DIR/last_raw.log"

[ -s "$USAGE_TSV" ] || printf 'timestamp\trepository\tmode\tmodel\teffort\tbase_ref\ttip_ref\tsession_id\ttokens_used\tresult\tfindings\tpass\n' > "$USAGE_TSV"

MODE="${1:-}"
[ -n "$MODE" ] || die "缺少 mode。用法: $0 <diff|targeted|deep> <base-ref> [ctx] | $0 resume [sid]"

case "$MODE" in
  diff)     EFFORT="xhigh";  EXTRA_FILE_LIMIT=3;  FINDING_LIMIT=3 ;;
  targeted) EFFORT="xhigh";  EXTRA_FILE_LIMIT=12; FINDING_LIMIT=5 ;;
  deep)     EFFORT="xhigh";  EXTRA_FILE_LIMIT=30; FINDING_LIMIT=8 ;;
  resume)   EFFORT="";       EXTRA_FILE_LIMIT="";  FINDING_LIMIT="" ;;
  *) die "未知 mode '$MODE'(可用:diff | targeted | deep | resume)" ;;
esac

build_flags() {   # $1 = effort ; $2 = kind("exec" | "resume")
  # --json:session id 與 token 用量改由機器可讀事件 + session 紀錄驗證,不再掃文字。
  FLAGS=(--ignore-user-config --json --model "$MODEL" -c "model_reasoning_effort=$1" -o "$LAST_MSG")
  if [ "${2:-exec}" = "resume" ]; then
    FLAGS+=(-c "sandbox_mode=read-only")
  else
    FLAGS+=(--sandbox read-only --cd "$REPO_ROOT")
  fi
  [ "$STRICT" = "1" ] && FLAGS+=(--strict-config)
  if [ "$HARDEN" = "1" ]; then
    FLAGS+=(-c "web_search=disabled" -c "features.apps=false")
  fi
  # --ignore-user-config 也丟掉使用者的 [windows] sandbox 設定;少了它,Windows 的
  # 唯讀沙箱會拒絕每一個指令(連 git diff 都不行),審查者什麼都讀不到。
  case "${OS:-}$(uname -s 2>/dev/null)" in
    *Windows_NT*|*MINGW*|*MSYS*|*CYGWIN*) FLAGS+=(-c "windows.sandbox=unelevated") ;;
  esac
}

# 沙箱用受限權杖執行,repo 目錄擁有者是 BUILTIN\Administrators 時 git 會回
# 「dubious ownership」而讀不到 diff。只對審查子行程、只把本 repo 標成 safe:
# GIT_CONFIG_PARAMETERS 屬命令列層級,git 承認它的 safe.directory;不動全域設定。
resolve_cli() {
  if [ -n "${CODEX_REVIEW_CLI:-}" ]; then
    CODEX_CLI="$CODEX_REVIEW_CLI"
  else
    # -X utf8: Python writes the (possibly non-ASCII) path as UTF-8 even when piped.
    CODEX_CLI="$("$PYTHON" -X utf8 "$SCRIPT_DIR/codex_review_identity.py" which | head -1 | tr -d '\r')" ||
      CODEX_CLI=""
  fi
  [ -n "$CODEX_CLI" ] || die "找不到可用的 Codex CLI;不降級到其他模型。"
  echo "[codex-review] cli=$CODEX_CLI"
  local quoted
  quoted="$(printf '%s' "$REPO_ROOT" | sed "s/'/'\\\\''/g")"
  export GIT_CONFIG_PARAMETERS="${GIT_CONFIG_PARAMETERS:+$GIT_CONFIG_PARAMETERS }'safe.directory'='$quoted'"
}

VERIFIED_SID=""
VERIFIED_TOKENS=""
# 實際 session 必須是本輪、本 repo、gpt-6.1-sol/xhigh/read-only;$1 = 續審時預期的 session。
verify_actual_session() {
  local identity
  local a=("$SCRIPT_DIR/codex_review_identity.py" --raw-log "$RAW_LOG"
           --repo "$REPO_ROOT" --started-at "$REVIEW_STARTED_AT"
           --proof "$STATE_DIR/last_identity.json")
  rm -f "$STATE_DIR/last_identity.json"
  [ -z "${1:-}" ] || a+=(--expected-session "$1")
  identity="$("$PYTHON" "${a[@]}")" || return 1
  IFS=$'\t' read -r VERIFIED_SID VERIFIED_TOKENS <<< "$identity"
  [ -n "$VERIFIED_SID" ] && [ -n "$VERIFIED_TOKENS" ]
}

# 舊版接受「任意 36 個 hex 或連字號」,那不是 UUID 形狀 —— 全連字號也會過,
# 然後被寫進 last_session_id 成為之後每一輪都信任的壞紀錄。改為錨定 8-4-4-4-12。
extract_session_id() { printf '%s' "$VERIFIED_SID"; }
extract_tokens() { printf '%s' "$VERIFIED_TOKENS"; }
extract_result() {
  [ -s "$LAST_MSG" ] || { echo "UNKNOWN"; return; }
  if grep -q 'REQUEST_CHANGES' "$LAST_MSG"; then echo "REQUEST_CHANGES"
  elif grep -q 'APPROVE' "$LAST_MSG"; then echo "APPROVE"
  else echo "UNKNOWN"; fi
}
extract_findings() {
  [ -s "$LAST_MSG" ] || { echo "unavailable"; return; }
  local n; n="$(grep -ciE '^[[:space:]]*[-*]?[[:space:]]*severity:' "$LAST_MSG" || true)"
  if grep -q 'NO_ACTIONABLE_FINDINGS' "$LAST_MSG"; then echo 0
  elif [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null; then echo "$n"
  else echo "unavailable"; fi
}
is_rate_limited() { grep -qiE 'usage limit|rate limit|try again at' "$RAW_LOG" 2>/dev/null; }

log_usage() {  # $1 mode $2 effort $3 base $4 pass
  local sid tok res fnd
  sid="$(extract_session_id)"; [ -n "$sid" ] || sid="unavailable"
  tok="$(extract_tokens)";     [ -n "$tok" ] || tok="unavailable"
  res="$(extract_result)"
  fnd="$(extract_findings)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$REPO_NAME" "$1" "$MODEL" "$2" "$3" "$TIP" "$sid" "$tok" "$res" "$fnd" "$4" >> "$USAGE_TSV"
  [ "$sid" = "unavailable" ] || printf '%s' "$sid" > "$SESSION_FILE"
  echo "$res"
}

# ================= resume(後續輪:第 2、3、…直到 APPROVE) =================
# [2026-07-13 使用者定案] 取消「每 task 最多兩輪」硬上限,改為「跑到 GPT-5.6 APPROVE
# 才能 push」。每一輪修正 confirmed findings 後 resume 同一 session 續審;pass 累加。
if [ "$MODE" = "resume" ]; then
  # 續審必須沿用同一 session。這裡有三個獨立的失敗模式,少一個都會讓
  # 「每一輪都 resume 同一 session」的規則失效:
  #   1. 沒給 SID          → 必須有紀錄可用,否則不知道要續哪一場。
  #   2. 給了但與紀錄不符  → 會續到**別場 review**,拿它的 APPROVE 放行 push。
  #   3. 給了但根本沒紀錄  → 無從比對,等於未經驗證就續審。
  # 另外仍保留 UUID 格式檢查:實測 `codex exec resume <非UUID>` 不會報錯,
  # 它會**靜默開一個全新 session**,輸出與正常 resume 完全無法區分。
  # UUID 大小寫不敏感,但 shell 字串比較敏感。格式檢查接受 A-F,所以貼上大寫版本
  # 的**同一個** session 會被判成「不同 session」而拒絕 —— 那會把人推去開新
  # session,正是本守衛要防的事。兩邊都正規化成小寫再比(codex 輸出本即小寫)。
  RECORDED_SID="$(cat "$SESSION_FILE" 2>/dev/null | tr 'A-Z' 'a-z' || true)"
  SID="$(printf '%s' "${2:-}" | tr 'A-Z' 'a-z')"
  if [ -z "$SID" ]; then
    [ -n "$RECORDED_SID" ] || die "找不到第一輪 session id($SESSION_FILE 不存在)。請先在同一 repo 完成第一輪,不要用 --last(可能 resume 到別的專案)。"
    SID="$RECORDED_SID"
  elif [ -n "$RECORDED_SID" ] && [ "$SID" != "$RECORDED_SID" ]; then
    die "提供的 session id 與本 repo 第一輪紀錄不符。
  提供:$SID
  紀錄:$RECORDED_SID
resume 只能沿用同一 task 的 session。要重開一輪全新審查請跑第一輪,不要用 resume 指向別場。"
  elif [ -z "$RECORDED_SID" ]; then
    die "本 repo 沒有第一輪 session 紀錄可比對;拒絕以未經驗證的 session id resume。"
  fi
  if ! printf '%s' "$SID" | grep -Eq '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'; then
    die "session id 格式不正確:'$SID'(需為 UUID)。不要把 task-context 檔路徑傳到這個位置。"
  fi
  PREV_PASS="$(cat "$PASS_FILE" 2>/dev/null || echo 0)"
  [ "$PREV_PASS" -ge 1 ] 2>/dev/null \
    || die "resume 只能在完成第一輪之後(目前 pass=$PREV_PASS)。請先跑第一輪。"
  THIS_PASS=$((PREV_PASS + 1))
  RESUME_EFFORT="xhigh"  # 2026-10-14:一律 xhigh,不沿用舊紀錄裡的 effort
  RESUME_BASE="$(tail -1 "$USAGE_TSV" | cut -f6)";   [ -n "$RESUME_BASE" ] || RESUME_BASE="unavailable"
  build_flags "$RESUME_EFFORT" resume
  read -r -d '' RESUME_PROMPT <<'RP' || true
Follow-up review pass. Inspect only the corrections made for CONFIRMED findings
from the previous review. Verify that those defects are resolved and that the
corrections introduced no concrete regression. Do not repeat the original full
exploration. Remain strictly read-only. End with exactly APPROVE or
REQUEST_CHANGES.
RP
  # PR flow: the head moved after the fixes; name the exact range under review.
  if [ -n "${CODEX_REVIEW_BASE:-}" ] && [ "$TIP" != "HEAD" ]; then
    RESUME_PROMPT="$RESUME_PROMPT

The pull request range is now ${CODEX_REVIEW_BASE}..${TIP}; inspect the corrections with git diff/log inside that range."
  fi
  # 續審時上一次裁決所審的 head(pr_review.py 設定):續審必須涵蓋 PREV..TIP 的**所有**變更,
  # 不只修正 —— 否則 APPROVE 後再推的其他 commit 會在「只看修正」的指示下被略過。
  PREV_TIP="${CODEX_REVIEW_PREV_TIP:-}"
  if [ -n "$PREV_TIP" ]; then
    printf '%s' "$PREV_TIP" | grep -Eq '^[0-9a-f]{40}$' || die "CODEX_REVIEW_PREV_TIP 必須是完整 40 碼 SHA。"
    [ "$TIP" != "HEAD" ] && [ -n "${CODEX_REVIEW_BASE:-}" ] || die "CODEX_REVIEW_PREV_TIP 只用於 PR 續審(需同時有 CODEX_REVIEW_BASE 與 CODEX_REVIEW_TIP)。"
    git -C "$REPO_ROOT" merge-base --is-ancestor "$PREV_TIP" "$TIP" 2>/dev/null || die "CODEX_REVIEW_PREV_TIP 不是 CODEX_REVIEW_TIP 的祖先;請重跑第一輪。"
    # An APPROVE here covers the whole new head, so every new commit is in scope.
    RESUME_PROMPT="$RESUME_PROMPT

Scope override: your previous verdict covered ${PREV_TIP}. Review EVERY change in ${PREV_TIP}..${TIP} (git log/diff ${PREV_TIP}..${TIP}), not only corrections: any other commit pushed since then gets the same full review contract, severities and limits as the first pass. Also confirm the previous CONFIRMED findings are resolved."
  fi
  # 反駁續審(使用者定案 2026-10-15):實作者以證據 REJECTED 的 finding,可在同一 session
  # 請審查者重新查證一次;審查者必須自己在 repo 裡核實,不能只憑說法撤回。
  REBUTTAL_FILE="${CODEX_REVIEW_REBUTTAL_FILE:-}"
  if [ -n "$REBUTTAL_FILE" ]; then
    [ -f "$REBUTTAL_FILE" ] || die "rebuttal 檔不存在:$REBUTTAL_FILE"
    if grep -qE '^(diff --git |@@ |index [0-9a-f]+\.\.)' "$REBUTTAL_FILE"; then
      die "rebuttal 檔看起來含 diff。只能放對 REJECTED finding 的證據說明。"
    fi
    RESUME_PROMPT="$RESUME_PROMPT

Implementer rebuttal: the implementer validated your previous findings against the code and REJECTED the ones below with evidence; no code was changed for them. Re-examine each rejected finding in the repository yourself (read-only). Withdraw it only if the evidence holds; otherwise keep it and state the concrete reason. Never withdraw a finding on assertion alone. Rebuttal text:
$(cat "$REBUTTAL_FILE")"
  fi
  echo "[codex-review] resume session=$SID effort=$RESUME_EFFORT (pass $THIS_PASS)"
  : > "$LAST_MSG"
  # ★stdin 一律導自 /dev/null★(2026-08-30 實測):codex CLI 在 stdin 不是
  #   TTY 時會印 "Reading additional input from stdin..." 並【等】額外輸入。
  #   背景執行(自動化)時那個 stdin 永遠不會 EOF —— 實際發生過一次卡死
  #   10 小時零輸出、一次被限流守衛判成「無結論」而 exit 4。prompt 本來就
  #   是用參數傳的,stdin 沒有任何用途。
  resolve_cli
  REVIEW_STARTED_AT="$("$PYTHON" -c 'import time; print(time.time())')" || die "Python unavailable"
  ( cd "$REPO_ROOT" && "$CODEX_CLI" exec resume "$SID" "${FLAGS[@]}" "$RESUME_PROMPT" ) </dev/null 2>&1 | tee "$RAW_LOG"
  CODEX_RC=${PIPESTATUS[0]}
  if [ "$CODEX_RC" -ne 0 ] && [ ! -s "$LAST_MSG" ]; then
    die "codex exec resume 啟動失敗(exit=$CODEX_RC),未產生 review;本輪不計。"
  fi
  if ! verify_actual_session "$SID"; then
    echo "[codex-review] 實際 session 驗證失敗(模型/effort/沙箱/session 不符)—— 本輪不計,勿據此 push。" >&2
    exit 4
  fi
  echo "$THIS_PASS" > "$PASS_FILE"
  RESULT="$(log_usage "resume" "$RESUME_EFFORT" "$RESUME_BASE" "$THIS_PASS")"
  echo; echo "[codex-review] result=$RESULT (pass $THIS_PASS)"
  # 限流僅在「未產生明確結論」時才視為不可信 —— 否則 review 的 diff 內容含 'rate limit'
  # 字面(如本 wrapper 自身原始碼)會誤判。有 APPROVE/REQUEST_CHANGES 即代表完整產出。
  if is_rate_limited && [ "$RESULT" = "UNKNOWN" ]; then
    echo "[codex-review] Codex 限流且無明確結論,結果不可信。" >&2; exit 4; fi
  case "$RESULT" in APPROVE) exit 0 ;; REQUEST_CHANGES) exit 2 ;; *) exit 5 ;; esac
fi

# ================= 第一輪 =================
BASE="${2:-}"
[ -n "$BASE" ] || die "缺少 base-ref。例:$0 $MODE origin/main [ctx]"
git -C "$REPO_ROOT" rev-parse --verify --quiet "${BASE}^{commit}" >/dev/null \
  || die "base-ref '$BASE' 無法解析為 commit。"
git -C "$REPO_ROOT" rev-parse --verify --quiet "${TIP}^{commit}" >/dev/null \
  || die "tip-ref '$TIP' 無法解析為 commit。"

CTX_FILE="${3:-}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
if [ -n "$CTX_FILE" ]; then
  [ -f "$CTX_FILE" ] || die "task-context 檔不存在:$CTX_FILE"
  if grep -qE '^(diff --git |@@ |index [0-9a-f]+\.\.)' "$CTX_FILE"; then
    die "task-context 檔看起來含 diff。只能放任務摘要/驗收標準/預期行為/non-goals/本機測試/已知限制。"
  fi
  cp "$CTX_FILE" "$TMP/ctx.txt"
else
  printf '(no task context supplied)\n' > "$TMP/ctx.txt"
fi
if [ -n "${CODEX_REVIEW_VERIFICATION:-}" ]; then printf '%s\n' "$CODEX_REVIEW_VERIFICATION" > "$TMP/ver.txt";
else printf '(not supplied by caller)\n' > "$TMP/ver.txt"; fi

cat > "$TMP/prompt.txt" <<'PROMPT'
You are an independent senior software engineer performing a read-only code
review of an implementation written by another coding model.

REVIEW MODE:
{{MODE}}

REVIEW RANGE:
Committed range {{BASE}}..{{TIP}} (one whole pull request: merge-base of main .. exact PR head).

TASK CONTEXT:
{{TASK_CONTEXT}}

LOCAL VERIFICATION:
{{VERIFICATION_RESULTS}}

Operate strictly in read-only mode.

Do not modify, create, delete, rename, format, stage, commit, revert, or patch
any file. Do not install dependencies. Do not run tests, builds, linters,
formatters, package managers, migrations, application code, or ad hoc
Python/Node probes. Do not use web search, browser, computer use, apps,
connectors, plugins, or external MCP tools.

Start from the repository's actual Git state:

1. Inspect `git status --short`. If the working tree has uncommitted changes,
   review those.
2. Review the committed change range with `git log --oneline {{BASE}}..{{TIP}}`
   and `git diff {{BASE}}..{{TIP}}`. The working tree may be clean; the changes
   under review live in that committed range.
3. Include relevant untracked or unchanged source files explicitly when needed
   to judge correctness.
4. Identify the exact runtime behavior changed by the implementation across the
   range.

Start from the diff, but do not limit the review to changed lines when directly
related repository context is required. Repository exploration must be driven by
a concrete concern raised by the diff.

Prioritize:

1. Direct callers and downstream consumers.
2. Referenced interfaces, schemas, shared types, and contracts.
3. Tests directly related to the changed behavior.
4. One analogous implementation when required.

Do not perform a whole-repository audit.

Unless a concrete P0 or P1 risk requires expansion:

- inspect no more than {{EXTRA_FILE_LIMIT}} additional files outside the diff
- report no more than {{FINDING_LIMIT}} findings
- do not inspect unrelated directories
- do not inspect generated files, vendored code, build output, caches, or
  dependency directories
- stop when no high-confidence actionable failure path remains

Report only concrete defects involving:

- incorrect behavior
- regression
- security or authorization
- data integrity
- compatibility
- concurrency or idempotency
- resource leaks
- material error-handling failures
- realistic performance pathologies

For this codebase specifically, weight heavily:

- medical dose/text miscomputation written back into the hospital system (UVB
  dose stitching, decrement/increment orders, excimer per-segment dose)
- hotkey (F1-F12) HIS automation clicking or sending to the wrong window,
  wrong field, or when it should have aborted (F12 cancel / interrupt)
- PHI leakage into logs or temp files

Do not report: style, naming, formatting, optional refactors, generic best
practices, speculative concerns, pre-existing unrelated problems, missing
comments, duplicated findings, or issues already prevented by existing
validation or contracts.

Every finding must include: severity (P0/P1/P2/P3); confidence
(high/medium/low); exact file and smallest useful line range; concrete trigger;
observable failure; repository evidence; why existing tests do not detect it;
minimal correction direction.

If no qualifying defect is found, output: NO_ACTIONABLE_FINDINGS

End with exactly one of:
APPROVE
REQUEST_CHANGES
PROMPT

sed -i -e "s|{{MODE}}|$MODE|g" -e "s|{{BASE}}|$BASE|g" -e "s|{{TIP}}|$TIP|g" \
       -e "s|{{EXTRA_FILE_LIMIT}}|$EXTRA_FILE_LIMIT|" -e "s|{{FINDING_LIMIT}}|$FINDING_LIMIT|" "$TMP/prompt.txt"
sed -i -e "/{{TASK_CONTEXT}}/r $TMP/ctx.txt" -e "/{{TASK_CONTEXT}}/d" "$TMP/prompt.txt"
sed -i -e "/{{VERIFICATION_RESULTS}}/r $TMP/ver.txt" -e "/{{VERIFICATION_RESULTS}}/d" "$TMP/prompt.txt"

build_flags "$EFFORT"
echo "[codex-review] mode=$MODE effort=$EFFORT range=$BASE..$TIP model=$MODEL (pass 1, read-only)"
# 第一輪開始就清掉上一場的 pass 與 session 紀錄:本輪若驗證失敗,不可讓 resume
# 接回上一場(那會把舊 session 的 APPROVE 當成這次的結論)。
echo 0 > "$PASS_FILE"
rm -f "$SESSION_FILE"
: > "$LAST_MSG"
# ★stdin 一律導自 /dev/null★(2026-08-30 實測):codex CLI 在 stdin 不是
#   TTY 時會印 "Reading additional input from stdin..." 並【等】額外輸入。
#   背景執行(自動化)時那個 stdin 永遠不會 EOF —— 實際發生過一次卡死
#   10 小時零輸出、一次被限流守衛判成「無結論」而 exit 4。prompt 本來就
#   是用參數傳的,stdin 沒有任何用途。
resolve_cli
REVIEW_STARTED_AT="$("$PYTHON" -c 'import time; print(time.time())')" || die "Python unavailable"
"$CODEX_CLI" exec "${FLAGS[@]}" "$(cat "$TMP/prompt.txt")" </dev/null 2>&1 | tee "$RAW_LOG"
CODEX_RC=${PIPESTATUS[0]}
if [ "$CODEX_RC" -ne 0 ] && [ ! -s "$LAST_MSG" ]; then
  die "codex exec 啟動失敗(exit=$CODEX_RC),未產生 review。請檢查 CLI 版本與旗標。"
fi
if ! verify_actual_session; then
  echo "[codex-review] 實際 session 驗證失敗(模型/effort/沙箱/session 不符)—— 本輪不計,勿據此 push。" >&2
  exit 4
fi

echo 1 > "$PASS_FILE"
RESULT="$(log_usage "$MODE" "$EFFORT" "$BASE" 1)"
echo; echo "[codex-review] result=$RESULT (pass 1)  usage → $USAGE_TSV"
# 限流僅在「未產生明確結論」時才視為不可信(避免 review 的 diff 內容含 'rate limit' 字面誤判)。
if is_rate_limited && [ "$RESULT" = "UNKNOWN" ]; then
  echo "[codex-review] Codex 限流且無明確結論,結果不可信,勿據此修改。" >&2; exit 4; fi
case "$RESULT" in
  APPROVE) exit 0 ;;
  REQUEST_CHANGES) exit 2 ;;
  *) echo "[codex-review] 未取得明確 APPROVE/REQUEST_CHANGES;人工檢視 $LAST_MSG" >&2; exit 5 ;;
esac
