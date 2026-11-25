import { expect, mock, test } from 'claude-code/testing'
import type { TestBody } from 'claude-code/testing'
import type { McpToolResult } from 'claude-code'

const PANE = {
  plugin: 'orchestrator',
  component: 'Pane',
  requestId: 'orchestrator-tasks',
  props: {
    title: 'Orchestrator tasks',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const PARENT = 'parent-task'

const task = (over: Record<string, unknown>) => ({
  id: String(over.title),
  priority: null,
  done: false,
  isBlocked: false,
  parentTaskId: PARENT,
  createdAt: '2026-11-01T00:00:00Z',
  description: 'long description that must never be drawn',
  ...over,
})

const TASKS = [
  { id: PARENT, title: 'Claude orchestrator: example', priority: null, done: false, isBlocked: false, parentTaskId: null, createdAt: '2026-10-01T00:00:00Z' },
  task({ title: 'Low one', priority: 'Low' }),
  task({ title: 'High newer', priority: 'High', createdAt: '2026-11-05T00:00:00Z' }),
  task({ title: 'High older', priority: 'High', createdAt: '2026-11-02T00:00:00Z' }),
  task({ title: 'Urgent blocked', priority: 'Urgent', isBlocked: true }),
  task({ title: 'No priority' }),
  task({ title: 'Done urgent', priority: 'Urgent', done: true }),
  task({ title: 'Unrelated child', priority: 'Urgent', parentTaskId: 'some-other-parent' }),
  { id: 'x', title: 'Unrelated top level', priority: 'High', done: false, isBlocked: false, parentTaskId: null, createdAt: '2026-11-01T00:00:00Z' },
]

const answer = (tasks: unknown[]) => ({
  value: { content: [{ type: 'text', text: JSON.stringify({ tasks }) }], isError: false },
})

const GH_PROJECTS = {
  projects: [
    { number: 1, title: 'Core', closed: false },
    { number: 2, title: 'Shop', closed: false },
    { number: 3, title: 'Old', closed: true },
  ],
}

const ghItem = (number: number, status: string | null, repository = 'acme/shop') => ({
  status,
  title: `Issue ${number}`,
  content: { number, title: `Issue ${number}`, repository, type: 'Issue', body: 'long body that must never be drawn' },
})

const GH_ITEMS: Record<string, unknown> = {
  '1': { items: [ghItem(12, 'Review'), ghItem(7, 'Doing'), ghItem(9, 'Todo'), ghItem(30, 'Doing', 'acme/other')] },
  '2': { items: [ghItem(5, 'Review'), ghItem(8, null), { status: 'Doing', title: 'A draft', content: { type: 'DraftIssue', title: 'A draft' } }] },
}

/** Stands for the `gh` CLI: answers the project list and each project's items, and records each call. */
const gh = (on: Parameters<TestBody>[1]) => {
  const calls: string[] = []
  on('process.run', (_, e) => {
    calls.push(e.argv.join(' '))
    const stdout =
      e.argv[2] === 'list' ? JSON.stringify(GH_PROJECTS) : JSON.stringify(GH_ITEMS[e.argv[3] ?? ''] ?? { items: [] })

    return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })

  return calls
}

/**
 * Starts a session with the pane loaded. `config` is the text of the
 * project's config file; the project has no such file when it is undefined.
 */
const start = async ($: Parameters<TestBody>[0], on: Parameters<TestBody>[1], config?: string) => {
  on('fs.exists', (_, e) => ({ value: config !== undefined && e.path === '/repo/.claude/orchestrator-tasks.json' }))
  if (config !== undefined) on('fs.read', () => ({ value: config }))
  on('command.register', () => ({ value: { command: 'orch-tasks' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.repo', () => ({ value: { root: '/repo', remote: 'git@github.com:acme/shop.git', internal: false, name: 'acme/shop' } }))
  const clock = mock.clock(on, { now: Date.UTC(2026, 10, 16, 12, 0) })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()

  return clock
}

const rowTexts = async (ui: { find: (q: { type: 'Text'; text: RegExp }) => Promise<{ text?: string } | undefined> }, titles: string[]) =>
  Promise.all(titles.map(async t => (await ui.find({ type: 'Text', text: new RegExp(t) }))?.text))

test('lists open children of the orchestrator parent, sorted by priority then age', async ($, on) => {
  on('mcp.call', () => answer(TASKS))
  await start($, on)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const header = await ui.find({ type: 'Text', text: /open/ })
    expect(header?.text).toMatch(/^5 open · Work · refreshed \d\d:\d\d$/)

    const rows = await rowTexts(ui, ['Urgent blocked', 'High older', 'High newer', 'Low one', 'No priority'])
    expect(rows).toEqual([
      'URG  ⊘ Urgent blocked',
      'HIGH High older',
      'HIGH High newer',
      'LOW  Low one',
      '--   No priority',
    ])
    expect(await ui.find({ type: 'Text', text: /Done urgent|Unrelated|long description/ })).toBeUndefined()
    await ui.unmount()
  }
})

test('says so in one line when no orchestrator parent exists', async ($, on) => {
  on('mcp.call', () => answer([TASKS[8]]))
  await start($, on)

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /orchestrator/ }))?.text).toBe('No "Claude orchestrator:" task in Work.')
  expect((await ui.find({ type: 'Text', text: /open/ }))?.text).toMatch(/^0 open/)
  await ui.unmount()
})

test('a failed refresh keeps the last good list and shows an error line', async ($, on) => {
  let isFailing = false
  on('mcp.call', () => {
    if (isFailing) throw new Error('server not connected')

    return answer(TASKS)
  })
  const clock = await start($, on)

  isFailing = true
  await clock.advance(2 * 60 * 1000)

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Could not read tasks/ }))?.text).toBeDefined()
  expect((await ui.find({ type: 'Text', text: /High older/ }))?.text).toBe('HIGH High older')
  await ui.unmount()

  isFailing = false
  await clock.advance(2 * 60 * 1000)
  const after = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await after.find({ type: 'Text', text: /Could not read/ })).toBeUndefined()
  await after.unmount()
})

