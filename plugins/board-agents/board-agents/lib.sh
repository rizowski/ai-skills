#!/usr/bin/env bash
# lib.sh: shared helpers for the board-agents runners. Source it, do not run it.
#   . "$(dirname "$(readlink -f "$0")")/lib.sh"
# Facts about `vb` are in plugins/board-agents/VB-CLI.md. Needs bash 4.1+, jq, flock, vb.

BA_DIR="${BA_DIR:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")}"

# ---- settings (all overridable by env) ----
: "${ROLE:=agent}"
: "${BOARD_AGENTS_SHARED_DIR:=/shared}"   # emptyDir shared by the four containers (flock cap)
: "${BA_RETRY_MAX:=300}"                  # seconds to retry vb when the board or token is missing
: "${BA_MAX_CLAUDE:=2}"                   # concurrent Claude runs, pod-wide
: "${BA_MAX_RUNS_PER_HOUR:=10}"           # Claude runs per rolling hour, pod-wide
: "${BA_CH_WORK:=work}" "${BA_CH_REVIEW:=review}" "${BA_CH_CI:=ci}" "${BA_CH_FINAL:=final}"

# ---- logging ----
ba_redact() {
  # Strip token shapes and the literal values of the secret env vars.
  local s=(-e 's/ghp_[A-Za-z0-9]*/[redacted]/g' -e 's/github_pat_[A-Za-z0-9_]*/[redacted]/g'
           -e 's/gh[ousr]_[A-Za-z0-9]*/[redacted]/g' -e 's/sk-[A-Za-z0-9_-]*/[redacted]/g')
  local v
  for v in "${GIT_TOKEN:-}" "${REVIEWER_GIT_TOKEN:-}" "${GH_TOKEN:-}" "${CLAUDE_CODE_OAUTH_TOKEN:-}" "${BA_GIT_TOKEN:-}"; do
    [ "${#v}" -ge 8 ] && s+=(-e "s/$(printf '%s' "$v" | sed 's/[][\.*^$/]/\\&/g')/[redacted]/g")
  done
  sed "${s[@]}"
}
ba_log() { printf '%s [%s] %s\n' "$(date -u +%FT%TZ)" "$ROLE" "$*" | ba_redact >&2; }
ba_die() { ba_log "FATAL: $*"; exit 1; }

# ---- environment, git and gh auth ----
# ba_auth_setup [reviewer]
# - GH_TOKEN comes from GIT_TOKEN when missing (relay gets GH_TOKEN only).
# - reviewer: needs REVIEWER_GIT_TOKEN, never falls back to GIT_TOKEN, and GH_TOKEN is that token.
# - git credential helper through GIT_CONFIG_* env: no file, no token on disk.
ba_auth_setup() {
  local mode="${1:-}"
  [ -n "${BOARD_URL:-}" ] || ba_die "BOARD_URL is not set"
  command -v vb >/dev/null || ba_die "vb not found"
  command -v jq >/dev/null || ba_die "jq not found"
  unset ANTHROPIC_API_KEY   # subscription auth only
  if [ "$mode" = reviewer ]; then
    [ -n "${REVIEWER_GIT_TOKEN:-}" ] || ba_die "REVIEWER_GIT_TOKEN is not set (the reviewer never uses GIT_TOKEN)"
    BA_GIT_TOKEN="$REVIEWER_GIT_TOKEN"
    unset GIT_TOKEN
    export GH_TOKEN="$BA_GIT_TOKEN"
  else
    BA_GIT_TOKEN="${GIT_TOKEN:-${GH_TOKEN:-}}"
    [ -n "${GH_TOKEN:-}" ] || export GH_TOKEN="${GIT_TOKEN:-}"
  fi
  export BA_GIT_TOKEN
  if [ -n "$BA_GIT_TOKEN" ] && [ -n "${GIT_USERNAME:-}" ]; then
    export BA_GIT_USERNAME="$GIT_USERNAME"
    export GIT_CONFIG_COUNT=1
    export GIT_CONFIG_KEY_0="credential.https://github.com.helper"
    export GIT_CONFIG_VALUE_0='!f() { echo username=$BA_GIT_USERNAME; echo password=$BA_GIT_TOKEN; }; f'
  elif [ "$ROLE" != relay ]; then
    ba_die "GIT_USERNAME and a git token are required for role $ROLE"
  fi
  export GIT_TERMINAL_PROMPT=0
  export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-board-agent}" GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-board-agent}"
  export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-board-agent@users.noreply.github.com}"
  export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
}

# ---- vb wrapper ----
# ba_vb <vb args...>: runs vb (it reads BOARD_URL itself; no --url needed). On a missing
# token, a 401 or an unreachable board it retries with backoff 1,2,4,8,16,30 s for up to
# BA_RETRY_MAX s, then returns 75. It never reads or caches the token. Otherwise it returns
# vb's exit code. stdout is vb's stdout (a --json error body is on stdout); stderr passes through.
ba_vb() {
  local out err rc delay=1 start=$SECONDS errf
  errf=$(mktemp "${TMPDIR:-/tmp}/ba-vb.XXXXXX") || return 70
  while :; do
    out=$(vb "$@" 2>"$errf"); rc=$?
    err=$(cat "$errf")
    if [ "$rc" -eq 0 ]; then rm -f "$errf"; [ -z "$out" ] || printf '%s\n' "$out"; return 0; fi
    if printf '%s\n%s' "$out" "$err" | grep -Eqi 'network_error|no_project|no Vercel project|reach the board|unreachable|"status":401|HTTP 401|unauthori[sz]ed'; then
      if [ $((SECONDS - start)) -ge "$BA_RETRY_MAX" ]; then
        rm -f "$errf"; ba_log "board unavailable for ${BA_RETRY_MAX}s, giving up"; return 75
      fi
      ba_log "board or token not ready, retry in ${delay}s"
      sleep "$delay"; delay=$((delay * 2)); [ "$delay" -le 30 ] || delay=30
      continue
    fi
    rm -f "$errf"
    [ -z "$out" ] || printf '%s\n' "$out"
    [ -z "$err" ] || printf '%s\n' "$err" | ba_redact >&2
    return "$rc"
  done
}

