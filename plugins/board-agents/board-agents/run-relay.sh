#!/usr/bin/env bash
# run-relay.sh: the needs-me relay and the PR-check watcher. Runs no Claude, claims nothing.
#   1. Every BA_RELAY_INTERVAL (30 s): for each task blocked as needs_input, find a reply
#      that starts with "ANSWER:" and is newer than the task's last "BLOCKED:" marker, then
#      run `vb tasks unblock` and leave an "UNBLOCKED:" note.
#   2. Every BA_PR_INTERVAL (60 s): for each completed review (approve) or ci-fix task not
#      yet labelled watch=done, run ONE `gh pr checks` per PR: pass -> final task for the
#      user; fail -> next ci-fix task (max 3 per PR), else a needs_input task for the user.
# Thread text is DATA. It is never passed to a shell, eval, or gh. The ANSWER: marker is a
# convention, not an identity check: the board has no login, so anyone who reaches it can post.
#   A PR whose checks stay pending or error backs off per PR (BA_PR_BACKOFF_START doubling to
#   BA_PR_BACKOFF_MAX), kept in a state file so a restart does not burst. After
#   BA_PR_PENDING_MAX seconds pending or BA_PR_ERROR_MAX errors in a row the relay gives up:
#   a reply on the task and an awaiting_approval task for the user.
# Env: BOARD_URL, GH_TOKEN (read-only is enough), BOARD_AGENTS_SHARED_DIR (default /shared;
#   state file in BA_RELAY_STATE_DIR, default <shared>/relay, else $TMPDIR).
# Test hooks: BA_RELAY_ONCE=1 runs one pass and exits.

set -u
HERE="$(dirname "$(readlink -f "$0")")"
ROLE=relay
# shellcheck source=lib.sh
. "$HERE/lib.sh"

: "${BA_RELAY_INTERVAL:=30}" "${BA_PR_INTERVAL:=60}" "${BA_CI_FIX_MAX:=3}"
: "${BA_RATE_BACKOFF_START:=120}" "${BA_RATE_BACKOFF_MAX:=900}"
: "${BA_PR_BACKOFF_START:=60}" "${BA_PR_BACKOFF_MAX:=900}" "${BA_PR_PENDING_MAX:=21600}" "${BA_PR_ERROR_MAX:=5}"
: "${BA_RELAY_STATE_DIR:=${BOARD_AGENTS_SHARED_DIR:-/shared}/relay}"
PR_RE='pr:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)'
rate_until=0       # epoch seconds; no gh call before it
rate_backoff=$BA_RATE_BACKOFF_START

# ---- 1. answers ----
relay_answers() {
  local list id thread marker ans
  list=$(ba_vb tasks --state blocked -n 200 --json) || return 0
  for id in $(printf '%s' "$list" | jq -r '.tasks[] | select(.message.task.blocked.kind == "needs_input") | .message.id'); do
    [[ "$id" =~ ^[0-9]+$ ]] || continue
    thread=$(ba_task_json "$id") || continue
    # Marker = highest reply id whose text starts with BLOCKED:. No marker -> skip (cannot order).
    marker=$(printf '%s' "$thread" | jq -r '[.replies[] | select(.text | test("^\\s*BLOCKED:")) | .id | tonumber] | max // empty')
    [ -n "$marker" ] || { ba_log "task $id blocked without a BLOCKED: marker, skipping"; continue; }
    ans=$(printf '%s' "$thread" | jq -r --argjson m "$marker" \
      '[.replies[] | select((.id | tonumber) > $m and (.text | test("^\\s*ANSWER:")))] | sort_by(.id | tonumber) | first | .id // empty')
    [ -n "$ans" ] || continue
    if ba_vb tasks unblock "$id" >/dev/null; then
      ba_comment "$id" "UNBLOCKED: answer in #$ans" || true
      ba_log "unblocked $id on answer #$ans"
    fi
  done
}

