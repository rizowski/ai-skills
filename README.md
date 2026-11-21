# ai-skills

My skills for AI coding agents, packaged as a Claude Code plugin marketplace.

| Plugin | What it does |
|---|---|
| [orchestrator](plugins/orchestrator/skills/orchestrator/SKILL.md) | Runs a Claude Code session as an orchestrator: it manages worker subagents against a priorities file, does no task work itself, and shows the project's tracker tasks in a pane. |

## Agents

The plugin ships two lean subagent types. They start with far less context than `general-purpose`, which keeps long orchestrator sessions cheaper.

| Agent | Model | Use it for |
|---|---|---|
| `orchestrator:scout` | haiku | Read-only lookups in a repo or on GitHub. No project instructions, no MCP tools, no edits. |
| `orchestrator:worker` | sonnet | A code task driven to an open PR. Keeps the repo's instructions. No MCP tools. |

Use `general-purpose` or a project specialist when a task needs MCP tools (cloud accounts, trackers, browsers). Give it an explicit `model`, or it runs on the orchestrator's model.

A worker is done when its work is committed, the checks pass by exit code, its feature branch is pushed with a pull request open, the tracker task has a comment, and its report lists the user's steps with exact commands. Workers never push to the default branch, merge a pull request or force push.

## Models

The orchestrator runs best on Opus with Fable 5.2 as the advisor. A skill cannot set either, so the launch report says which one is missing:

```text
/model opus
/advisor fable
```

The advisor is experimental. It is attached only when it ranks at or above the main model. Each advisor call reads the whole transcript without the cache, so the skill consults it only at a few moments.

## Scripts

Both are in `plugins/orchestrator/scripts/`. The skill runs them; you can also run them by hand inside a repo.

