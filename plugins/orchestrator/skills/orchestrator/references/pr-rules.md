# PR rules: evidence, step labels, review ask

When you brief a worker on a visible change, or when a PR changes step, read this.

## Evidence location

Evidence images and videos are never committed on the PR branch and never appear in the PR
diff. They are only displayed in the PR body.

Each PR gets its own orphan branch, `pr-evidence-<pr number>`. One branch per PR, because
workers cannot share one evidence branch: only one worktree can check out a branch, and a
push guard can allow only `git push origin <branch>`.

1. Open the PR first, to get its number.
2. Make a second worktree outside the repo:
   `git worktree add --detach ../<repo>-evidence-<pr>`, then in it
   `git switch --orphan pr-evidence-<pr>`.
3. Copy the files in, commit them, and run `git push origin pr-evidence-<pr>`.
4. Embed each image in the PR body as
   `![name](https://github.com/<owner>/<repo>/blob/pr-evidence-<pr>/<file>?raw=true)`.
   Link a video as `[name](same URL)`.
5. Show before and after side by side in a two-column table, before on the left.
6. When done, remove the evidence worktree. Leave the branch on the remote.

Check: `gh pr diff <n> --name-only` lists no image or video file.

## Step labels

An open PR that the orchestrator or a worker still works on carries exactly one `step:`
label that names the current step:

- `step: building`
- `step: code review`
- `step: fixing review`
- `step: design pass`
- `step: waiting on merge order`

Create a missing label with `gh label create "<name>" --force`. Change the label at each
transition (worker start, review start, fix start, done):
`gh pr edit <n> --remove-label "<old>" --add-label "<new>"`.

## Ask for review

When nothing is left to do on a PR, swap its step label for `ready for review`.
In the next message, ask me to review it, with the full PR URL. This is a "review" line in
"Waiting on you".

If the PR author is my own account, GitHub cannot request my review, so the label and the
chat ask replace it. Otherwise also run `gh pr edit <n> --add-reviewer <user>`.

## Watch after the ask

A PR labeled `ready for review` is not finished. Watch it until it merges or closes.

- Start a background poll, for example every 3 minutes:
  `gh pr list --label "ready for review" --json number,state,mergeable,statusCheckRollup`.
  Make it exit on a merge conflict (`mergeable` is `CONFLICTING`), a failed check, or a
  merge. The exit wakes you.
- When event subscriptions are available, prefer `gh event listen --resource <PR URL>
  --event com.github.pull_request.state.changed.v1`.
- On a conflict or a failed check, send a fix worker at once. Do not wait for me. Swap the
  label back to a `step:` label, usually `step: fixing review`.
- When the fix is done, swap to `ready for review` and ask me to review again.
- After you handle an exit, start the poll again.
