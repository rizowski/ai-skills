# vb CLI facts (`@vercel/board` 0.54.0)

Captured from `npm i -g @vercel/board@0.54.0` in a `node:22` (22.23.5) container, and
tested against a throwaway `vb serve` in the same container as UID 1000. This is the source
for the board-agents scripts. Where this file and `DESIGN.md` differ, this file wins.

## Findings that change the design

- `vb` reads `BOARD_URL` itself (`vb serve --help` says so; tested). `--url` is not needed
  when `BOARD_URL` is set. `vb` is the same binary as `board`.
- The list command is `vb tasks [channel] [--state S] [--label k=v]`, not `tasks list`.
  `--state` takes waiting, ready, claimed, blocked, completed, failed, canceled, open, all.
- Block kinds are `needs_input` and `awaiting_approval`. There is no `decision` or
  `approval` kind. Map: decision -> `needs_input`, final review -> `awaiting_approval`.
- Create: `tasks create <channel> "<text>" --label step=work --needs <id>`. The channel is
  a positional and `--label` routes. `claim --ready <channel> --label k=v` filters on both.
  `--json` prints `.task.id`. Ids are numeric strings and not sequential.
- Identity: `--as <profile>` names the claimer (`local:user:node+worker-1`). `block`,
  `renew`, `complete`, `fail` and `release` must pass the same `--as` that claimed.
  `unblock`, `reopen`, `needs` and `cancel` are owner actions and need no claim. Every
  caller is the same actor `local:user:<os user>`, so any container may call them.
- `block` ends the claim. The block reason is stored on the task
  (`.task.task.blocked = {kind, reason}`), not as a thread reply. The runner must also
  `reply` a marker so the relay can order replies.
- Read a thread: `vb read <id> --json` gives `{message, replies:[{id,text,createdAt,...}]}`.
  Reply ids grow; compare them as numbers.
- Post a comment: `vb reply <id> "<text>" [--as name]`.
- `vb tasks --json` gives `{tasks:[{message:{id,channel,text,labels,task:{state,blocked,...}},replies,last}]}`.
- `fail <id> <reason>` needs the claim. `tasks run` fails the task by exit code.
- `tasks run <id|channel> [--ready --label k=v] [--lease D] [--as N] [--result T] -- <cmd>`
  claims, renews while the command runs, completes on exit 0 and fails otherwise. The
  command gets env `BOARD_TASK` (the id). It does not pass the text; read it with `vb read`.
- Lease: `--lease` 30s to 1h, default 15m.
- Errors (`--json`, exit 1): `{"status":"error","reason":...}`. Seen: `nothing_ready`
  (normal for a loop), `network_error` (board down), `no_project` (token file missing),
  `task_claimed`. Text mode: "You hold no claim on task #N as <name>" (exit 1).
  Retry on `network_error`, `no_project` and HTTP 401; do not retry the rest.
- Claim state lives under `~/.config/board/claims/` (the shared token dir). Keep that dir.

## Help output

### `vb tasks create`

```text
usage: board tasks create <channel|id> [text] [options]

start a thread as a task, or with an ID, make that thread one: you own it. With
--file, a plan of them

arguments:
  channel|id  a channel, or a thread's ID to make a task of it as it is
  text        what to do, for a new thread: - reads it from stdin, and --file
              takes its place

options:
  --label <k=v>...         label it, to route it
  --needs <id>...          wait for a task to complete first
  --for <profile>          only this profile of yours claims it
  --claim                  claim it in the same write
  --lease <duration>       with --claim: how long to hold it
  --file <path>            a plan, from a JSON file (- reads stdin): an array of
                           tasks, each { key?, text, labels?, needs?, for?,
                           noSync?, attachments? }, all made, in as many writes
                           as it takes
  --no-sync                keep it on the board: sync it nowhere
  --idempotency-key <key>  sending the same one again makes it once

examples:
  hand a fix to whoever takes fixes
    board tasks create work "fix CVE-123 in acme/web" --label step=fix

  a review that waits for it
    board tasks create work "review the fix for CVE-123" --label step=review --needs 812

  make a task of a thread that is there
    board tasks create 812

  a plan of tasks that wait for each other
    board tasks create work --file plan.json --idempotency-key migration-7

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks claim`

```text
usage: board tasks claim <id|channel> [options]

hold a task for a lease, by its ID, or with --ready the oldest ready one in a
channel

arguments:
  id|channel  a task's ID, or with --ready a channel or a path prefix (work/)

options:
  --ready             the oldest ready task in a channel, or under a prefix
                      (work/)
  --label <k=v>...    with --ready: one whose root has this label, one value
                      exactly
  --from <id>...      with --ready: take this owner's tasks too, by their ID
  --wait <duration>   with --ready: how long to wait for one, e.g. 10m
  --lease <duration>  how long to hold it: 30s to 1h (default: 15m, or what the
                      board gives)

examples:
  claim a task
    board tasks claim 812

  claim the next fix, waiting for one
    board tasks claim --ready work/ --label step=fix --wait 10m

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks run`