- `sweep.sh`: one digest of at most about 60 lines. It shows the default branch against `origin`, each worktree (branch, ahead and behind, uncommitted files, lock and whether the lock's process is alive), local branches that `origin` does not have, open pull requests with CI state, the last 5 merged pull requests, open issues by Project Status, the size of the priorities file, and a `STALLED` list. It needs git and gh, works on macOS bash 3.2, and spends two GitHub API calls. Options: `--no-fetch`, `--no-projects`, `--priorities FILE`.
- `spend.py`: estimates cost from Claude Code transcripts. Run it with `python3 -I`. See "Measuring".
- `score.py`: scores each subagent run into a SQLite ledger. See "Scoring workers". `transcripts.py` holds the transcript parsing that `spend.py` and `score.py` share.
- `judge.py`: a small model labels the quality of a sample of runs. See "Judging quality".

## Recommended settings

These are optional. Each name is in the Claude Code docs. Nothing here is changed for you.

- `CLAUDE_CODE_SUBAGENT_MODEL`: environment variable that sets the model for subagents that do not set their own. Set `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` as well to force it onto every subagent, which overrides the `model` field of these agents.
- `subagentPromptCacheTtl`: setting, `"5m"` or `"1h"`. It picks how long the prompt cache holds subagent requests. The default for subagents is 5 minutes. Use `"1h"` if you often send follow-ups to a worker after a pause.
- Turn off MCP servers and connectors you do not use. Their tool definitions load into every agent that inherits tools.
- Measure with `/usage` (session cost and cache hit rate) and `/context` (what fills the context window), and with `spend.py` (see "Measuring").

## Measuring

`spend.py` reads the transcripts in `~/.claude/projects/<folder>/` and prints an estimate in weighted units, not dollars. The weights are in the tables at the top of `transcripts.py`. The output holds only numbers, model names, agent types, tool names and 8-character session ids.

```sh
python3 -I plugins/orchestrator/scripts/spend.py --project <repo folder> --days 10
python3 -I plugins/orchestrator/scripts/spend.py --current
```

`--current` prints the context size and the cost so far of the session that runs it (from `CLAUDE_CODE_SESSION_ID`), or of the newest session of the project. It also prints `HANDOFF: yes` near 120,000 tokens of context, which is when the orchestrator hands off to a fresh session.

### Scoring workers

`score.py` scores every subagent run from the same transcripts and keeps one row per run in a SQLite file, `~/.claude/orchestrator-ledger/ledger.db` by default. The key is the session id and the agent id, so a second scan adds no duplicate. The rows hold ids, model names, agent types, counts, branch names, PR numbers and times. They hold no transcript text. No command makes a network call.

```sh
python3 -I plugins/orchestrator/scripts/score.py scan --project <repo folder> [--days 14]
python3 -I plugins/orchestrator/scripts/score.py report [--weeks 4]
python3 -I plugins/orchestrator/scripts/score.py pr-cost --repo <owner/name>
```

`stats` prints one JSON object for a date range, for a job that reads it: `python3 -I plugins/orchestrator/scripts/score.py stats --from 2026-11-01 --to 2026-11-28 [--ledger FILE] [--project NAME]`. `--from` is inclusive and `--to` is exclusive, both local dates, compared with the start time of a run. `--project` limits the rows to one project name as stored in the ledger. A missing ledger is an error: exit code 1 and one line on stderr. An empty range gives zero counts and exit code 0. The shape is a contract, and it holds only numbers and names, never text from a transcript:

```json
{
  "schema": 1,
  "from": "2026-11-01",
  "to": "2026-11-28",
  "runs": 123,
  "code_runs": 80,
  "cost": 123456789,
  "failures": {"no_model": {"runs": 10, "cost": 1234}, "over_calls": {"runs": 3, "cost": 5678}},
  "judge": {"off_brief": {"yes": 2, "no": 3, "unknown": 1}},
  "denials": {"git-push": 4}
}
```

`runs` is every run in the range and `code_runs` the code workers among them. `failures` lists every failure kind (also with 0 runs) with the total cost of the runs that have it. `judge` holds each label with its counts, and is `{}` with no judge tables. `denials` is the count per rule, `{}` when there is none. Schema 1: a change to a key or its meaning needs a new schema number.

`scan` without `--days` reads every transcript. `report` lists the failure kinds by total cost, the policy guard denials by rule, the runs and cost by model (with the share of runs that have a failure), the cost per pull request and the top runs. `pr-cost` prints one line per pull request of a repository. `sweep.sh` uses it to print the cost per merged pull request from the ledger, with no GitHub call, and prints "not read" when the ledger is missing. A run can fail in these ways. The limits are constants at the top of `score.py`.

- `no_model`: the Agent call did not set `model`. When the parent transcript does not show the call, `model_set` is NULL and the run is not failed.
- `over_calls`: more than 60 tool calls.
- `gate_gave_up`: the finish gate let the run stop after 3 blocked stops.
- `no_commit` and `no_pr`: a code worker (`orchestrator:worker`, or `general-purpose` with Edit or Write calls) made no successful `git commit`, or no `gh pr create` that printed a pull request URL. Read-only runs are never failed for these.
- `big_context`: peak context above 150,000 tokens.

Policy guard denials are counted in the row and are never a failure. Hook events come from the hook event log (`~/.claude/hook-events/events.jsonl`, or `$CLAUDE_HOOKS_LOG_DIR/events.jsonl`), joined on the session id and the agent id. A run uses only the log when the log holds an event for its session id and agent id, or when it started at or after the first line of the log. Any older run uses only the fixed texts in the transcript (one table in `score.py`). The two are never added. A missing or empty log means the text match for every run, and a broken line is skipped. The ledger keeps denials per rule in the `run_denials` table. A commit counts when its Bash call did not fail and did not report "nothing to commit". Tests: `cd plugins/orchestrator/scripts && python3 -m unittest`.

Baseline before version 2.8.0, from 10 days of real sessions in one project:

| Measure | Value |
|---|---|
| Subagent share of cost | 57% |
| Subagents on `general-purpose` or `Explore` with Opus | 197 of 264 |
| Subagents of type `orchestrator:worker` or `orchestrator:scout` | 12 of 264 |
| Tool calls of the five costliest workers | 354 to 532 each |
| Main-thread cost from turns over 150k tokens of context | 66% |
| Orchestrator context per turn | median 180k, max 347k |
| Context at the first turn | 70k to 80k |
| Main-thread cost from cache misses after idle gaps | 2.6% |

To compare, run the same command on sessions that started after the update, with `--days` set so that only those sessions count. Look for a lower subagent share, more subagents on Sonnet and Haiku, fewer tool calls per subagent, and a lower median context per turn. These are real sessions, not scripted runs, so compare weeks of similar work.

Advisor tokens also show in `/usage`. The transcript records each advisor call as an `advisor_message` entry in `usage.iterations`, with its own model and token counts, outside the top-level usage numbers. `spend.py` counts those entries at the advisor model's weight and shows them as `<model> (advisor)`. If a later Claude Code version stops writing these entries, `spend.py` cannot see the advisor; then trust `/usage`.

### Judging quality

The failure kinds of `score.py` see cost and process. They do not see quality, for example a worker that ignored its brief. `judge.py` covers that gap. Its labels are evidence for a person. A label never blocks anything, and `judge.py` never opens a pull request.

```sh
python3 -I plugins/orchestrator/scripts/judge.py run --project <repo folder> [--weeks 1] [--limit N] [--dry-run]
python3 -I plugins/orchestrator/scripts/judge.py review [--count 5]
python3 -I plugins/orchestrator/scripts/judge.py mark <session id> <agent id> right|wrong
```

`run` picks the 10 costliest runs of code workers, up to 10 more runs with a script failure, and 5 runs with no failure as a control group. The control group is seeded with the ISO week, so a second run picks the same runs. A run that has labels is skipped. For each run it builds a small input: the brief (cut at 6000 characters), the last report (cut at 4000), the files changed by Edit and Write calls with the call count per file, the counts from the ledger, and a summary of the repository rules (the name and description of each skill in `.claude/skills`, and the headings of `CLAUDE.md` in the root and up to two folders down, cut at 3000 characters). The repository folder comes from the `cwd` of the session transcript. When it is not found on this machine, the input says "repository rules: not available" and `ignored_rule` is `unknown`. The summary is never stored. The input says when text was cut. It never holds the whole transcript. One `claude -p` call on `haiku`, with no tools and a temp folder as the working directory, answers six questions with `yes`, `no` or `unknown`: `brief_unclear`, `off_brief`, `unsupported_claim`, `redundant_tests`, `ignored_rule` and `scope_grew`. A call that fails, times out or answers in a wrong shape stores `unknown` for all six. `--dry-run` prints the sample and makes no call and no write.

The ledger holds the answers in `judge_labels` and the verdicts of a person in `judge_checks`. The reason lines go only to `judge-reasons.jsonl` next to the ledger, because a reason can quote the work. `review` prints judged runs with their reasons. `mark` stores your verdict. `score.py report` shows the counts per label and the share of checked runs that you marked right. The only network use is the `claude -p` call.

## Install

This repository is private. Claude Code clones it with the git credentials already on the machine, so sign in first:

```sh
gh auth login
gh auth setup-git
```

Add the marketplace, then install a plugin:

```sh
claude plugin marketplace add rizowski/ai-skills
claude plugin install orchestrator@rizo-ai-skills
```

Inside a session, the same steps are:

```text
/plugin marketplace add rizowski/ai-skills
/plugin install orchestrator --marketplace rizowski/ai-skills
```

If the clone tries SSH and fails, set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`.

Start the skill with `/orchestrator`, or `/orchestrator:orchestrator` if another command uses that name.

## Orchestrator tasks pane

The plugin adds a pane called **Orchestrator tasks**. It shows the tasks that the orchestrator tracks for the project of the session.

- It opens when a session starts. Run `/orch-tasks` to open it and refresh it at any time.
- It refreshes every 2 minutes. It also refreshes right after Claude changes a Work task, or runs a `gh` command that changes an issue or a Project item.
- If no source can be read, the pane shows an error line, keeps the last list and tries again.

### Sources

A source is a tracker that the pane can read. The pane tries the sources of the project in order and shows the first one that answers.

| Source | What it reads | What it needs |
|---|---|---|
| `work` | The open subtasks of each task titled `Claude orchestrator: <project>` in the Tasks tab of the Work app, sorted by priority and then by age. | The `Work` MCP server, connected. |
| `github` | The issues and pull requests of this repository on the owner's GitHub Projects, grouped by the `Status` field. | The `gh` CLI, signed in with the `project` scope. |

A project with no configuration file gets `work` first and `github` second.

### Configuration per project

To choose the sources of a project, add `.claude/orchestrator-tasks.json` at the root of its repository:

```json
{
  "sources": [
    { "type": "github", "owner": "my-org", "projects": ["Core", "Shop"], "statuses": ["Doing", "Review"] }
  ]
}
```

- `sources`: the sources to try, in order. List one source to turn the fallback off.
- `owner` (`github`): the user or organization that owns the Projects. The default is the owner of the `origin` remote.
- `projects` (`github`): the titles of the Projects to read. The default is every open Project of the owner. A title that does not exist is an error.
- `statuses` (`github`): the `Status` values to show, in the order drawn. The default is `Doing` and `Review`.

The `github` source shows only items from the repository of the session. It skips draft items.

If the file has a mistake, the pane shows the mistake and reads no source. It does not fall back to the defaults.

Version 2.0.0 removed the pane because it read only Work. Version 2.7.0 brings it back with sources per project.

## Updates

Each plugin sets a `version` in its `plugin.json`. An install stays on its copy until that version changes, so bump it with every release.

- **Automatic:** run `/plugin`, open **Marketplaces**, select `rizo-ai-skills`, and choose **Enable auto-update**.
- **Manual:** `claude plugin marketplace update rizo-ai-skills`, then `claude plugin update orchestrator@rizo-ai-skills`.
- In a running session, apply an update with `/reload-plugins`.

## Add a plugin

1. Create `plugins/<name>/skills/<name>/SKILL.md`.
2. Create `plugins/<name>/.claude-plugin/plugin.json` with `name`, `version` and `description`. Set the version there only, not in `marketplace.json`.
3. Add an entry to `.claude-plugin/marketplace.json` with the same `name` and `"source": "./plugins/<name>"`.
4. Run `claude plugin validate .` before you commit.
