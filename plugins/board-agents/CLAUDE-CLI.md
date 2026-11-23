# claude CLI facts for the runners

Checked with `claude --help` and `claude --version` (2.1.330) on the dev machine. The pod image
installs its own version; re-check these flags after a version bump.

Flags used by `ba_claude` in `board-agents/lib-run.sh`:

| Flag | Why |
|---|---|
| `-p` | headless, reads the prompt from stdin, prints the answer |
| `--model sonnet` | default model (`BA_MODEL` overrides) |
| `--max-turns N` | turn cap. It is not in `--help` in 2.1.330 but the parser accepts it (an unknown flag errors, this one does not). Unverified in a real run |
| `--permission-mode dontAsk` | anything not in the allow list is denied without a prompt, so a run never stalls. Choices in this version: acceptEdits, auto, bypassPermissions, manual, dontAsk, plan |
| `--allowedTools a,b,...` | the allow list. `Bash(git add:*)` style prefixes; no `git push`, no `gh`, no `vb` |
| `--disallowedTools ...` | explicit deny for push, gh, vb, curl, wget, ssh (and Edit/Write/NotebookEdit for the reviewer) |
| `--no-session-persistence` | no transcript files in the pod |
| `--append-system-prompt "<rules>"` | the role prompt (`prompts/common.md` plus the role file) |

Not used: `--dangerously-skip-permissions`, `--bare` (it skips CLAUDE.md and hooks; the repo's own
rules must load). `--max-budget-usd` exists but the pod uses a subscription, not API billing.

Other facts:
- Wall clock: `timeout -k 20 <seconds> claude ...` (work 2700, review and CI 1200). Exit 124 means timeout.
- The task data goes on stdin, wrapped in `<<<TASK TEXT ... >>>` markers and labelled untrusted. The
  rules go in the system prompt.
- Auth: `CLAUDE_CODE_OAUTH_TOKEN` only. The runner unsets `ANTHROPIC_API_KEY`. The Claude process also
  gets no `GH_TOKEN`, `GIT_TOKEN`, `REVIEWER_GIT_TOKEN` or git credential env, so it cannot push even
  if a tool rule is bypassed. The runner pushes and calls `gh`.
- `CLAUDE_CONFIG_DIR` is `$HOME/.claude-<role>`.
- A target repo's `.claude/settings.json` (hooks) loads with the checkout. Hooks run with the pod's
  env minus the credentials above.
- Not verified here: a real headless run with the OAuth token, first-run prompts, and that
  `dontAsk` plus the allow list leaves the repo's check commands runnable (pnpm, npm, node, npx are allowed).