test('a result that does not parse is an error with a shape summary, and the full shape is written', async ($, on) => {
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_, e) => {
    writes.push({ path: e.path, text: e.text })

    return { value: undefined }
  })
  on('env.get', () => ({ value: '/home/someone' }))
  on('process.run', () => ({
    value: { exitCode: 1, stdout: '', stderr: 'gh: not signed in\nmore', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: 'not json at all' }], isError: false } }))
  await start($, on)

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Could not read/ }))?.text).toBe(
    'Could not read tasks (Work: no task list; blocks: text(15) | structured: no | starts: "not json at all"; GitHub: gh project list failed: gh: not signed in). Retrying in 2 min.',
  )
  expect(writes).toHaveLength(1)
  expect(writes[0]?.path).toBe('/home/someone/.claude/dev-mods/orchestrator-tasks-diagnostics/last-parse-failure.json')
  expect(JSON.parse(writes[0]?.text ?? '{}').failures[0].shape.blocks[0]).toEqual({ type: 'text', length: 15, head: 'not json at all' })
  expect((await ui.find({ type: 'Text', text: /Loading/ }))?.text).toBe('Loading…')
  await ui.unmount()
})

test('a Work task write refreshes the pane and passes the result through', async ($, on) => {
  let calls = 0
  on('mcp.call', () => {
    calls += 1

    return answer(TASKS)
  })
  on('tool.call', () => ({ result: { ok: true } }))
  const clock = await start($, on)
  const before = calls

  const ran = await $.tool.call({ tool: 'mcp__Work__update_task', id: 'x', done: true })
  await clock.settle()

  expect(ran).toEqual(expect.objectContaining({ result: { ok: true } }))
  expect(calls).toBe(before + 1)
})

const loadsWith = (name: string, value: () => McpToolResult, extra?: (on: Parameters<TestBody>[1]) => void) =>
  test(`reads the task list from ${name}`, async ($, on) => {
    on('mcp.call', () => ({ value: value() }))
    extra?.(on)
    await start($, on)

    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    expect(await ui.find({ type: 'Text', text: /Could not read/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /open/ }))?.text).toMatch(/^5 open/)
    expect((await ui.find({ type: 'Text', text: /High older/ }))?.text).toBe('HIGH High older')
    await ui.unmount()
  })

