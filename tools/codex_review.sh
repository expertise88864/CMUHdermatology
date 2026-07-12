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
#   diff      低風險/局部(文案、註解、CSS、tests-only)     medium / 額外檔 3 / findings 3
#   targeted  一般非 trivial 實作(預設)                     medium / 額外檔 12 / findings 5
#   deep      醫療劑量、醫令自動化、資安、資料完整、併發、大重構 high / 額外檔 30 / findings 8
#   resume    第二輪(僅限 confirmed P0/P1/material P2 修正後);沿用第一輪 session
#
# 設計原則(勿改):
#   * 絕不把完整 diff 放進 prompt 或 argv;Codex 在 repo 內自行跑 git。
#   * --ignore-user-config 隔離 ~/.codex/config.toml(不載 plugins/apps/browser/notify)。
#   * --sandbox read-only:Codex 不得寫檔、commit、跑 tests/build/lint/probe。
#   * 每個 task 最多兩輪;第二輪必須 resume 同一 session,不得重建。
#   * 結果只讀「最後一則訊息」(-o),不掃整份輸出。
set -uo pipefail

MODEL="gpt-5.6-sol"
HARDEN="${CODEX_REVIEW_HARDEN:-1}"
STRICT="${CODEX_REVIEW_STRICT:-1}"
TIP="${CODEX_REVIEW_TIP:-HEAD}"

die() { echo "[codex-review] ERROR: $*" >&2; exit 64; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" \
  || die "必須在 git repository 內執行。"
REPO_NAME="$(basename "$REPO_ROOT")"
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
  diff)     EFFORT="medium"; EXTRA_FILE_LIMIT=3;  FINDING_LIMIT=3 ;;
  targeted) EFFORT="medium"; EXTRA_FILE_LIMIT=12; FINDING_LIMIT=5 ;;
  deep)     EFFORT="high";   EXTRA_FILE_LIMIT=30; FINDING_LIMIT=8 ;;
  resume)   EFFORT="";       EXTRA_FILE_LIMIT="";  FINDING_LIMIT="" ;;
  *) die "未知 mode '$MODE'(可用:diff | targeted | deep | resume)" ;;
esac

build_flags() {   # $1 = effort ; $2 = kind("exec" | "resume")
  FLAGS=(--ignore-user-config --model "$MODEL" -c "model_reasoning_effort=$1" -o "$LAST_MSG")
  if [ "${2:-exec}" = "resume" ]; then
    FLAGS+=(-c "sandbox_mode=read-only")
  else
    FLAGS+=(--sandbox read-only --cd "$REPO_ROOT")
  fi
  [ "$STRICT" = "1" ] && FLAGS+=(--strict-config)
  if [ "$HARDEN" = "1" ]; then
    FLAGS+=(-c "web_search=disabled" -c "features.apps=false")
  fi
}

extract_session_id() { grep -oiE 'session id:[[:space:]]*[0-9a-f-]{36}' "$RAW_LOG" 2>/dev/null | head -1 | grep -oiE '[0-9a-f-]{36}' || true; }
extract_tokens() { awk 'tolower($0) ~ /tokens used/ {found=1; next} found && $0 ~ /[0-9]/ {gsub(/[^0-9]/,"",$0); if (length($0)) {print $0; exit}}' "$RAW_LOG" 2>/dev/null || true; }
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

# ================= resume(第二輪) =================
if [ "$MODE" = "resume" ]; then
  SID="${2:-}"
  if [ -z "$SID" ]; then
    [ -s "$SESSION_FILE" ] || die "找不到第一輪 session id。請:$0 resume <session-id>。不要用 --last。"
    SID="$(cat "$SESSION_FILE")"
  fi
  PREV_PASS="$(cat "$PASS_FILE" 2>/dev/null || echo 0)"
  [ "$PREV_PASS" = "1" ] || die "第二輪只能在完成第一輪之後(目前 pass=$PREV_PASS)。每 task 最多兩輪。"
  RESUME_EFFORT="$(tail -1 "$USAGE_TSV" | cut -f5)"; [ -n "$RESUME_EFFORT" ] || RESUME_EFFORT="medium"
  RESUME_BASE="$(tail -1 "$USAGE_TSV" | cut -f6)";   [ -n "$RESUME_BASE" ] || RESUME_BASE="unavailable"
  build_flags "$RESUME_EFFORT" resume
  read -r -d '' RESUME_PROMPT <<'RP' || true
Second and final review pass. Inspect only the corrections made for CONFIRMED
findings from the previous review. Verify that those defects are resolved and
that the corrections introduced no concrete regression. Do not repeat the
original full exploration. Remain strictly read-only. End with exactly APPROVE
or REQUEST_CHANGES.
RP
  echo "[codex-review] resume session=$SID effort=$RESUME_EFFORT (pass 2/2)"
  : > "$LAST_MSG"
  ( cd "$REPO_ROOT" && codex exec resume "$SID" "${FLAGS[@]}" "$RESUME_PROMPT" ) 2>&1 | tee "$RAW_LOG"
  CODEX_RC=${PIPESTATUS[0]}
  if [ "$CODEX_RC" -ne 0 ] && [ ! -s "$LAST_MSG" ]; then
    die "codex exec resume 啟動失敗(exit=$CODEX_RC),未產生 review;不計第二輪。"
  fi
  echo 2 > "$PASS_FILE"
  RESULT="$(log_usage "resume" "$RESUME_EFFORT" "$RESUME_BASE" 2)"
  echo; echo "[codex-review] result=$RESULT (pass 2/2)"
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
Committed range {{BASE}}..{{TIP}} (post-hoc audit of already-merged commits).

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
echo "[codex-review] mode=$MODE effort=$EFFORT range=$BASE..$TIP model=$MODEL (pass 1/2, read-only)"
: > "$LAST_MSG"
codex exec "${FLAGS[@]}" "$(cat "$TMP/prompt.txt")" 2>&1 | tee "$RAW_LOG"
CODEX_RC=${PIPESTATUS[0]}
if [ "$CODEX_RC" -ne 0 ] && [ ! -s "$LAST_MSG" ]; then
  die "codex exec 啟動失敗(exit=$CODEX_RC),未產生 review。請檢查 CLI 版本與旗標。"
fi

echo 1 > "$PASS_FILE"
RESULT="$(log_usage "$MODE" "$EFFORT" "$BASE" 1)"
echo; echo "[codex-review] result=$RESULT (pass 1/2)  usage → $USAGE_TSV"
# 限流僅在「未產生明確結論」時才視為不可信(避免 review 的 diff 內容含 'rate limit' 字面誤判)。
if is_rate_limited && [ "$RESULT" = "UNKNOWN" ]; then
  echo "[codex-review] Codex 限流且無明確結論,結果不可信,勿據此修改。" >&2; exit 4; fi
case "$RESULT" in
  APPROVE) exit 0 ;;
  REQUEST_CHANGES) exit 2 ;;
  *) echo "[codex-review] 未取得明確 APPROVE/REQUEST_CHANGES;人工檢視 $LAST_MSG" >&2; exit 5 ;;
esac
