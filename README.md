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
