#!/usr/bin/env bash
# run-worker.sh: claim work tasks, run Claude on a fresh checkout, push the board/* branch,
# open the PR, create the review task (waits on this one), complete with the PR URL.
# Task text carries: repo:<owner/repo>  [branch:board/..]  [issue:<owner/repo>#<n>]  [round N/3].
# Claude commits only; this script pushes and calls gh (about 2 gh calls per task).
# Env: BOARD_URL, CLAUDE_CODE_OAUTH_TOKEN, GIT_USERNAME, GIT_TOKEN. Test hook: BA_ONCE=1.

set -u
HERE="$(dirname "$(readlink -f "$0")")"
ROLE=worker
BA_NAME="${BA_NAME:-worker-1}"
# shellcheck source=lib.sh
. "$HERE/lib.sh"
# shellcheck source=lib-run.sh
. "$HERE/lib-run.sh"
: "${BA_WORK_TIMEOUT:=2700}" "${BA_WORK_TURNS:=80}"

ALLOWED=(Read Edit Write Glob Grep 'Bash(git add:*)' 'Bash(git commit:*)' 'Bash(git status:*)' 'Bash(git diff:*)'
  'Bash(git log:*)' 'Bash(git show:*)' 'Bash(git restore:*)' 'Bash(git rm:*)' 'Bash(git mv:*)'
  'Bash(pnpm:*)' 'Bash(npm:*)' 'Bash(npx:*)' 'Bash(node:*)' 'Bash(corepack:*)' 'Bash(ls:*)' 'Bash(cat:*)'
  'Bash(mkdir:*)' 'Bash(jq:*)' 'Bash(bash:*)')
DENIED=('Bash(git push:*)' 'Bash(gh:*)' 'Bash(vb:*)' 'Bash(curl:*)' 'Bash(wget:*)' 'Bash(ssh:*)' 'Bash(git reset:*)' 'Bash(git rebase:*)')

work_job() { # <id> <as>
  local id=$1 as=$2 text repo branch round issue dir out in last base ahead url title body n key
  text=$(ba_task_text "$id") || { ba_fail "$id" "$as" "cannot read task"; return 1; }
  repo=$(ba_text_field "$text" repo)
  if [ -z "$repo" ] && ba_parse_pr "$text"; then repo=$T_REPO; fi
  [[ "$repo" =~ $BA_REPO_RE ]] || { ba_fail "$id" "$as" "task text needs repo:<owner/repo>"; return 1; }
  branch=$(ba_text_field "$text" branch); : "${branch:=board/$id}"
  [[ "$branch" =~ $BA_BRANCH_RE ]] || { ba_fail "$id" "$as" "branch must match board/*"; return 1; }
  round=$(printf '%s' "$text" | grep -Eo 'round [0-9]+/' | head -n1 | grep -Eo '[0-9]+'); : "${round:=1}"
  issue=$(ba_text_field "$text" issue); [[ "$issue" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+$ ]] || issue=""
  dir="$BA_WORK_ROOT/worker/$id"; BA_CUR_CHECKOUT=$dir
  out="$BA_RUN_DIR/claude.out"; in="$BA_RUN_DIR/input.txt"

  ba_clone "$repo" "$dir" || { ba_fail "$id" "$as" "clone of $repo failed"; return 1; }
  base=$(ba_default_branch "$dir")
  [ "$branch" != "$base" ] || { ba_fail "$id" "$as" "refusing to work on the default branch"; return 1; }
  # Resume from the pushed branch when it exists (after unblock, or rework), else start from the default branch.
  if git -C "$dir" fetch --quiet origin "$branch" 2>/dev/null; then git -C "$dir" checkout --quiet -B "$branch" FETCH_HEAD
  else git -C "$dir" checkout --quiet -b "$branch"; fi
  ba_input_file "$in" "Task #$id for repo $repo, branch $branch" "$id"

  ba_claude "$dir" "$out" "$BA_WORK_TIMEOUT" "$BA_WORK_TURNS" "$in" "$HERE/prompts/common.md" "$HERE/prompts/worker.md" \
    -- "${ALLOWED[@]}" -- "${DENIED[@]}"
  local rc=$?
  if ba_lost || [ "$rc" -eq 143 ]; then ba_log "claim lost on $id: dropping checkout, no push"; return 0; fi
  [ "$rc" -eq 0 ] || { ba_fail "$id" "$as" "claude exited $rc$([ "$rc" -eq 124 ] && echo ' (timeout)')" "$out"; return 1; }

  last=$(ba_last_line "$out")
  base_ref="origin/$base"
  ahead=$(git -C "$dir" rev-list --count "$base_ref..HEAD" 2>/dev/null || echo 0)
  case "$last" in
    NEEDS_INPUT:*)
      [ "$ahead" -eq 0 ] || ba_push "$dir" "$branch" || ba_log "WIP push failed"
      ba_lost || ba_ask "$id" "$as" "$last"; return 0 ;;
    'RESULT: done') ;;
    *) ba_fail "$id" "$as" "no RESULT line (last: ${last:0:80})" "$out"; return 1 ;;
  esac
  [ "$ahead" -gt 0 ] || { ba_fail "$id" "$as" "claude made no commits"; return 1; }
  [ -z "$(git -C "$dir" status --porcelain)" ] || { ba_fail "$id" "$as" "uncommitted changes left in the checkout"; return 1; }
  if ba_lost; then ba_log "claim lost on $id before push"; return 0; fi
  ba_push "$dir" "$branch" || { ba_fail "$id" "$as" "push of $branch failed"; return 1; }

  url=$(gh pr list --repo "$repo" --head "$branch" --state open --json url -q '.[0].url // empty' 2>/dev/null)
  if [ -z "$url" ]; then
    title=$(head -n1 "$dir/.board-pr.md" 2>/dev/null | cut -c1-200); : "${title:=board task #$id}"
    body=$({ tail -n +2 "$dir/.board-pr.md" 2>/dev/null; [ -z "$issue" ] || printf '\n\nRefs %s\n' "$issue"; printf '\nBoard task #%s.\n' "$id"; } | ba_redact)
    url=$(gh pr create --repo "$repo" --base "$base" --head "$branch" --title "$(printf '%s' "$title" | ba_redact)" --body "$body" 2>&1 | tail -n1)
  fi
  n=${url##*/}
  [[ "$url" =~ ^https://github.com/.+/pull/[0-9]+$ ]] || { ba_fail "$id" "$as" "gh pr create failed: ${url:0:150}"; return 1; }
  key="pr:$repo#$n"
  ba_create "$BA_CH_REVIEW" review "review $key repo:$repo branch:$branch round:$round${issue:+ issue:$issue} (work task #$id)" "$id" "review-$id" >/dev/null ||
    { ba_fail "$id" "$as" "could not create the review task"; return 1; }
  ba_complete "$id" "$as" "$url" || { ba_log "complete of $id failed (claim lost?): dropping checkout"; return 0; }
  ba_log "task $id done: $url"
}

trap 'ba_cleanup; exit 143' TERM INT
ba_auth_setup
ba_runner_loop "$BA_CH_WORK" work work_job
