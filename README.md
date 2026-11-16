# ai-skills

My skills for AI coding agents, packaged as a Claude Code plugin marketplace.

| Plugin | What it does |
|---|---|
| [orchestrator](plugins/orchestrator/skills/orchestrator/SKILL.md) | Runs a Claude Code session as an orchestrator: it manages worker subagents against a priorities file, does no task work itself, and shows its open Work tasks in a pane. |

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

The orchestrator plugin also adds a pane called **Orchestrator tasks**. It shows the open tasks the orchestrator is tracking.

The tasks live in the Tasks tab of the Work app. Each project has one parent task titled `Claude orchestrator: <project>`, with one subtask for each high-level request. The pane lists the open subtasks of every such parent task, sorted by priority and then by age. Blocked tasks show a mark.

- It needs the `Work` MCP server to be connected. If it is not, the pane shows an error line and tries again.
- It opens when a session starts. Run `/orch-tasks` to open it and refresh it at any time.
- It refreshes every 2 minutes, and right after Claude creates, updates or deletes a Work task.
- If a refresh fails, the pane keeps the last good list and shows the error.
- It is read-only. It never changes a task.

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
