# board-agents: design for Vercel Board agent roles

Status: design, with slice 1 of the scripts (`board-agents/`) built. Section 10 lists what the real CLI changed; `VB-CLI.md` wins over older text here. Tracker: Kanzo ai-skills#87.
Pod build: Kanzo home#85 (the home session builds the pod from section 7).

## 0. How to read this

- **Verified** = shown in the spike (`k3s/services/board/NOTES.md`, `demo-output.txt`).
- **UNVERIFIED** = not in the spike. Test it first. Where a `vb` command shape is not in
  the notes, the doc says "shape not in notes" and does not invent flags.
- Spike facts: `@vercel/board` 0.54.0, CLI `vb` / `board`, Node >= 22.18 (host 22.18.0
  fails on `tasks create` with a sqlite 500; image `node:22.23.5` works).

Verified `vb` commands (from the demo):

```
vb tasks claim <id> --lease 30s --as <name>     # claim one task
vb tasks claim --ready work --as <name>         # claim the next ready task in channel "work"
vb tasks complete <id> "<text>" --as <name>
vb tasks block <id> "<text>" --kind awaiting_approval   # real kinds: needs_input, awaiting_approval (section 10)
vb tasks unblock <id>
vb read <id>                                    # who holds it, until when, what it waits for
vb tasks create ... --needs <id>                # --needs is in the notes; full shape is not
vb tasks run -- <cmd>                           # completes or fails by exit code; renews the lease
```

Verified errors: `task_claimed` (second claim), `task_waiting` (`--needs` prerequisite not
complete), `task_blocked` (held for a person), `claim_lost` (late complete). Verified
states (label `board.task`): `ready`, `claimed`, `blocked`, `completed`. A task has a
`channel` (demo: `work`) and a `thread`.

## 1. Goals and non-goals

Goals: a queue where an interactive orchestrator files work, headless roles carry it to a
PR, review, CI repair and a final user check, and the user is asked only for what needs
them. Each Board task links its GitHub issue; GitHub issues stay the tracker for the-monorepo.

Non-goals: no merge, deploy, migration or force push by any role. No change to the
orchestrator plugin. No real multi-user identity (Board has none, see section 5).

Conventions reused from `plugins/orchestrator`: the brief (goal, repo, priority, tracker
id, model, what is ruled out, the standard "do not" lines, finish line, word cap); the
finish line (commit, checks by exit code, push, PR, comment, steps with `cd <abs path>`
commands); the "Waiting on you" kinds (phone check, merge, deploy or migration, decision,
close); the worker rules in `plugins/orchestrator/agents/worker.md`. Every role prompt is
those worker rules plus a role section.

## 2. Roles at a glance

| Role | Trigger | Claims | Ends with |
|---|---|---|---|
| Orchestrator (interactive, outside the pod) | the user | nothing | files work tasks, links the GitHub issue |
| Worker | a `ready` work task | work tasks | PR open, `complete`, creates the review task |
| Reviewer | a review task (waits on `--needs`) | review tasks | `complete` (approve) or send back |
| CI fixer | a ci-fix task | ci-fix tasks | pushes a fix, `complete` |
| Needs-me relay | a timer, no claim, no Claude | nothing | runs `unblock` after a user answer; watches PR checks |

How task kinds are told apart (channel or tag) is open: see 5.1 and O1.

## 3. Task state machine

Per unit of work: one WORK task (W), one REVIEW task (R, created with `--needs W`),
zero or more CI-FIX tasks (C1..Cn), one FINAL task (F, for the user).

```
 orchestrator files W (ready), text links the GitHub issue
        |
        v
   W ready --claim--> W claimed --Claude finishes--> PR open
        |                 |-- needs the user --> W blocked(decision)
        |                 |        user answers in thread --> relay `unblock` --> W ready
        |                 \-- lease lapses --> W ready (stale worker gets claim_lost)
        v
   worker: create R --needs W; complete W "<PR url>"   (R: task_waiting -> ready)
        v
   R ready --claim--> reviewer reads the PR
        |-- approve   --> complete R
        \-- send back --> create W' (round+1, findings quoted); complete R "send_back"
        v
   watcher polls PR checks after approve (and after each C)
        |-- all pass --> create F, blocked --kind approval ("final review")
        \-- any fail --> create C(n+1) while n < cap; else block decision
   F: the user merges (merge kind) or sends back with a comment (new W').
```