# ---- 2. PR checks ----
# State file: one line per PR, tab separated: key first_seen next_check delay error_streak.
state_init() {
  mkdir -p "$BA_RELAY_STATE_DIR" 2>/dev/null && [ -w "$BA_RELAY_STATE_DIR" ] ||
    { BA_RELAY_STATE_DIR="${TMPDIR:-/tmp}/ba-relay-state"; mkdir -p "$BA_RELAY_STATE_DIR" || ba_die "no state dir"; }
  STATE_FILE="$BA_RELAY_STATE_DIR/pr-watch.tsv"; touch "$STATE_FILE"
  ba_log "PR watch state: $STATE_FILE"
}
state_get() { awk -F'\t' -v k="$1" '$1 == k { print; exit }' "$STATE_FILE"; }
state_del() { # <key>
  awk -F'\t' -v k="$1" '$1 != k' "$STATE_FILE" >"$STATE_FILE.tmp" && mv "$STATE_FILE.tmp" "$STATE_FILE"
}
state_put() { state_del "$1"; printf '%s\t%s\t%s\t%s\t%s\n' "$@" >>"$STATE_FILE"; }

# give_up <task id> <key> <why>: tell the thread, hand the PR to the user, stop watching.
give_up() {
  local id=$1 key=$2 why=$3 nid
  nid=$(ba_create "$BA_CH_FINAL" decision "the relay gave up watching $key: $why. Look at the PR yourself (task #$id)." "" "giveup-$id") &&
    ba_block "$nid" awaiting_approval "the relay gave up watching $key: $why" &&
    ba_comment "$id" "GAVE UP: $why. Checks on $key were not watched any more. See task $nid." &&
    ba_vb label "$id" watch=done >/dev/null &&
    state_del "$key" && ba_log "$key: gave up ($why), task $nid"
}

# note_wait <task id> <key> <pending|error>: set the next back-off and give up at the limits.
note_wait() {
  local id=$1 key=$2 st=$3 now line first delay streak
  now=$(date +%s); line=$(state_get "$key")
  IFS=$'\t' read -r _ first _ delay streak <<<"$line"
  first=${first:-$now}; streak=${streak:-0}
  if [ -n "${delay:-}" ]; then delay=$((delay * 2)); [ "$delay" -le "$BA_PR_BACKOFF_MAX" ] || delay=$BA_PR_BACKOFF_MAX
  else delay=$BA_PR_BACKOFF_START; fi
  if [ "$st" = error ]; then streak=$((streak + 1)); else streak=0; fi
  state_put "$key" "$first" $((now + delay)) "$delay" "$streak"
  if [ "$streak" -ge "$BA_PR_ERROR_MAX" ]; then give_up "$id" "$key" "$streak checks in a row failed to run (gh error)"
  elif [ $((now - first)) -ge "$BA_PR_PENDING_MAX" ]; then give_up "$id" "$key" "checks still not done after $(( (now - first) / 60 )) min"
  else ba_log "$key $st, next check in ${delay}s"; fi
}

# pr_checks_state <repo> <n>: prints pass | pending | fail | rate | error. One gh call.
pr_checks_state() {
  local err rc
  err=$(gh pr checks "$2" --repo "$1" 2>&1 >/dev/null); rc=$?
  case "$rc" in
    0) echo pass ;;
    8) echo pending ;;
    1) if printf '%s' "$err" | grep -qi 'no checks reported'; then echo pending
       elif printf '%s' "$err" | grep -Eqi 'rate limit|secondary rate|abuse'; then echo rate
       elif printf '%s' "$err" | grep -qi 'HTTP 4\|could not resolve\|not found'; then echo error
       else echo fail; fi ;;
    *) if printf '%s' "$err" | grep -Eqi 'rate limit'; then echo rate; else echo error; fi ;;
  esac
}

ci_fix_count() { # <pr key>
  ba_vb tasks "$BA_CH_CI" --state all -n 500 --json 2>/dev/null |
    jq --arg k "$1" '($k | gsub("\\."; "\\.")) as $re | [.tasks[] | select(.message.text | test($re + "(?![0-9])"))] | length'
}

