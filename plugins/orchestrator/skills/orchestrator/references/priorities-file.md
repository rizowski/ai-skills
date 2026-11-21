# Migrating a long priorities file

Read this when the sweep says that the priorities file is over its limit of 40 lines.
Do the migration once, at that launch.

Why: the file is read at every launch and often during a session. Dated notes and history
make each read cost more, and they go stale while the tracker moves on.

1. Tell me in the launch report that the file is over the limit and that you will move
   its history into the tracker. Do not wait for an answer unless a note looks like a
   decision that is not in any task.
2. For each priority, keep the label, a one-line goal and the task ids.
3. Move each dated note, state note or history line that is still useful into a comment
   on its task. Drop a note that the tracker already holds, or that is no longer true.
4. Move each step that waits on me into "Waiting on you", one line per step, with its
   kind (phone check, merge, deploy or migration, decision, close) and task id.
5. A note that belongs to no task: create a task for it, or ask me once whether to drop
   it.
6. Write the new file in the shape that the skill gives, and set "Last updated".
7. Run the sweep again and check that it no longer warns.
