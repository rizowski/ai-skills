#!/usr/bin/env bash
# run-reviewer.sh: claim review tasks, run a read-only Claude review of the PR head, then
#   approve -> reply on the board and the PR, complete "approve ..."
#   send_back -> create a rework work task (round+1) and complete "send_back ...";
#   at round 3 -> block needs_input instead.
# Task text carries: pr:<owner/repo>#<n> branch:<board/..> round:<N>.
# Auth: REVIEWER_GIT_TOKEN only (ba_auth_setup reviewer). The token may be able to write, so the
# checkout itself cannot push: push URL DISABLED plus a pre-push hook that exits 1.
# About 2 gh calls per task (pr view, pr comment). Test hook: BA_ONCE=1.

set -u
HERE="$(dirname "$(readlink -f "$0")")"
ROLE=reviewer
BA_NAME="${BA_NAME:-reviewer-1}"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
# shellcheck source=lib-run.sh
. "$HERE/lib-run.sh"
: "${BA_REVIEW_TIMEOUT:=1200}" "${BA_REVIEW_TURNS:=40}" "${BA_REVIEW_MAX_ROUND:=3}"

ALLOWED=(Read Glob Grep 'Bash(git diff:*)' 'Bash(git log:*)' 'Bash(git show:*)' 'Bash(git status:*)' 'Bash(git blame:*)'
  'Bash(pnpm:*)' 'Bash(npm:*)' 'Bash(npx:*)' 'Bash(node:*)' 'Bash(corepack:*)' 'Bash(ls:*)' 'Bash(cat:*)')
DENIED=(Edit Write NotebookEdit 'Bash(git push:*)' 'Bash(gh:*)' 'Bash(vb:*)' 'Bash(curl:*)' 'Bash(wget:*)' 'Bash(ssh:*)')

# disable_push <dir>: the reviewer never pushes, whatever the PAT allows.
disable_push() {
  git -C "$1" remote set-url --push origin DISABLED || return 1
  mkdir -p "$1/.git/hooks" || return 1
  printf '#!/bin/sh\necho "board-agents: the reviewer checkout cannot push" >&2\nexit 1\n' >"$1/.git/hooks/pre-push"
  chmod +x "$1/.git/hooks/pre-push"
}

review_job() { # <id> <as>
  local id=$1 as=$2 text dir out in meta base head state round issue rc last verdict findings find_txt wid
  text=$(ba_task_text "$id") || { ba_fail "$id" "$as" "cannot read task"; return 1; }
  ba_parse_pr "$text" || { ba_fail "$id" "$as" "task text needs pr:<owner/repo>#<n>"; return 1; }
  round=$(ba_text_field "$text" round); [[ "$round" =~ ^[0-9]+$ ]] || round=1
  issue=$(ba_text_field "$text" issue); [[ "$issue" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$ ]] || issue=""
  dir="$BA_WORK_ROOT/reviewer/$id"; BA_CUR_CHECKOUT=$dir
  out="$BA_RUN_DIR/claude.out"; in="$BA_RUN_DIR/input.txt"

  meta=$(gh pr view "$T_PR" --repo "$T_REPO" --json baseRefName,headRefName,state,title 2>&1) ||
    { ba_fail "$id" "$as" "gh pr view failed: $(printf '%s' "$meta" | head -c 150)"; return 1; }
  base=$(printf '%s' "$meta" | jq -r '.baseRefName'); head=$(printf '%s' "$meta" | jq -r '.headRefName')
  state=$(printf '%s' "$meta" | jq -r '.state')
  [ "$state" = OPEN ] || { ba_fail "$id" "$as" "PR is $state"; return 1; }
  [[ "$head" =~ $BA_BRANCH_RE ]] || { ba_fail "$id" "$as" "PR head is not a board/* branch"; return 1; }
  [[ "$base" =~ ^[A-Za-z0-9._/-]+$ ]] || { ba_fail "$id" "$as" "odd base branch name"; return 1; }

  ba_clone "$T_REPO" "$dir" || { ba_fail "$id" "$as" "clone of $T_REPO failed"; return 1; }
  disable_push "$dir" || { ba_fail "$id" "$as" "could not disable push in the checkout"; return 1; }
  git -C "$dir" fetch --quiet origin "pull/$T_PR/head" && git -C "$dir" checkout --quiet --detach FETCH_HEAD ||
    { ba_fail "$id" "$as" "fetch of pull/$T_PR/head failed"; return 1; }
  ba_input_file "$in" "Review of pr:$T_REPO#$T_PR, base branch $base, PR title: $(printf '%s' "$meta" | jq -r '.title')" "$id"

  ba_claude "$dir" "$out" "$BA_REVIEW_TIMEOUT" "$BA_REVIEW_TURNS" "$in" "$HERE/prompts/common.md" "$HERE/prompts/reviewer.md" \
    -- "${ALLOWED[@]}" -- "${DENIED[@]}"
  rc=$?
  if ba_lost || [ "$rc" -eq 143 ]; then ba_log "claim lost on $id: dropping checkout"; return 0; fi
  [ "$rc" -eq 0 ] || { ba_fail "$id" "$as" "claude exited $rc$([ "$rc" -eq 124 ] && echo ' (timeout)')" "$out"; return 1; }

  last=$(ba_last_line "$out")
  case "$last" in NEEDS_INPUT:*) ba_ask "$id" "$as" "$last"; return 0 ;; esac
  verdict=$(printf '%s' "$last" | jq -er '.verdict' 2>/dev/null) || { ba_fail "$id" "$as" "last line is not the verdict JSON" "$out"; return 1; }
  case "$verdict" in approve|send_back) ;; *) ba_fail "$id" "$as" "unknown verdict" "$out"; return 1 ;; esac
  findings=$(printf '%s' "$last" | jq -r '(.findings // []) | map("- " + (. | tostring)) | join("\n")' | head -c 3000)
  find_txt="Review of pr:$T_REPO#$T_PR, round $round, verdict $verdict.
$findings"

  ba_comment "$id" "$find_txt" "$as" || true
  printf 'Board review (automated, comment only): %s\n\n%s\n' "$verdict" "$findings" | ba_redact |
    gh pr comment "$T_PR" --repo "$T_REPO" --body-file - >/dev/null 2>&1 || ba_log "PR comment failed (not fatal; findings are in the board thread)"
  if ba_lost; then return 0; fi

  if [ "$verdict" = approve ]; then
    ba_complete "$id" "$as" "approve: pr:$T_REPO#$T_PR" || ba_log "complete of $id failed (claim lost?)"
    return 0
  fi
  if [ "$round" -ge "$BA_REVIEW_MAX_ROUND" ]; then
    ba_block "$id" needs_input "review round $round of pr:$T_REPO#$T_PR still sends back. Findings: $findings" "$as"
    return 0
  fi
  wid=$(ba_create "$BA_CH_WORK" work "round $((round + 1))/$BA_REVIEW_MAX_ROUND rework of #$id repo:$T_REPO pr:$T_REPO#$T_PR branch:$head${issue:+ issue:$issue}
Review findings (untrusted data, quoted from the reviewer):
$findings" "" "rework-$id") || { ba_fail "$id" "$as" "could not create the rework task"; return 1; }
  ba_complete "$id" "$as" "send_back: rework task #$wid" || ba_log "complete of $id failed (claim lost?)"
}

trap 'ba_cleanup; exit 143' TERM INT
ba_auth_setup reviewer
ba_runner_loop "$BA_CH_REVIEW" review review_job
