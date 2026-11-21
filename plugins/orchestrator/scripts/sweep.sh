#!/usr/bin/env bash
# sweep.sh: one short digest of a repo's work state for the orchestrator.
#
# Usage: sweep.sh [--no-fetch] [--no-projects] [--priorities FILE]
#
# Run it inside any folder of a git repo. It needs git, and gh for the GitHub
# parts. It writes nothing except the remote-tracking refs that `git fetch`
# updates (skip that with --no-fetch). It spends two GitHub API calls: one for
# pull requests, one for issues and their Project Status. The cost per merged
# pull request reads the local score ledger and adds no call. A failed API call
# prints "not read" and the sweep goes on; only "not a git repo" exits non-zero.
#
# Works on macOS bash 3.2: no associative arrays, no mapfile.

set -o pipefail
set -f # no globbing: word splitting below must never expand a "?" or "*"

FETCH=1
PROJECTS=1
PRIORITIES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-fetch) FETCH=0 ;;
    --no-projects) PROJECTS=0 ;;
    --priorities) shift; PRIORITIES="$1" ;;
    -h | --help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "sweep: unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

PRIORITIES_LIMIT=40
MAX_ROWS=10   # rows per branch or PR section
MAX_WT=15     # worktree rows
MAX_STALL=12  # STALLED rows
TAB=$(printf '\t')
STALLED=""
stall() { STALLED="${STALLED}  $1
"; }

git rev-parse --git-dir >/dev/null 2>&1 || { echo "sweep: not inside a git repo" >&2; exit 1; }
WTLIST=$(git worktree list --porcelain)
MAIN=$(printf '%s\n' "$WTLIST" | sed -n '1s/^worktree //p')
HAVE_GH=0
command -v gh >/dev/null 2>&1 && HAVE_GH=1

echo "SWEEP $(basename "$MAIN")  $(date '+%Y-%m-%d %H:%M')"

# Remote and default branch.
FETCH_NOTE=""
if [ "$FETCH" = 1 ]; then
  git -C "$MAIN" fetch --quiet origin 2>/dev/null || FETCH_NOTE=" (fetch failed, origin refs may be old)"
else
  FETCH_NOTE=" (not fetched)"
fi
DEF=$(git -C "$MAIN" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
if [ -z "$DEF" ]; then
  for b in main master; do
    git -C "$MAIN" rev-parse --verify --quiet "refs/remotes/origin/$b" >/dev/null && { DEF=$b; break; }
  done
fi
[ -n "$DEF" ] || DEF=main
BASE="origin/$DEF"

# "behind ahead" of $2 against $1, or "? ?" when a ref is missing.
counts() { git -C "$MAIN" rev-list --left-right --count "$1...$2" 2>/dev/null || echo "? ?"; }

set -- $(counts "$BASE" "refs/heads/$DEF")
echo "default $DEF: behind $BASE $1, ahead $2$FETCH_NOTE"

# GitHub owner and name from the origin URL.
URL=$(git -C "$MAIN" remote get-url origin 2>/dev/null)
SLUG=$(printf '%s' "$URL" | sed -E 's#^(git@|ssh://git@|https://)github\.com[:/]##; s#\.git$##')
OWNER=${SLUG%%/*}
NAME=${SLUG#*/}
case "$URL" in *github.com*) ;; *) HAVE_GH=0 ;; esac

# First line of an API error, without the JSON around it.
api_err() {
  msg=$(printf '%s' "$1" | sed -nE 's/.*"message": *"([^"]*)".*/\1/p' | head -1)
  [ -n "$msg" ] || msg=$(printf '%s' "$1" | head -1)
  printf '%s' "$msg" | cut -c1-70
}