```text
usage: board tasks run <id|channel> <cmd>... [options]

claim a task and run a command on it: renew while it runs, complete on exit 0,
fail otherwise

arguments:
  id|channel  a task's ID, or with --ready a channel or a path prefix
  cmd         the command, after --: it gets BOARD_TASK, the task's ID

options:
  --result <text>     what it came to, when the command exits 0
  --ready             the oldest ready task in a channel, or under a prefix
                      (work/)
  --label <k=v>...    with --ready: one whose root has this label, one value
                      exactly
  --from <id>...      with --ready: take this owner's tasks too, by their ID
  --wait <duration>   with --ready: how long to wait for one, e.g. 10m
  --lease <duration>  how long to hold it: 30s to 1h (default: 15m, or what the
                      board gives)

examples:
  run the tests as a task
    board tasks run 812 --result "tests pass" -- make test

  work the next fix
    board tasks run --ready work/ --label step=fix -- ./fix.sh

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks renew`

```text
usage: board tasks renew <id> [options]

extend the lease of a task you hold, from now

arguments:
  id  a task's ID, as tasks shows it (#812)

options:
  --lease <duration>  how long to hold it: 30s to 1h (default: 15m, or what the
                      board gives)

examples:
  hold it for another 30 minutes
    board tasks renew 812 --lease 30m

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks release`

```text
usage: board tasks release <id>

give a task you hold back: it is ready again

arguments:
  id  a task's ID, as tasks shows it (#812)

examples:
  give a task back
    board tasks release 812

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks complete`

```text
usage: board tasks complete <id> [result]

finish a task you hold: what waits only for it is ready

arguments:
  id      a task's ID, as tasks shows it (#812)
  result  what it came to, in a line

examples:
  complete a task
    board tasks complete 812 "fixed in a1b2c3, tests pass"

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks fail`

```text
usage: board tasks fail <id> <reason>

give up on a task you hold: what waits for it keeps waiting, until it's reopened

arguments:
  id      a task's ID, as tasks shows it (#812)
  reason  why, in a line

examples:
  fail a task
    board tasks fail 812 "the tests need a database"

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks block`

```text
usage: board tasks block <id> <reason> [options]

hold a task for a person: with your claim, which it ends, or as its owner

arguments:
  id      a task's ID, as tasks shows it (#812)
  reason  what you need from them

options:
  --kind <kind>  what the hold is for: needs_input, awaiting_approval

examples:
  ask for approval
    board tasks block 813 "approve the prod deploy?" --kind awaiting_approval

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks unblock`

```text
usage: board tasks unblock <id>

lift a hold, as its owner: ready again, or waiting while a task it needs has not
completed

arguments:
  id  a task's ID, as tasks shows it (#812)

examples:
  lift a hold
    board tasks unblock 813

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks reopen`

```text
usage: board tasks reopen <id>

make a failed task ready again, as its owner

arguments:
  id  a task's ID, as tasks shows it (#812)

examples:
  retry a failed task
    board tasks reopen 812

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks needs`

```text
usage: board tasks needs <id> [needs]... [options]

set what a task waits for, as its owner: the tasks named, in place of what it
waited for

arguments:
  id     a task's ID, as tasks shows it (#812)
  needs  the tasks it waits for now, by ID

options:
  --none  wait for nothing

examples:
  wait for two tasks
    board tasks needs 814 812 813

  wait for nothing
    board tasks needs 814 --none

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb tasks cancel`

```text
usage: board tasks cancel <id> <reason>

cancel a task that has not completed, as its owner: any claim ends

arguments:
  id      a task's ID, as tasks shows it (#812)
  reason  why, in a line

examples:
  cancel a task
    board tasks cancel 812 "superseded by #820"

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb read`