const JSON_TEXT = JSON.stringify({ tasks: TASKS })
const text = (t: string) => ({ type: 'text', text: t })

loadsWith('text split across several blocks', () => ({
  content: [text(JSON_TEXT.slice(0, 100)), text(JSON_TEXT.slice(100, 400)), text(JSON_TEXT.slice(400))],
  isError: false,
}))
loadsWith('a double-encoded text block', () => ({ content: [text(JSON.stringify(JSON_TEXT))], isError: false }))
loadsWith('text with a prefix and suffix', () => ({
  content: [text(`Tasks for you:\n${JSON.stringify({ tasks: TASKS }, null, 2)}\n(end)`)],
  isError: false,
}))
loadsWith('a top-level array in text', () => ({ content: [text(JSON.stringify(TASKS))], isError: false }))
loadsWith('structuredContent as the array', () => ({ content: [], structuredContent: TASKS, isError: false }))
loadsWith('structuredContent under data', () => ({ content: [text('see structured')], structuredContent: { data: TASKS }, isError: false }))
loadsWith(
  'a saved-to-file notice',
  () => ({
    content: [
      text('Error: result (71,908 characters across 1,822 lines) exceeds maximum allowed tokens. Output has been saved to /tmp/out/tasks.txt'),
    ],
    isError: false,
  }),
  on =>
    on('fs.read', (_, e) => {
      if (e.path !== '/tmp/out/tasks.txt') throw new Error('ENOENT')

      return { value: JSON.stringify([text(JSON_TEXT)]) }
    }),
)

test('a saved-to-file notice the mod cannot read says so', async ($, on) => {
  on('mcp.call', () => ({
    value: { content: [text('Output has been saved to /secret/tasks.txt')], isError: false },
  }))
  on('fs.read', () => ({ deny: 'EACCES: permission denied' }))
  await start($, on)

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Could not read/ }))?.text).toMatch(
    /result saved to a file the mod cannot read \(.*EACCES.*\)/,
  )
  await ui.unmount()
})

const NOT_CONNECTED = () => {
  throw new Error('no connected MCP tool "list_my_tasks" on a server named "Work"')
}

test('falls back to GitHub Projects when Work does not answer', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  const calls = gh(on)
  await start($, on)

  expect(calls).toEqual([
    'gh project list --owner acme --format json --limit 100',
    'gh project item-list 1 --owner acme --format json --limit 500',
    'gh project item-list 2 --owner acme --format json --limit 500',
  ])
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /GitHub/ }))?.text).toMatch(
      /^3 in Doing or Review · GitHub · refreshed \d\d:\d\d$/,
    )
    expect(await rowTexts(ui, ['Issue 7', 'Issue 5', 'Issue 12'])).toEqual(['#7 Issue 7', '#5 Issue 5', '#12 Issue 12'])
    expect((await ui.find({ type: 'Text', text: /^Doing$/ }))?.text).toBe('Doing')
    expect((await ui.find({ type: 'Text', text: /^Review$/ }))?.text).toBe('Review')
    expect(await ui.find({ type: 'Text', text: /Issue 9|Issue 30|Issue 8|draft|long body|Could not read/ })).toBeUndefined()
    await ui.unmount()
  }
})

test('the project config file picks the sources, the owner, the projects and the statuses', async ($, on) => {
  let workCalls = 0
  on('mcp.call', () => {
    workCalls += 1

    return answer(TASKS)
  })
  const calls = gh(on)
  await start($, on, JSON.stringify({ sources: [{ type: 'github', owner: 'other-org', projects: ['Shop'], statuses: ['Review'] }] }))

  expect(workCalls).toBe(0)
  expect(calls).toEqual([
    'gh project list --owner other-org --format json --limit 100',
    'gh project item-list 2 --owner other-org --format json --limit 500',
  ])
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /GitHub/ }))?.text).toMatch(/^1 in Review · GitHub/)
  expect(await ui.find({ type: 'Text', text: /Issue 7|Issue 12/ })).toBeUndefined()
  await ui.unmount()
})