Rules:

- Send-back is a new task, not a reopened one (reopen shape not in notes, UNVERIFIED).
  Its text starts `round N/3 rework of #<id>` and quotes the findings. The verdict is also
  posted as a PR comment.
- Caps, read from the task text before a role starts: review send-backs at most 2 (round
  3 is the last); CI fixes at most 3 per PR; decision round trips at most 3 per task. At
  a cap the role calls `block --kind decision` with the history and stops.
- Task ids are not sequential. Never compute `id+1`; read ids from command output. Every
  task text carries `pr:<owner/repo>#<n>` and `issue:<owner/repo>#<n>` as the link.
- One role owns each transition: worker creates R; reviewer creates W'; watcher (in the
  relay container) creates C and F; relay runs `unblock`.

### 3.1 How the CI fixer learns checks failed

A small loop in the relay container (no Claude), every 60 s, for each PR whose R or C
completed:

```
gh pr checks <n> --repo <r>     # exit 0 pass, 8 pending, 1 fail (gh docs; verify in image)
pending  -> wait
pass     -> create F
fail     -> if fewer than 3 C tasks: create C(n+1) in the ci queue; else block decision
```

State lives in the board (task text, thread), not on disk (emptyDir). Webhooks are out:
the board is loopback only and the pod has no public endpoint. Rejected alternative: the
reviewer waits for checks; it would hold a lease and Claude time while idle.

## 4. Role specs

Common to all Claude roles:

- `claude -p "<prompt>" --model sonnet --max-turns <N>`; exact flag set for headless use is
  UNVERIFIED. Subscription auth only: `CLAUDE_CODE_OAUTH_TOKEN`, never `ANTHROPIC_API_KEY`.
- Lease: wrap the whole job in `vb tasks run -- <runner>`. The notes say `tasks run`
  renews at one third of the lease (max 5 minutes) and completes or fails by exit code. How
  it picks the task and passes id and text to the child, and the CLI `fail` shape, are
  UNVERIFIED (not in notes). Fallback: `claim`, then a background renew loop; the renew
  command is not in the notes. Test both in the first implementation PR.
- A Claude run longer than the lease is normal. `tasks run` keeps the lease; Claude never
  sees it. Wall-clock cap: 45 min work, 20 min review and CI fix (`timeout` around
  `claude`); on timeout the runner exits non-zero and the task fails.
- claim_lost mid-run (renew fails or `complete` returns `claim_lost`): kill the Claude
  process group, delete the role's checkout, do not push, do not complete, exit. If the
  branch was already pushed, the next owner starts from it; the task text names the branch.
- Claude crash or non-zero exit: runner exits non-zero, the task fails, and the last 40
  lines of output (secrets filtered) go on the task as a comment. Two failures in a row:
  block as `decision`.
- Missing board token: retry `vb` with backoff, never cache the token, never read
  `~/.config/board` directly (7.5).
- Cost caps per task: `--max-turns`, wall-clock cap, the round caps of section 3, and
  after 3 Claude runs on one task the next role blocks it for a decision. Spend is read
  with `plugins/orchestrator/scripts/spend.py` (use in the pod UNVERIFIED). Sonnet by
  default; Opus only with a reason in the task text.
- Concurrent Claude runs are capped pod-wide at 2 (7.6).

### 4.1 Worker

- Trigger: `vb tasks claim --ready work --as worker-1` (verified).
- Steps: claim; `vb read <id>` for the text; clone the repo, branch `board/<id>-<slug>`;
  run Claude with the worker prompt; push with the PAT; `gh pr create`; create R with
  `--needs <W>`; `complete W "<PR url>"`.
- Prompt: the task text is the brief (the orchestrator writes it in the section 1 format),
  after the `worker.md` rules. Add-on: "Work only on this task. Open the PR against the
  default branch. Never merge. If a user decision is needed, push a `WIP:` commit and print
  `NEEDS_DECISION: <question>` as the last line."