```text
usage: board read [channel|id|link] [filters] [options]

the latest threads, one each (root, replies, latest reply), or one thread, every
message

arguments:
  channel|id|link  a channel (every channel without one, but not on Vercel's
                   board), a message's ID, or a Slack or GitHub link the board
                   synced

options:
  -n, --limit <N>   threads to list (default: 50, at most 500)
  --before <older>  a channel's threads before a page: the older its read
                    printed

filters:
  --re <id>             in a thread: its root's ID
  --by <handle>         by an author: a handle, me, or an actor ID
  --source <source>     from a source: a sync key such as slack, or board
  --label <k[=v|w]>...  with a label on the message or its thread's root
  --text <words>        containing this text, as one phrase (a substring, not
                        word search)
  --mentions [@handle]  mentioning a handle or email, or you without one
  --after <id>          only what came after a message or a cursor (read and
                        wait print one)
  labels match a message or its thread's root; repeated flags all must match
  Vercel's board reads one channel or thread here, with only --re, --label, and
  --after, or on a channel --mentions and --text, which look through one page of
  its threads: it keeps no search index

examples:
  list a channel's latest threads
    board read dev

  read one thread, every message
    board read 812

  list the open bugs
    board read dev --label kind=bug --label state=open

  what mentions you, before you wait for more: it prints the wait that goes on from there
    board read dev --mentions

  only what's new since the cursor an earlier read printed
    board read dev --after 1042

  the page before one, by the older it printed
    board read dev --before 1031.1024

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb reply`

```text
usage: board reply <id> [text] [options]

a reply in a thread

arguments:
  id    the ID of a message in the thread
  text  what it says: - reads it from stdin, and it may be left out when you
        --attach or --file

options:
  --label <k=v>...         label it
  --file <path>            the text, from a file: - reads stdin
  --schema <schema>        the text is JSON for this schema, such as sandbox@1
  --attach <file>...       attach a file: up to 10 per message, each up to 100
                           MB (4 MB on some boards)
  --idempotency-key <key>  sending the same one again posts once
  --no-sync                keep it on the board: sync it nowhere
  --after <id>             a reply only while this is the thread's newest
                           message

examples:
  reply in a thread
    board reply 812 "on it"

  reply with a label
    board reply 812 "fixed in a1b2c3" --label step=fix

  reply only if #815 is still the thread's newest
    board reply 812 "on it" --after 815

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb post`

```text
usage: board post <channel|id> [text] [options]

a new thread in a channel, or with an ID, a reply in its thread

arguments:
  channel|id  a channel, or a message ID to reply in its thread
  text        what it says: - reads it from stdin, and it may be left out when
              you --attach or --file

options:
  --label <k=v>...         label it
  --file <path>            the text, from a file: - reads stdin
  --schema <schema>        the text is JSON for this schema, such as sandbox@1
  --attach <file>...       attach a file: up to 10 per message, each up to 100
                           MB (4 MB on some boards)
  --sync <target>...       sync the new thread to a target too, e.g.
                           slack:#incidents (not on Vercel's board yet)
  --no-sync                keep it on the board: sync it nowhere
  --idempotency-key <key>  sending the same one again posts once
  --after <id>             a reply only while this is the thread's newest
                           message

examples:
  start a thread
    board post dev "deploys are green again"

  start a labeled thread
    board post dev "checkout times out" --label kind=bug

  post data that matches a schema
    board post deploys --schema sandbox@1 '{"id":"sbx-123","action":"started"}'

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb serve`

```text
usage: board serve [options]

run a board on this machine (127.0.0.1:4100, no sync) and open the inspector:
other commands reach it with --url or BOARD_URL. Its log lives in .board/, and
you're local:user:<your OS user>

options:
  --port <N>              the port to listen on (default: 4100)
  --dir <dir>             where the board keeps its log (default: .board/)
  --no-inspect            don't start the inspector
  --no-open               don't open the browser
  --allow-host <host>...  also answer requests for this host, from a proxy in
                          front: the board is at its URL there, and whoever
                          reaches the inspector there acts as you
  --tailscale <host>...   also answer requests for this host, the board's name
                          on your tailnet behind tailscale serve: whoever
                          Tailscale says is calling is a person on the board

examples:
  run a board here, and open it
    board serve

  share it on your tailnet, behind tailscale serve
    board serve --tailscale box.tailnet.ts.net

  serve it behind another proxy, on another name
    board serve --allow-host box.example.com

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb whoami`

```text
usage: board whoami

who am I, on which board, with which credential, and what I can do

examples:
  see who you are on the board
    board whoami

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb label`

```text
usage: board label <id> [k=v]... [options]

set labels on a message, or remove them

arguments:
  id   a message ID, as read shows it (#812)
  k=v  a label to set: a key of up to 64 lowercase letters, numbers, ., _, and
       -, starting with a letter or number, and a value of up to 256 characters

options:
  --unset <k>...     remove a label
  --if <k=v>...      only while the message has this label now
  --if-unset <k>...  only while the message doesn't have this label

examples:
  mark a bug fixed
    board label 812 state=fixed --unset wip

  claim a thread, unless someone has
    board label 812 owner=claude --if-unset owner

  move it on from open only
    board label 812 state=fixing --if state=open

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

### `vb help`

```text
usage: board help [command]...

this help, or a command's

global:   --json  --as <profile>  --scope <team>  --url <url>
          and the rest every command takes: board help
```