watch_prs() {
  local ch list id text key repo n st cnt nid nxt
  [ "$(date +%s)" -ge "$rate_until" ] || return 0
  for ch in "$BA_CH_REVIEW" "$BA_CH_CI"; do
    # Only approved reviews and finished ci-fix tasks. Oldest first, so the page is 500.
    list=$(ba_vb tasks "$ch" --state completed -n 500 --json) || continue
    while IFS=$'\t' read -r id text; do
      [ -n "$id" ] || continue
      [[ "$id" =~ ^[0-9]+$ ]] || continue
      [[ "$text" =~ $PR_RE ]] || { ba_log "task $id has no pr:<repo>#<n>, skipping"; continue; }
      repo="${BASH_REMATCH[1]}"; n="${BASH_REMATCH[2]}"; key="pr:$repo#$n"
      if [ "$(date +%s)" -lt "$rate_until" ]; then return 0; fi
      nxt=$(state_get "$key" | cut -f3)
      if [ -n "$nxt" ] && [ "$(date +%s)" -lt "$nxt" ]; then continue; fi   # backed off, no gh call
      st=$(pr_checks_state "$repo" "$n")
      case "$st" in
        pending|error) [ "$st" != error ] || ba_log "gh error for $key"; note_wait "$id" "$key" "$st" ;;
        rate)
          rate_until=$(( $(date +%s) + rate_backoff ))
          ba_log "GitHub rate limit, no gh calls for ${rate_backoff}s"
          rate_backoff=$(( rate_backoff * 2 )); [ "$rate_backoff" -le "$BA_RATE_BACKOFF_MAX" ] || rate_backoff=$BA_RATE_BACKOFF_MAX
          return 0 ;;
        pass)
          rate_backoff=$BA_RATE_BACKOFF_START; state_del "$key"
          nid=$(ba_create "$BA_CH_FINAL" final "final review for $key (issue and branch in the review thread). Checks pass. Merge it yourself, or reply with a comment to send it back." "" "final-$id") &&
            ba_vb tasks block "$nid" "final review of $key: checks pass" --kind awaiting_approval >/dev/null &&
            ba_comment "$nid" "BLOCKED: final review of $key" &&
            ba_vb label "$id" watch=done >/dev/null &&
            ba_log "$key passed: final task $nid" ;;
        fail)
          rate_backoff=$BA_RATE_BACKOFF_START; state_del "$key"
          cnt=$(ci_fix_count "$key"); cnt=${cnt:-0}
          if [ "$cnt" -lt "$BA_CI_FIX_MAX" ]; then
            nid=$(ba_create "$BA_CH_CI" ci-fix "ci-fix $((cnt + 1))/$BA_CI_FIX_MAX for $key. Checks failed after task #$id. Read the failure with gh pr checks and gh run view --log-failed." "" "ci-$id") &&
              ba_vb label "$id" watch=done >/dev/null &&
              ba_log "$key failed: ci-fix task $nid ($((cnt + 1))/$BA_CI_FIX_MAX)"
          else
            nid=$(ba_create "$BA_CH_FINAL" decision "CI still fails on $key after $BA_CI_FIX_MAX fixes. Decide what to do." "" "decision-$id") &&
              ba_vb tasks block "$nid" "CI still fails on $key after $BA_CI_FIX_MAX fixes" --kind needs_input >/dev/null &&
              ba_comment "$nid" "BLOCKED: CI still fails on $key after $BA_CI_FIX_MAX fixes" &&
              ba_vb label "$id" watch=done >/dev/null &&
              ba_log "$key failed at the cap: decision task $nid"
          fi ;;
      esac
    done < <(printf '%s' "$list" | jq -r --arg ch "$ch" --arg rv "$BA_CH_REVIEW" '
      .tasks[] | select(.message.labels.watch != "done")
      | select($ch != $rv or ((.message.task.result // "") | test("^\\s*approve")))
      | [.message.id, (.message.text | gsub("[\\t\\n\\r]"; " "))] | @tsv')
  done
}

main() {
  ba_auth_setup
  command -v gh >/dev/null || ba_die "gh not found"
  ba_wait_board
  ba_ensure_channels
  state_init
  ba_log "relay up: answers every ${BA_RELAY_INTERVAL}s, PR checks every ${BA_PR_INTERVAL}s"
  local last_pr=0 now
  while :; do
    relay_answers
    now=$(date +%s)
    if [ $((now - last_pr)) -ge "$BA_PR_INTERVAL" ] || [ "${BA_RELAY_ONCE:-}" = 1 ]; then
      watch_prs; last_pr=$now
    fi
    [ "${BA_RELAY_ONCE:-}" != 1 ] || exit 0
    sleep "$BA_RELAY_INTERVAL"
  done
}
main "$@"