test('a config file with a mistake is an error line, and no source is read', async ($, on) => {
  let workCalls = 0
  on('mcp.call', () => {
    workCalls += 1

    return answer(TASKS)
  })
  await start($, on, JSON.stringify({ sources: [{ type: 'kanzo' }] }))

  expect(workCalls).toBe(0)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Could not read/ }))?.text).toBe(
    'Could not read .claude/orchestrator-tasks.json (sources[0].type must be "work" or "github"). Retrying in 2 min.',
  )
  await ui.unmount()
})

test('a configured Project title that does not exist is an error', async ($, on) => {
  gh(on)
  await start($, on, JSON.stringify({ sources: [{ type: 'github', projects: ['Missing'] }] }))

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Could not read/ }))?.text).toBe(
    'Could not read tasks (GitHub: acme has no open Project titled "Missing"). Retrying in 2 min.',
  )
  await ui.unmount()
})

test('a gh command that moves an issue refreshes the pane, and another command does not', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  const calls = gh(on)
  on('tool.call', () => ({ result: { ok: true } }))
  const clock = await start($, on)
  const before = calls.length

  await $.tool.call({ tool: 'Bash', command: 'ls' })
  await clock.settle()
  expect(calls.length).toBe(before)

  await $.tool.call({ tool: 'Bash', command: 'gh project item-edit 2 --owner acme --url u --field Status --value Doing' })
  await clock.settle()
  expect(calls.length).toBe(before + 3)
})

const CACHE = '/home/someone/.claude/dev-mods/orchestrator-tasks-cache'
const CACHE_FILE = `${CACHE}/github-acme__shop_____Doing,Review.json`
const MINUTE = 60 * 1000

/**
 * Stands for the shell and `gh` of a machine whose home is `/home/someone`:
 * `files` holds the cache and lock files, `limit` is the GraphQL points left,
 * `isFailing` makes every `gh project` command fail with a rate limit error.
 * Records each `gh project` call in `calls`.
 */
const sharedGh = (on: Parameters<TestBody>[1], files: Map<string, string>, state: { remaining: number; isFailing: boolean }) => {
  const calls: string[] = []
  on('env.get', () => ({ value: '/home/someone' }))
  on('process.run', (_, e) => {
    const [cmd, ...rest] = e.argv
    const done = (stdout: string, exitCode = 0, stderr = '') => ({
      value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false },
    })
    if (cmd === 'cat') {
      const text = files.get(rest[0] ?? '')

      return text === undefined ? done('', 1) : done(text)
    }
    if (cmd === 'rm') {
      files.delete(rest[1] ?? '')

      return done('')
    }
    if (cmd === 'sh') {
      const [, , script = '', , file = '', text = ''] = e.argv
      if (script.includes('set -C')) {
        if (files.has(file)) return done('held\n')
        files.set(file, text)

        return done('taken\n')
      }
      files.set(file, text)

      return done('')
    }
    if (rest[0] === 'api') {
      return done(JSON.stringify({ limit: 5000, remaining: state.remaining, used: 5000 - state.remaining, reset: Date.UTC(2026, 10, 16, 13, 0) / 1000 }))
    }
    calls.push(e.argv.join(' '))
    if (state.isFailing) return done('', 1, 'GraphQL: API rate limit already exceeded for user ID 1')

    return done(rest[1] === 'list' ? JSON.stringify(GH_PROJECTS) : JSON.stringify(GH_ITEMS[rest[2] ?? ''] ?? { items: [] }))
  })

  return calls
}

const FULL_SCAN = [
  'gh project list --owner acme --format json --limit 100',
  'gh project item-list 1 --owner acme --format json --limit 500',
  'gh project item-list 2 --owner acme --format json --limit 500',
]

