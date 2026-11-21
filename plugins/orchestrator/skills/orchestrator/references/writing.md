# Picking the writing skill

Read this before the first report of a session.

Write every summary, status report and decision request with a writing skill. Use the
first one in this list that is installed:

1. `simple-english` (Plain mode). Skill tool name: `simple-english:simple-english`.
   File: `~/.claude/plugins/marketplaces/simple-english/skills/simple-english/SKILL.md`.
2. `i-have-adhd`. Skill tool name: `i-have-adhd:i-have-adhd`.
   File: `~/.claude/plugins/marketplaces/i-have-adhd/skills/i-have-adhd/SKILL.md`.

Use one skill, not both. Load it before the first report and follow it for the rest of the
session. Try the Skill tool first. If the skill is not in the session's skill list, or the
Skill tool refuses it (a skill can turn off model invocation), read its `SKILL.md` at the
path above and follow that. A skill counts as installed when either way works.

If neither skill is installed, say so once in the launch report. Then write short, plain
reports: the action I must take first, one idea per sentence, no filler.

This covers what I read: the launch report, worker summaries, decision requests and the
end-of-round summary. Worker briefs are exempt.
