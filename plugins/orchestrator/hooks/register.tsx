import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, Timer } from 'claude-code'

import type { OrchView, TaskGroup } from '../types'
import { clockTime, groupOrchestratorTasks, parseTasks, tagOf, TaskParseError, truncate } from './tasks'

const PANE = 'orchestrator-tasks'
const TITLE = 'Orchestrator tasks'
const SERVER = 'Work'
const REFRESH_MS = 2 * 60 * 1000
const WRITE_TOOLS = ['mcp__Work__create_task', 'mcp__Work__update_task', 'mcp__Work__delete_task'] as const

const EMPTY: OrchView = { groups: [], hasLoaded: false, refreshedAt: null, error: null }
const view = atom({ plugin: 'orchestrator', key: 'view' } as const, EMPTY, { shape: 'view-1' })

/**
 * Where a failed parse's full shape is written: a fixed folder under the
 * user's `~/.claude`, the same for every session and every install.
 */
export const diagnosticsPath = (home: string) =>
  `${home.replace(/\/+$/, '')}/.claude/dev-mods/orchestrator-tasks-diagnostics/last-parse-failure.json`

/**
 * Writes the full shape of a result that did not parse to the diagnostics file
 * and the debug log. Side effects: one file write (skipped when HOME is unset) and one
 * debug-log line; never throws.
 */
const recordFailure = async ($: EngineInterface, at: number, err: unknown) => {
  const report = {
    at: new Date(at).toISOString(),
    error: err instanceof Error ? err.message : String(err),
    shape: err instanceof TaskParseError ? err.shape : null,
  }
  try {
    $.ui.log(`orchestrator-tasks parse failure: ${JSON.stringify(report)}`, { to: 'debug' })
  } catch {
    // The debug log is best effort.
  }
  try {
    const home = await $.env.get('HOME')
    if (home) await $.fs.write(diagnosticsPath(home), `${JSON.stringify(report, null, 2)}\n`)
  } catch {
    // The file is best effort; the pane line still carries the summary.
  }
}

/**
 * Reads the tasks from Work and writes the pane's view. Never throws: a failed
 * call or a result that does not parse keeps the last good list, sets `error`,
 * and records the result's shape (see recordFailure).
 */
const refresh = async ($: EngineInterface) => {
  try {
    let groups: TaskGroup[] | undefined
    let error: string | null = null
    const now = await $.clock.now()
    try {
      const result = await $.mcp.call(SERVER, 'list_my_tasks', {})
      groups = groupOrchestratorTasks(await parseTasks(result, path => $.fs.read(path)))
    } catch (err) {
      const reason = err instanceof Error && err.message ? err.message : 'the call failed'
      error = `Could not read Work tasks (${reason}). Retrying in 2 min.`
      await recordFailure($, now, err)
    }
    await update($, view, prev => {
      const base = prev ?? EMPTY

      return groups
        ? { groups, hasLoaded: true, refreshedAt: now, error: null }
        : { ...base, error }
    })
  } catch {
    // The view could not be written either; the next tick tries again.
  }
}

let tick: Timer | undefined

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'orch-tasks',
      description: "Show the orchestrator's open tasks from Work in a pane",
    })
    tick?.cancel()
    try {
      tick = $.clock.every(REFRESH_MS, () => void refresh($))
    } catch {
      tick = undefined
    }
    void $.ui.open({ id: PANE, title: TITLE }).catch(() => undefined)
    void refresh($)

    return next(e)
  })

  on('command.run', { command: 'orch-tasks' }, async $ => {
    await $.ui.open({ id: PANE, title: TITLE })
    await refresh($)

    return { text: 'Orchestrator tasks pane opened and refreshed.' }
  })

  on('tool.call', { tool: WRITE_TOOLS }, async ($, e, next) => {
    const result = await next(e)
    void refresh($)

    return result
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const { groups, hasLoaded, refreshedAt, error } = await read($, view)
    const width = Math.max(12, e.props.bodyColumns ?? 40)
    const titleWidth = width - 7
    const count = groups.reduce((sum, g) => sum + g.tasks.length, 0)
    const isGrouped = groups.length > 1
    const header = hasLoaded
      ? `${count} open${refreshedAt === null ? '' : ` · refreshed ${clockTime(refreshedAt)}`}`
      : 'Loading…'

    return (
      <Box flexDirection="column">
        <Text dimColor>{truncate(header, width)}</Text>
        {error !== null && <Text color="warning">{error}</Text>}
        {hasLoaded && groups.length === 0 && (
          <Text dimColor>{truncate('No "Claude orchestrator:" task in Work.', width)}</Text>
        )}
        {groups.map(group => (
          <Box flexDirection="column">
            {isGrouped && <Text bold>{truncate(group.parentTitle, width)}</Text>}
            {group.tasks.length === 0 && <Text dimColor>No open tasks.</Text>}
            {group.tasks.map(task => (
              <Text>
                <Text color={task.priority === 'Urgent' ? 'error' : task.priority === 'High' ? 'warning' : 'subtle'}>
                  {tagOf(task.priority)}
                </Text>{' '}
                {task.isBlocked ? '⊘ ' : ''}
                {truncate(task.title, titleWidth - (task.isBlocked ? 2 : 0))}
              </Text>
            ))}
          </Box>
        ))}
      </Box>
    )
  })
}