test('one GitHub read serves every tick for ten minutes, then reads only the Projects that held this repository', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  const files = new Map<string, string>()
  const calls = sharedGh(on, files, { remaining: 4000, isFailing: false })
  const clock = await start($, on)
  expect(calls).toEqual(FULL_SCAN)

  await clock.advance(8 * MINUTE)
  expect(calls).toHaveLength(3)
  expect(files.has(`${CACHE_FILE}.lock`)).toBe(false)

  await clock.advance(4 * MINUTE)
  expect(calls.slice(3)).toEqual([
    'gh project item-list 1 --owner acme --format json --limit 500',
    'gh project item-list 2 --owner acme --format json --limit 500',
  ])
})

test('a session that finds a fresh shared cache reads nothing from GitHub', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  const loaded = {
    label: 'GitHub',
    noun: 'in Doing or Review',
    emptyText: 'none',
    isAlwaysGrouped: true,
    groups: [{ id: 'Doing', title: 'Doing', tasks: [1, 2, 3].map(n => ({ id: `acme/shop#${n}`, title: `T${n}`, tag: `#${n}`, tone: 'subtle', isBlocked: false })) }],
  }
  const entry = { fetchedAt: Date.UTC(2026, 10, 16, 12, 0) - MINUTE, loaded, relevant: [1, 2], scannedAt: Date.UTC(2026, 10, 16, 12, 0) - MINUTE }
  const files = new Map<string, string>([[CACHE_FILE, JSON.stringify(entry)]])
  const calls = sharedGh(on, files, { remaining: 4000, isFailing: false })
  await start($, on)

  expect(calls).toEqual([])
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /GitHub/ }))?.text).toMatch(/^3 in Doing or Review · GitHub/)
  await ui.unmount()
})

test('a rate limit error stops every read until the back-off ends, and never retries at once', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  const files = new Map<string, string>()
  const state = { remaining: 4000, isFailing: true }
  const calls = sharedGh(on, files, state)
  const clock = await start($, on)
  expect(calls).toEqual(['gh project list --owner acme --format json --limit 100'])

  await clock.advance(20 * MINUTE)
  expect(calls).toHaveLength(1)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Not asking again until/ }))?.text).toMatch(/rate limit already exceeded/)
  await ui.unmount()

  state.isFailing = false
  await clock.advance(15 * MINUTE)
  expect(calls.slice(1)).toEqual(FULL_SCAN)
})

test('few GraphQL points left means no read before the reset', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  const files = new Map<string, string>()
  const state = { remaining: 120, isFailing: false }
  const calls = sharedGh(on, files, state)
  const clock = await start($, on)
  expect(calls).toEqual([])

  await clock.advance(30 * MINUTE)
  expect(calls).toEqual([])
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Only 120 GitHub GraphQL points/ }))?.text).toBeDefined()
  await ui.unmount()
})

test('a session waits for the lock that another session holds and reads nothing', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  const files = new Map<string, string>([[`${CACHE_FILE}.lock`, String(Date.UTC(2026, 10, 16, 12, 0))]])
  const calls = sharedGh(on, files, { remaining: 4000, isFailing: false })
  await start($, on)

  expect(calls).toEqual([])
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Another session is reading GitHub/ }))?.text).toBeDefined()
  await ui.unmount()
})

test('a lock file that is still empty is not stale, so the session does not take it', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  const files = new Map<string, string>([[`${CACHE_FILE}.lock`, '']])
  const calls = sharedGh(on, files, { remaining: 4000, isFailing: false })
  await start($, on)

  expect(calls).toEqual([])
  expect(files.get(`${CACHE_FILE}.lock`)).toBe('')
})

test('a gh write right after a read does not read again, and a write after 30 seconds does', async ($, on) => {
  on('mcp.call', NOT_CONNECTED)
  on('tool.call', () => ({ result: { ok: true } }))
  const files = new Map<string, string>()
  const calls = sharedGh(on, files, { remaining: 4000, isFailing: false })
  const clock = await start($, on)
  expect(calls).toHaveLength(3)

  const edit = 'gh project item-edit 2 --owner acme --url u --field Status --value Doing'
  await $.tool.call({ tool: 'Bash', command: edit })
  await clock.settle()
  expect(calls).toHaveLength(3)

  await clock.advance(MINUTE)
  await $.tool.call({ tool: 'Bash', command: edit })
  await clock.settle()
  expect(calls).toHaveLength(6)
})