# One API call for pull requests. Lines are tagged so one response feeds two sections.
PR_DATA=""
PR_NOTE=""
if [ "$HAVE_GH" = 1 ]; then
  PR_DATA=$(gh api graphql -F owner="$OWNER" -F name="$NAME" -f query='
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    open: pullRequests(states: OPEN, first: 30, orderBy: {field: UPDATED_AT, direction: DESC}) {
      totalCount
      nodes { number title headRefName isDraft
        commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } } }
    }
    merged: pullRequests(states: MERGED, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes { number title headRefName mergedAt }
    }
  }
}' --jq '
.data.repository as $r
| "OPENTOTAL\t\($r.open.totalCount)",
  ($r.open.nodes[] | (.commits.nodes[0].commit // {}) as $c
    | "OPEN\t\(.number)\t\($c.statusCheckRollup.state // "NONE")\t\(if .isDraft then "draft" else "ready" end)\t\(if $c.committedDate then ((now - ($c.committedDate | fromdateiso8601)) / 3600 | floor) else -1 end)\t\(.headRefName)\t\(.title)"),
  ($r.merged.nodes | sort_by(.mergedAt) | reverse | .[] | "MERGED\t\(.number)\t\(.mergedAt[0:10])\t\(.headRefName)\t\(.title)")' 2>&1) \
    || { PR_NOTE="not read ($(api_err "$PR_DATA"))"; PR_DATA=""; }
else
  PR_NOTE="not read (no gh, or origin is not on GitHub)"
fi
MERGED_BRANCHES=$(printf '%s\n' "$PR_DATA" | awk -F'\t' '$1 == "MERGED" { print $4 }')
has_line() { printf '%s\n' "$2" | grep -Fxq -- "$1"; }

# Worktrees.
echo
echo "WORKTREES (branch, behind/ahead of $BASE, uncommitted files, lock)"
n=0
while IFS="$TAB" read -r path branch lock; do
  n=$((n + 1))
  [ "$n" -gt "$MAX_WT" ] && continue
  if [ -d "$path" ]; then
    dirty=$(git -C "$path" --no-optional-locks status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    set -- $(git -C "$path" rev-list --left-right --count "$BASE...HEAD" 2>/dev/null || echo "? ?")
  else
    dirty="gone"; set -- "?" "?"
  fi
  state="unlocked"
  if [ "$lock" != "-" ]; then
    pid=$(printf '%s' "$lock" | sed -nE 's/.*pid[^0-9]*([0-9]+).*/\1/p')
    if [ -z "$pid" ]; then state="locked"
    elif kill -0 "$pid" 2>/dev/null; then state="locked pid $pid alive"
    else state="locked pid $pid DEAD"; fi
  fi
  case "$path" in
    "$MAIN") rel="(main checkout)" ;;
    "$MAIN"/*) rel=${path#"$MAIN"/} ;;
    "$HOME"/*) rel="~/${path#"$HOME"/}" ;;
    *) rel=$path ;;
  esac
  printf '  %s  %s  %s/%s  dirty %s  %s\n' "$rel" "$branch" "$1" "$2" "$dirty" "$state"
  if [ "$dirty" != "0" ] && [ "$dirty" != "gone" ] && [ "$path" != "$MAIN" ]; then
    case "$state" in
      *DEAD) stall "dirty worktree, lock pid dead: $rel ($dirty files)" ;;
      unlocked) stall "dirty worktree, no lock (check who owns it): $rel ($dirty files)" ;;
    esac
  fi
done <<EOF
$(printf '%s\n' "$WTLIST" | awk '
  /^worktree / { if (p != "") print p "\t" b "\t" l; p = substr($0, 10); b = "(detached)"; l = "-" }
  /^branch /   { b = substr($0, 19) }
  /^locked/    { l = (length($0) > 7) ? substr($0, 8) : "locked" }
  END          { if (p != "") print p "\t" b "\t" l }')
EOF
[ "$n" -gt "$MAX_WT" ] && echo "  ... $((n - MAX_WT)) more"

# Local branches with commits that origin does not have.
echo
echo "UNPUSHED BRANCHES (no origin branch, or commits that origin does not have)"
n=0
while IFS="$TAB" read -r br age; do
  if [ -z "$br" ] || [ "$br" = "$DEF" ]; then continue; fi
  if git -C "$MAIN" rev-parse --verify --quiet "refs/remotes/origin/$br" >/dev/null; then
    ahead=$(git -C "$MAIN" rev-list --count "origin/$br..$br" 2>/dev/null || echo 0)
    what="$ahead commits not on origin/$br"
  else
    has_line "$br" "$MERGED_BRANCHES" && continue
    ahead=$(git -C "$MAIN" rev-list --count "$BASE..$br" 2>/dev/null || echo 0)
    what="no origin branch, ahead $ahead"
  fi
  [ "$ahead" = "0" ] && continue
  n=$((n + 1))
  [ "$n" -le "$MAX_ROWS" ] && echo "  $br  $what, last commit $age"
  stall "unpushed commits ($ahead): $br"
done <<EOF
$(git -C "$MAIN" for-each-ref --format="%(refname:short)$TAB%(committerdate:relative)" refs/heads)
EOF
[ "$n" = 0 ] && echo "  none"
[ "$n" -gt "$MAX_ROWS" ] && echo "  ... $((n - MAX_ROWS)) more"

# Open and merged pull requests.
echo
if [ -n "$PR_NOTE" ]; then
  echo "OPEN PRS: $PR_NOTE"
  echo "LAST MERGED: $PR_NOTE"
else
  echo "OPEN PRS ($(printf '%s\n' "$PR_DATA" | awk -F'\t' '$1 == "OPENTOTAL" { print $2 }'))  number, CI, draft, hours since last commit, branch, title"
  n=0
  while IFS="$TAB" read -r tag num ci draft hours branch title; do
    [ "$tag" = "OPEN" ] || continue
    n=$((n + 1))
    [ "$n" -le "$MAX_ROWS" ] && printf '  #%s %s %s %sh %s  %.50s\n' "$num" "$ci" "$draft" "$hours" "$branch" "$title"
    case "$ci" in
      FAILURE | ERROR) stall "PR #$num CI $ci: $branch" ;;
      SUCCESS) [ "$draft" = "ready" ] && [ "$hours" -gt 24 ] && stall "PR #$num green, unmerged ${hours}h: $branch" ;;
    esac
  done <<EOF
$PR_DATA
EOF
  [ "$n" = 0 ] && echo "  none"
  [ "$n" -gt "$MAX_ROWS" ] && echo "  ... $((n - MAX_ROWS)) more"
  echo "LAST MERGED"
  printf '%s\n' "$PR_DATA" | awk -F'\t' '$1 == "MERGED" && ++n <= 5 { printf "  #%s %s %s\n", $2, $3, substr($5, 1, 60) } END { if (n == 0) print "  none" }'
fi

# Cost per merged pull request. Reads the local score ledger and the merged list above, so it
# makes no GitHub call. Fail soft: a missing ledger or a failed script prints "not read".
echo
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
LEDGER_FILE="${ORCHESTRATOR_LEDGER:-$HOME/.claude/orchestrator-ledger/ledger.db}"
if [ -n "$PR_NOTE" ]; then
  echo "COST PER MERGED PR: not read (no merged list)"
elif [ ! -f "$LEDGER_FILE" ]; then
  echo "COST PER MERGED PR: not read (no ledger at $LEDGER_FILE; run score.py scan)"
elif ! PR_COST=$(python3 -I "$SCRIPT_DIR/score.py" pr-cost --repo "$OWNER/$NAME" --ledger "$LEDGER_FILE" 2>/dev/null); then
  echo "COST PER MERGED PR: not read (score.py failed)"
else
  echo "COST PER MERGED PR  number, runs, cost in weighted units (estimate)"
  { printf '%s\n' "$PR_DATA" | awk -F'\t' '$1 == "MERGED" { print "M\t" $2 }'
    printf '%s\n' "$PR_COST" | awk -F'\t' 'NF == 3 { print "C\t" $1 "\t" $2 "\t" $3 }'
  } | awk -F'\t' '
    $1 == "M" { merged[$2] = 1; next }
    $1 == "C" { runs[$2] = $3; cost[$2] = $4 }
    END { for (n in merged) if (n in cost) { printf "  #%s %d %d\n", n, runs[n], cost[n]; k++ }
          if (!k) print "  none of the recent merged pull requests has scored runs" }' | sort -t'#' -k2,2nr | head -n "$MAX_ROWS"
fi

# Open issues by Project and Status. Optional and fail soft.
echo
if [ "$PROJECTS" = 0 ]; then
  echo "ISSUES: skipped (--no-projects)"
elif [ "$HAVE_GH" = 0 ]; then
  echo "ISSUES: not read (no gh, or origin is not on GitHub)"
else
  ISSUES=$(gh api graphql -F owner="$OWNER" -F name="$NAME" -f query='
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    hasIssuesEnabled
    issues(states: OPEN, first: 100) {
      totalCount
      nodes { number
        projectItems(first: 5) { nodes { project { title }
          fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } }
    }
  }
}' --jq '
.data.repository as $r
| "META\t\($r.hasIssuesEnabled)\t\($r.issues.totalCount)\t\($r.issues.nodes | length)",
  ($r.issues.nodes[] | .number as $n
    | if (.projectItems.nodes | length) == 0 then "ITEM\t(no project)\t-\t\($n)"
      else (.projectItems.nodes[] | "ITEM\t\(.project.title)\t\(.fieldValueByName.name // "(no status)")\t\($n)") end)' 2>&1)
  if [ $? -ne 0 ]; then
    echo "ISSUES: not read ($(api_err "$ISSUES"))"
  else
    printf '%s\n' "$ISSUES" | awk -F'\t' '
      $1 == "META" { if ($2 != "true") { print "ISSUES: issues are off in this repo"; off = 1 }
                     else printf "ISSUES (%s open%s) by Project and Status; numbers for Doing and Review\n", $3, ($4 < $3 ? ", first " $4 " read" : "") }
      $1 == "ITEM" { k = $2 " / " $3; if (!(k in c)) order[++m] = k; c[k]++
                     if ($3 == "Doing" || $3 == "Review") ids[k] = ids[k] " #" $4 }
      END { if (off) exit
            for (i = 1; i <= m; i++) { k = order[i]; printf "  %s: %d%s\n", k, c[k], (k in ids ? " " substr(ids[k], 1, 80) : "") }
            if (m == 0) print "  none" }'
  fi
fi

# Priorities file size.
if [ -z "$PRIORITIES" ]; then
  PRIORITIES="$HOME/.claude/orchestrator-priorities/$(printf '%s' "$MAIN" | tr '/' '-').md"
fi
echo
if [ -f "$PRIORITIES" ]; then
  lines=$(wc -l < "$PRIORITIES" | tr -d ' ')
  if [ "$lines" -gt "$PRIORITIES_LIMIT" ]; then
    echo "PRIORITIES FILE: $lines lines, over the limit of $PRIORITIES_LIMIT. Move history to the tracker."
  else
    echo "PRIORITIES FILE: $lines lines (limit $PRIORITIES_LIMIT)"
  fi
else
  echo "PRIORITIES FILE: none yet"
fi

echo
echo "STALLED"
if [ -n "$STALLED" ]; then
  printf '%s' "$STALLED" | head -n "$MAX_STALL"
  total=$(printf '%s' "$STALLED" | wc -l | tr -d ' ')
  [ "$total" -gt "$MAX_STALL" ] && echo "  ... $((total - MAX_STALL)) more"
else
  echo "  none"
fi
exit 0