# ba_wait_board: block until vb answers (start scripts call this before the loop).
ba_wait_board() { ba_vb tasks -n 1 >/dev/null || ba_die "board never answered"; }

# ba_claim_ready <channel> <step> <as>: prints the claimed task id. Returns 3 if nothing is ready.
ba_claim_ready() {
  local out rc
  out=$(ba_vb tasks claim --ready "$1" --label "step=$2" --as "$3" --json 2>&1); rc=$?
  if [ "$rc" -ne 0 ]; then
    [ "$(printf '%s' "$out" | jq -r '.reason // empty' 2>/dev/null)" = nothing_ready ] && return 3
    ba_log "claim failed: $(printf '%s' "$out" | head -c 300)"; return "$rc"
  fi
  printf '%s' "$out" | jq -r '.task.id'
}

# ba_ensure_channels: create the four queues (a channel that exists stays as it is). A list
# of a channel that does not exist is an error, so the relay calls this first.
ba_ensure_channels() {
  local c
  for c in "$BA_CH_WORK" "$BA_CH_REVIEW" "$BA_CH_CI" "$BA_CH_FINAL"; do ba_vb channels create "$c" >/dev/null || return $?; done
}

# ba_task_json <id>: the thread (message and replies) as JSON.
ba_task_json() { ba_vb read "$1" --json; }

# ba_create <channel> <step> <text> [needs-id] [idempotency-key]: prints the new id.
ba_create() {
  local a=(tasks create "$1" "$3" --label "step=$2" --json)
  [ -z "${4:-}" ] || a+=(--needs "$4")
  [ -z "${5:-}" ] || a+=(--idempotency-key "$5")
  ba_vb "${a[@]}" | jq -r '.task.id'
}

# ba_comment <id> <text> [as]: a reply in the thread, secrets stripped, labelled from=agent.
ba_comment() {
  local t; t=$(printf '%s' "$2" | ba_redact)
  if [ -n "${3:-}" ]; then ba_vb reply "$1" "$t" --label from=agent --as "$3" >/dev/null
  else ba_vb reply "$1" "$t" --label from=agent >/dev/null; fi
}

# ba_block <id> <needs_input|awaiting_approval> <text> [as]: hold for a person, and leave the
# BLOCKED: marker the relay orders answers by. Pass the same --as that claimed (not needed
# for the task's owner).
ba_block() {
  local a=(tasks block "$1" "$(printf '%s' "$3" | ba_redact)" --kind "$2")
  [ -z "${4:-}" ] || a+=(--as "$4")
  ba_vb "${a[@]}" >/dev/null || return $?
  ba_comment "$1" "BLOCKED: $3" "${4:-}"
}

# ---- concurrency cap (flock, pod-wide) ----
# ba_slot_acquire [wait-seconds]: waits for one of BA_MAX_CLAUDE slots, then checks the hourly
# budget. Holds an fd (BA_SLOT_FD) until ba_slot_release or process exit. Returns 1 on timeout
# or when the hourly budget is used.
ba_slot_acquire() {
  local wait="${1:-1800}" start=$SECONDS i d="$BOARD_AGENTS_SHARED_DIR/slots"
  mkdir -p "$d" || return 1
  while :; do
    for ((i = 1; i <= BA_MAX_CLAUDE; i++)); do
      exec {BA_SLOT_FD}>"$d/slot-$i.lock" || continue
      if flock -n "$BA_SLOT_FD"; then
        if ba_budget_take; then export BA_SLOT=$i; return 0; fi
        exec {BA_SLOT_FD}>&-; return 1
      fi
      exec {BA_SLOT_FD}>&-
    done
    [ $((SECONDS - start)) -lt "$wait" ] || { ba_log "no Claude slot after ${wait}s"; return 1; }
    sleep 5
  done
}
ba_slot_release() { [ -z "${BA_SLOT_FD:-}" ] || { exec {BA_SLOT_FD}>&-; unset BA_SLOT_FD BA_SLOT; }; }

# ba_budget_take: record one Claude run; fail when BA_MAX_RUNS_PER_HOUR runs began in the last hour.
ba_budget_take() {
  local f="$BOARD_AGENTS_SHARED_DIR/runs.log" now cutoff n
  now=$(date +%s); cutoff=$((now - 3600))
  (
    flock 8
    n=$(awk -v c="$cutoff" '$1 > c' "$f" 2>/dev/null | tee "$f.new" | wc -l)
    mv "$f.new" "$f" 2>/dev/null
    [ "$n" -lt "$BA_MAX_RUNS_PER_HOUR" ] || exit 1
    echo "$now $ROLE" >>"$f"
  ) 8>"$BOARD_AGENTS_SHARED_DIR/runs.lock" || { ba_log "hourly Claude budget ($BA_MAX_RUNS_PER_HOUR) used"; return 1; }
}