- Needs the user: the runner sees `NEEDS_DECISION:` and runs
  `vb tasks block <id> "<question, options, recommendation>" --kind decision` (value
  UNVERIFIED; `approval` is verified). After `unblock`, a worker re-clones and continues
  from the pushed branch.
- Finish line: commits at each step, repo checks pass by exit code, branch pushed, PR open
  with `Refs #<issue>`, `complete` with the PR URL, R created.

### 4.2 Reviewer

- Trigger: `vb tasks claim --ready review --as reviewer-1`. R is `task_waiting` until W
  completes (verified), so it never sees a half-done PR.
- Steps: clone, `gh pr checkout <n>`, run Claude with a review-only prompt (read the diff,
  run the repo's named checks, no edits, no push). Last line: JSON
  `{"verdict":"approve|send_back","findings":[...]}`.
- approve: PR comment with findings; `complete R "approve"`.
- send_back: PR comment; create W' (round+1, findings, branch name); `complete R "send_back"`.
  At round 3: `block --kind decision` instead.
- No edit tools (`--disallowedTools Edit Write`, flag name UNVERIFIED) and a read-only PAT (O5).

### 4.3 CI fixer

- Trigger: C task from the watcher; `vb tasks claim --ready ci --as ci-fixer-1`.
- Steps: clone, check out the PR branch, read the failure (`gh pr checks`, `gh run view
  <run> --log-failed`; standard gh, flags verified only by docs), run Claude with a fix-only
  prompt: smallest change for the named failure; never delete or rewrite an existing test
  (if the test is wrong, `NEEDS_DECISION`); normal push to the same branch, never force;
  `complete C "<sha>"`.
- Flaky check: same failure and log with no code change, rerun once
  (`gh run rerun --failed`) before editing.
- Cap: 3 C tasks per PR, then `block --kind decision`.

### 4.4 Needs-me relay

- Trigger: timer, every 30 s. Claims nothing, runs no Claude.
- Loop: (1) list blocked tasks (list command shape not in notes; the SDK is the fallback);
  (2) read each thread (`vb read <id>`; reading replies UNVERIFIED); (3) find a reply after
  the block comment that starts with `ANSWER:` (5.2); (4) run `vb tasks unblock <id>`
  (verified) and post a thread note (comment shape not in notes).
- A reply alone never unblocks (demo: `task_blocked` until `unblock`). The relay is the only
  role that unblocks, so a role cannot answer its own question.
- Check task state before `unblock`; unblocking a `ready` task is UNVERIFIED.
- PR watcher limits (env, defaults): a PR with pending or errored checks backs off
  `BA_PR_BACKOFF_START` 60 s, doubling to `BA_PR_BACKOFF_MAX` 900 s, per PR, in
  `$BA_RELAY_STATE_DIR/pr-watch.tsv` (default `$BOARD_AGENTS_SHARED_DIR/relay`, so a restart
  keeps it). It gives up after `BA_PR_PENDING_MAX` 21600 s pending or `BA_PR_ERROR_MAX` 5
  errors in a row: a reply on the task, and an `awaiting_approval` task for the user.
- The user is outside the pod and the board is loopback only (O7). The interactive
  orchestrator carries the user's answer into the thread and keeps its "Waiting on you"
  list from the board; the relay writes no file.

## 5. Identity and safety

### 5.1 Avoiding another role's tasks

`--as <name>` is a display name only (verified); any process in the pod can claim any task.
Separation is by convention, enforced in the runner code:

1. One channel per role kind (`work`, `review`, `ci`), each runner uses
   `claim --ready <its channel>` only. Channels exist (verified: `work`); choosing one at
   create time is UNVERIFIED (shape not in notes).
2. Fallback: a `[type:review]` prefix in the task text; the runner checks it after claim and
   on mismatch fails the task with `wrong role` (release shape not in notes, so a wrong claim
   costs a lease cycle).
3. Names `worker-<n>`, `reviewer-<n>`, `ci-fixer-<n>`, `relay` for audit only, never for access.
4. Reviewer and worker are different containers, so no session reviews its own diff.

### 5.2 Telling user replies from agent replies

Every actor is `local:user:root` (demo output), so identity cannot separate them. The user's
answer starts with `ANSWER:`; agents never write that prefix (O3).

### 5.3 Git and GitHub

- Push only to `board/*` branches the role created (CI fixer: the PR branch). No push to
  the default branch, no merge, no force push. Enforced three ways: the prompt; GitHub
  branch protection on the default branch (the user sets it; a PAT scope cannot do this);
  a `pre-push` hook in the image that rejects `--force`, `+` refspecs and default-branch refs
  (hook is simple, content UNVERIFIED).
- No deploy, apply or migration. If one is needed the role blocks `decision` with the
  exact command, starting `cd <absolute path>`.
- Fine-grained PAT: `contents: write`, `pull requests: write`, `checks: read`,
  `issues: write` (link comment), limited to the chosen repos.
- Secrets never go in task text, comments or commits. A filter strips `ghp_`,
  `github_pat_`, `sk-`, and the env token values before any comment.

## 6. Prompts and skills

Role prompts live in `plugins/board-agents/prompts/` (`worker.md`, `reviewer.md`,
`ci-fixer.md`), written in the implementation PR: the `worker.md` rules plus the section 4
add-on. Loaded as `claude -p "$(cat prompts/worker.md)"` with the task text appended. The
orchestrator plugin is not needed in the pod. A target repo's own `.claude/` and
CLAUDE.md come with the checkout and win. The orchestrator-side change (file a W from a
brief, link the issue, pick the channel) is a later PR; the orchestrator plugin stays unchanged.

## 7. Container requirements (send as-is to the home session, Kanzo home#85)

**FIXED** = decided by the user. **OPEN** = needs an answer; a recommendation is given.

### 7.1 Pod shape

| Item | Value | Status |
|---|---|---|
| Containers | `board`, `worker`, `reviewer`, `ci-fixer`, `relay` (relay also runs the PR watcher) | FIXED |
| Replicas | one of each | OPEN (recommend 1; raise later) |
| Network | shared pod loopback; board on `127.0.0.1:4100`, nothing exposed | FIXED |
| Image | one image for all: `node:22.23.5` | FIXED |
| Nodes | amd64 only: front-desk, beelink-001, beelink-002 (about 4 CPU, 15 GiB each, shared). Image must be amd64; set `nodeSelector kubernetes.io/arch: amd64` | FIXED |
| UID / HOME | one UID, one HOME; recommend UID 1000 (`node` user), `HOME=/home/node` | FIXED (shared), OPEN (value) |
| Token volume | emptyDir at `$HOME/.config/board`, mounted in all five containers | FIXED |
| Repo checkouts | emptyDir, recommend `/work` with `/work/<role>` per role | FIXED (emptyDir), OPEN (path) |
| Board state | `vb serve --dir /data/board` (`.board/local.db`, sqlite WAL). On emptyDir, tasks are lost at pod restart | OPEN (recommend a 1 GiB PVC) |

### 7.2 Image contents

FIXED: `node:22.23.5`, `@vercel/board@0.54.0` (`npm i -g @vercel/board@0.54.0`), git.
OPEN, all recommended yes:

- `claude`: `npm i -g @anthropic-ai/claude-code`, version pinned at build (package name and
  headless behaviour to confirm against the Claude Code install docs; UNVERIFIED here).
- `gh`: apt package from the GitHub CLI repo (the node image is Debian-based) or the release tarball.
- `jq`, `curl`, `ca-certificates`, coreutils `timeout` and `flock`. No `openssh-client`: the pod
  pushes over HTTPS with the PAT (the user's "SSH only" rule is for laptop workers).
- Each target repo's toolchain for its checks (for the-monorepo, its package manager, e.g.
  pnpm via corepack). OPEN: which repos (O10).
- Runner scripts and prompts from this plugin at `/opt/board-agents`.

### 7.3 Secrets

Agent containers read one plain Kubernetes Secret `board-agents` in namespace `board`
(`envFrom: secretRef` or `secretKeyRef`): `CLAUDE_CODE_OAUTH_TOKEN`, `GIT_USERNAME`,
`GIT_TOKEN`. The user creates it by hand with `kubectl` for the first deploy; later an
operator (Infisical operator or External Secrets Operator, not chosen) fills the same
Secret. Agents need no Infisical CLI, SDK or token. FIXED. The `board` container needs no
secret.

### 7.4 Git, gh and Claude

- Git: a credential helper reading the env, set at container start, no token on disk:
  `git config --global credential.helper '!f() { echo username=$GIT_USERNAME; echo password=$GIT_TOKEN; }; f'`.
  `user.name` and `user.email` from env (OPEN: values). FIXED: env names.
- gh: `lib.sh` exports `GH_TOKEN=$GIT_TOKEN` when it is missing; no `gh auth login`. The credential
  helper is set through `GIT_CONFIG_*` env vars, so no config file and no token on disk.
- `BOARD_AGENTS_SHARED_DIR` (default `/shared`): shared emptyDir for the Claude-run cap (flock
  slot files and the hourly run log). Not the token dir, not `/data`.
- Reviewer token (decided by the home session): the reviewer reads `REVIEWER_GIT_TOKEN`. If it is
  missing, the reviewer fails at start. It never falls back to `GIT_TOKEN`. `lib.sh` `ba_auth_setup reviewer`
  enforces this and uses that token for git and `GH_TOKEN`. Until the pod sets it, the reviewer
  does not run. The relay takes only `GH_TOKEN`.
- Claude: `CLAUDE_CODE_OAUTH_TOKEN` (subscription, from `claude setup-token`); `ANTHROPIC_API_KEY`
  must be unset. FIXED.
- `~/.claude`: lives in the shared HOME; recommend emptyDir, plus `CLAUDE_CONFIG_DIR=$HOME/.claude-<role>`
  per role to avoid shared state. OPEN. Transcripts vanish on restart; a PVC only if spend
  tracking needs them. Headless first-run prompts and permission mode are UNVERIFIED.
- Skills or plugins: none required.

### 7.5 Per role

| Role | Start command | Env | Paths | Board restart |
|---|---|---|---|---|
| board | `vb serve --dir /data/board` (loopback, port 4100; no bind flag) | none | `/data/board`; writes `$HOME/.config/board/local/<hash>.json` | New in-memory keys, new token file. State survives only on a PVC. Readiness: TCP 4100 (HTTP gives 401 without a token) |
| worker | `/opt/board-agents/run-worker.sh` (loop of `vb tasks run -- ...`) | `board-agents` Secret, `GH_TOKEN`, `ROLE=worker` | `/work/worker`, `~/.claude-worker`, `$BOARD_AGENTS_SHARED_DIR` (`/shared`) | see rule below |
| reviewer | `/opt/board-agents/run-reviewer.sh` | Secret (read-only PAT if O5), `ROLE=reviewer` | `/work/reviewer`, `~/.claude-reviewer`, `/shared` | same |
| ci-fixer | `/opt/board-agents/run-ci-fixer.sh` | Secret, `ROLE=ci` | `/work/ci`, `~/.claude-ci`, `/shared` | same |
| relay | `/opt/board-agents/run-relay.sh` (loop plus PR watcher) | `GH_TOKEN` only (read-only); no Claude token | none | same |

Board restart rule (FIXED, from the user): agents call `vb` per command, never cache the
token, and tolerate a missing token file. On a missing file, 401 or connection error: retry
with backoff 1, 2, 4, 8, 16, 30 s for up to 5 minutes, then exit non-zero so Kubernetes
restarts the container. A role mid-run: its lease lapses (at most 5 min), the task returns
to `ready` (verified), and the old runner gets `claim_lost` and discards its checkout.
Start scripts wait for `vb` to answer before the loop; each role clones lazily per task
(emptyDir, so every pod restart means a fresh clone).

### 7.6 Sizing on shared nodes, and a cap on concurrent Claude runs

The nodes are shared, so keep requests low and limits firm. Estimates, UNVERIFIED:

| Container | CPU req / limit | Memory req / limit |
|---|---|---|
| board | 100m / 250m | 128Mi / 256Mi |
| worker | 500m / 1500m | 1Gi / 2Gi |
| reviewer | 250m / 1000m | 768Mi / 1.5Gi |
| ci-fixer | 250m / 1000m | 768Mi / 1.5Gi |
| relay | 50m / 100m | 64Mi / 128Mi |
| **Pod total** | **1150m / 3850m** | **2.7Gi / 5.4Gi** |

Concurrency cap (recommended, OPEN): at most 2 Claude runs at once across the pod, using
a `flock` semaphore with 2 slot files under `$BOARD_AGENTS_SHARED_DIR/slots` (`/shared`, an emptyDir mounted in all four agent containers; each runner
takes a slot before `claude` and waits, with the lease kept by `tasks run`). Any role can
take a slot, but the worker is the heaviest, so a normal peak is worker + one of
reviewer or ci-fixer. With the cap, the peak is worker + reviewer + board + relay: about
1.5 + 1.0 + 0.25 + 0.1 = 2.9 CPU and 2 + 1.5 + 0.25 + 0.125 = 3.9 Gi at limits, which fits
one node next to other work. Without the cap, three Claude runs plus repo builds could
reach the 5.4 Gi total. Waiting for a slot counts against the wall-clock cap only after
the slot is taken. Repo checks (builds, tests) are the memory risk, not Claude itself; raise
the worker limit per repo if the-monorepo needs it.

## 8. Open questions for the user

| # | Question | Recommendation |
|---|---|---|
| O1 | One Board channel per role (`work`, `review`, `ci`) or tags on one channel? | Channels, if `tasks create` can set one. Test first. |
| O2 | Caps: 2 send-backs, 3 CI fixes, 3 decision round trips, 45 min work run. OK? | Yes; tune after about 20 tasks. |
| O3 | Do you accept an `ANSWER:` prefix on replies so the relay can tell them from agents? | Yes; the board has no identity. |
| O4 | Does the relay container host the PR watcher (no Claude)? | Yes; polling every 60 s is cheap, no webhook path exists. |
| O5 | A separate read-only PAT for the reviewer and relay? | Yes. |
| O6 | Reviewer approves on GitHub or only comments? | Comment only; you do the final review. |
| O7 | How do you reach the board to answer? | Through your orchestrator session only; keep the board on loopback. The path from laptop to pod is not defined. |
| O8 | Cap concurrent Claude runs at 2 pod-wide? | Yes (7.6); also an hourly cap of 10 runs for subscription limits. |
| O9 | PVC for board state and for `~/.claude`? | PVC for board state; emptyDir for `~/.claude`. |
| O10 | Which repos and toolchains go in the image? | Start with the-monorepo only. |

## 9. Unverified in the spike (test before building)

1. `tasks create` options: channel, tags, `--needs` with them, output format.
2. `--kind decision`: it does not exist (kinds are `needs_input` and `awaiting_approval`; see section 10).
3. How `tasks run` binds a task to the child (id, text) and its lease flag.
4. The renew command; the CLI `fail` shape.
5. Release without completing; reopen; list tasks or blocked tasks; read thread replies; post a comment.
6. That the loopback token file reads across containers on one emptyDir with one UID.
7. `claude -p` headless with the OAuth token: prompts, permission mode, shared-HOME concurrency.
8. `gh pr checks` fields and exit codes in the installed `gh`.
9. Client recovery after a board restart (the notes say only that the token changes).
10. All CPU and memory figures.

## 10. Facts from the real CLI (slice 1)

Full help text and tested behaviour: `VB-CLI.md`. What changed against sections 0 to 9:

- `BOARD_URL`: `vb` reads it itself. The pod sets `BOARD_URL=http://127.0.0.1:4100/api/board`; no
  `--url` flag is needed. Agents talk to `vb serve` directly and never go through Caddy, so a later
  Caddy login on `board.k3s.lan` does not affect them.
- The smoke and list command is `vb tasks` (with `--state`, `--label`, a channel). Not `tasks list`.
- Block kinds: `needs_input` (was "decision") and `awaiting_approval` (was "approval").
- Create: `vb tasks create <channel> "<text>" --label step=<kind> [--needs <id>] [--idempotency-key K]`.
  Claim: `vb tasks claim --ready <channel> --label step=<kind> --as <name>`. One channel per role
  (`work`, `review`, `ci`, `final`) and a `step` label; the relay creates missing channels. A list
  of a channel that does not exist is an error.
- `--as <name>` on claim must be repeated on `block`, `renew`, `complete`, `fail`, `release`.
  `unblock`, `label`, `reopen`, `cancel` are owner actions and need no claim.
- Thread: `vb read <id> --json` returns `replies[]` with `id`, `text`, `createdAt`. Comment:
  `vb reply <id> "<text>"`. Ids are numeric and grow.
- `block` stores its reason on the task, not in the thread. So the runner also posts a reply that
  starts `BLOCKED:`. The relay unblocks on the first reply that starts `ANSWER:` and has a higher id
  than the last `BLOCKED:` reply, then posts `UNBLOCKED:`. A task blocked without a `BLOCKED:` marker
  is skipped. The relay handles `needs_input` only; `awaiting_approval` (final review) is for the user.
- `tasks run` passes only `BOARD_TASK` (the id) to the child. The runner reads the text with `vb read`.
- Error reasons: `nothing_ready` (normal), `network_error`, `no_project` (token file missing). The
  last two are retried with backoff. Claim records live in `~/.config/board/claims/`; keep that dir.
- Security: the board web inspector has no login, so any user on the LAN can post in any thread.
  The relay treats thread text as data (never evaluated, never put in a command line except as a
  quoted argument, PR keys checked against a strict pattern). The `ANSWER:` marker is a convention,
  not an identity check. A person who can reach the board can answer for the user. If that is not
  acceptable, put a login in front of the inspector. Agents are unaffected, as above.
- GitHub quota: a fine-grained PAT is rate limited per user, so the agents' PAT draws on the same
  5,000 requests per hour as the user's own `gh` calls (GitHub documents the limit per user; not
  proven against this PAT in the pod). The watcher makes one `gh pr checks` call per open PR per
  60 s poll and backs off (2 min, doubling to 15 min) on a rate-limit reply. Five open PRs cost
  about 300 calls per hour.

