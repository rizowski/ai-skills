# ai-skills

My skills for AI coding agents, packaged as a Claude Code plugin marketplace.

| Plugin | What it does |
|---|---|
| [orchestrator](plugins/orchestrator/skills/orchestrator/SKILL.md) | Runs a Claude Code session as an orchestrator: it manages worker subagents against a priorities file, and does no task work itself. |

## Agents

The plugin ships two lean subagent types. They start with far less context than `general-purpose`, which keeps long orchestrator sessions cheaper.

| Agent | Model | Use it for |
|---|---|---|
| `orchestrator:scout` | haiku | Read-only lookups in a repo or on GitHub. No project instructions, no MCP tools, no edits. |
| `orchestrator:worker` | sonnet | A code task driven to an open PR. Keeps the repo's instructions. No MCP tools. |

Use `general-purpose` or a project specialist when a task needs MCP tools (cloud accounts, trackers, browsers).

## Recommended settings

These are optional. Each name is in the Claude Code docs. Nothing here is changed for you.

- `CLAUDE_CODE_SUBAGENT_MODEL`: environment variable that sets the model for subagents that do not set their own. Set `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` as well to force it onto every subagent, which overrides the `model` field of these agents.
- `subagentPromptCacheTtl`: setting, `"5m"` or `"1h"`. It picks how long the prompt cache holds subagent requests. The default for subagents is 5 minutes. Use `"1h"` if you often send follow-ups to a worker after a pause.
- Turn off MCP servers and connectors you do not use. Their tool definitions load into every agent that inherits tools.
- Measure with `/usage` (session cost and cache hit rate) and `/context` (what fills the context window).

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

Version 2.0.0 removed the **Orchestrator tasks** pane and its `/orch-tasks` command. The pane read one tracker's tasks, so it now ships with the project that uses that tracker.

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
