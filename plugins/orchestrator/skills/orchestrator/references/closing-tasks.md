# Asking me to close tasks

Read this the first time in a session that you ask me to close tasks.

A task is ready to close when all of its PRs are merged, or closed on purpose and
replaced. Check this against the PR, not against a worker's claim.

## The question

- Ask in the same message as your next report, without waiting for me to ask.
- If a merged task still waits on a step of mine (a deploy, an apply, a migration, a
  device check, a decision), ask anyway and name the step, so I can close it or keep it.
  The step is also a "Waiting on you" line of kind "close".
- Use one multi-select question. Each option is one task: the label is a short task name,
  and the description gives its merged PRs and what is still left. I must be able to
  decide from the dialog alone, because the report may not be on screen while it is open.
- Put the task that you recommend closing first and mark it as recommended. If you
  recommend that I keep everything open, say so in the question text, because nothing
  ticked means "close none".
- The tool allows 4 options per question and 4 questions per call. With more than 4 tasks,
  split them across questions, the most ready first. With more than 16, ask about the 16
  most ready and list the rest under "Ready to close" in the report.

## The answer

- The tasks I tick are the yes. Mark only those done. Unticked tasks stay open.
- Ask once per task. If I do not answer, list the task under "Ready to close" in later
  reports, and do not ask again each turn. If I keep a task open, do not ask again unless
  something changes.
- A yes for a listed set covers that set only.
- After a yes: write small leftovers (an optional cleanup, a known gap, a reminder) into
  the task text or their own task, mark the task done, remove its finished worktrees when
  the repo's rules allow, and update or remove its entry and its "Waiting on you" lines in
  the priorities file.
- Never close a task that I made outside your parent task or the priorities file.