### Slice 2: the runners (`run-worker.sh`, `run-reviewer.sh`, `run-ci-fixer.sh`)

- Lease: the runner claims, then a background loop runs `vb tasks renew --as` every 5 min (lease 15 min).
  `vb tasks run` is not used: its `--result` is fixed before the job starts, but the reviewer's
  result depends on the verdict. A failed renew is `claim_lost`: Claude is stopped, the checkout is
  deleted, nothing is pushed or completed.
- Claude gets no git or GitHub credentials. The runner clones, pushes (plain push of `HEAD` to a
  `board/*` branch, never the default branch) and makes the few `gh` calls. Claude flags: `CLAUDE-CLI.md`.
- Last-line protocol: worker `RESULT: done` or `NEEDS_INPUT: ...`; reviewer one JSON verdict line;
  CI fixer `RESULT: fixed`, `RESULT: no_change ...` or `NEEDS_INPUT: ...`.
- Reviewer push guard: the reviewer's PAT (`REVIEWER_GIT_TOKEN`) may be able to write for now. So
  the reviewer checkout cannot push: `git remote set-url --push origin DISABLED` plus a `pre-push`
  hook that exits 1, and the prompt says the reviewer only comments. This does not depend on the PAT being read-only.
- Task text keys: `repo:<owner/repo>`, `branch:board/...`, `issue:<owner/repo>#<n>`, `round N/3` (work),
  `round:N` (review), `pr:<owner/repo>#<n>`. Values are matched against strict patterns.
- gh calls per task: worker 2 (`pr list`, `pr create`), reviewer 2 (`pr view`, `pr comment`), CI fixer
  2 to 3 (`pr view`, `run view --log-failed` for up to 2 runs).
- Not built: flaky rerun (`gh run rerun`), "two failures in a row, then block", the pod's `pre-push` hook for the worker.

### Assumptions taken from section 8 (not confirmed by the user)

O1 channels per role plus a `step` label. O2 caps 2 send-backs, 3 CI fixes, 3 decision round trips
(CI cap built in the relay). O3 the `ANSWER:` prefix. O4 the relay hosts the PR watcher. O5 a
separate reviewer PAT (now `REVIEWER_GIT_TOKEN`). O6 the reviewer only comments. O8 two Claude runs
at once, 10 runs per hour. O10 the-monorepo only.
