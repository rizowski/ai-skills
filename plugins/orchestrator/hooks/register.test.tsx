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

const start = async (...[$, on]: Parameters<TestBody>) => {
  on('command.register', () => ({ value: { command: 'orch-tasks' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
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
    expect(header?.text).toMatch(/^5 open · refreshed \d\d:\d\d$/)

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
  expect((await ui.find({ type: 'Text', text: /Could not read Work tasks/ }))?.text).toBeDefined()
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
  on('mcp.call', () => ({ value: { content: [{ type: 'text', text: 'not json at all' }], isError: false } }))
  await start($, on)

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /Could not read/ }))?.text).toBe(
    'Could not read Work tasks (no task list; blocks: text(15) | structured: no | starts: "not json at all"). Retrying in 2 min.',
  )
  expect(writes).toHaveLength(1)
  expect(writes[0]?.path).toBe('/home/someone/.claude/dev-mods/orchestrator-tasks-diagnostics/last-parse-failure.json')
  expect(JSON.parse(writes[0]?.text ?? '{}').shape.blocks[0]).toEqual({ type: 'text', length: 15, head: 'not json at all' })
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
