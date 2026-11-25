import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, Timer } from 'claude-code'

import type { Loaded, OrchView } from '../types'
import { CONFIG_FILE, DEFAULT_SOURCES, parseConfig } from './config'
import type { Source } from './config'
import { GITHUB_LABEL, loadGithub } from './github'
import type { GithubCache } from './github'
import { isRecord } from './json'
import { clockTime, loadWork, truncate, WORK_LABEL } from './tasks'

const PANE = 'orchestrator-tasks'
const TITLE = 'Orchestrator tasks'
const REFRESH_MS = 2 * 60 * 1000
const WORK_WRITE_TOOLS = ['mcp__Work__create_task', 'mcp__Work__update_task', 'mcp__Work__delete_task'] as const
/** A `gh` command that changes an issue or its place on a Project. */
const GH_WRITE = /\bgh\s+(?:project\s+item-(?:add|edit|delete|archive)|issue\s+(?:create|edit|close|reopen))\b/

const EMPTY: OrchView = { loaded: null, refreshedAt: null, error: null }
const view = atom({ plugin: 'orchestrator', key: 'view' } as const, EMPTY, { shape: 'view-2' })

type Failure = { source: string; error: string; shape: Record<string, unknown> | null }

/**
 * Where the failures of a refresh are written: a fixed folder under the
 * user's `~/.claude`, the same for every session and every install.
 */
export const diagnosticsPath = (home: string) =>
  `${home.replace(/\/+$/, '')}/.claude/dev-mods/orchestrator-tasks-diagnostics/last-parse-failure.json`

/**
 * Writes the failures of one refresh to the diagnostics file and the debug
 * log. Side effects: one file write (skipped when HOME is unset) and one
 * debug-log line; never throws.
 */
const recordFailures = async ($: EngineInterface, at: number, failures: Failure[]) => {
  const report = { at: new Date(at).toISOString(), failures }
  try {
    $.ui.log(`orchestrator-tasks failure: ${JSON.stringify(report)}`, { to: 'debug' })
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

const labelOf = (source: Source) => (source.type === 'work' ? WORK_LABEL : GITHUB_LABEL)

/**
 * Reads the project's sources and its origin remote. The sources come from
 * the config file at the repository root (the session's folder outside a
 * repository), or the defaults when the project has no such file. Throws when
 * the file exists and cannot be read as a config.
 */
const readProject = async ($: EngineInterface) => {
  const repo = await $.session.repo()
  const root = repo?.root ?? (await $.session.cwd())
  const path = `${root.replace(/\/+$/, '')}/${CONFIG_FILE}`
  const remote = repo?.remote ?? null
  if (!(await $.fs.exists(path))) return { sources: DEFAULT_SOURCES, remote }
  try {
    return { sources: parseConfig(await $.fs.read(path)), remote }
  } catch (err) {
    throw new Error(`Could not read ${CONFIG_FILE} (${err instanceof Error ? err.message : 'unknown error'}).`)
  }
}

/**
 * Where the shared GitHub read is cached: a fixed folder under the user's
 * `~/.claude`, the same for every session. Without HOME there is no cache.
 */
export const cacheDir = (home: string) => `${home.replace(/\/+$/, '')}/.claude/dev-mods/orchestrator-tasks-cache`

const githubCache = async ($: EngineInterface, now: number): Promise<GithubCache | undefined> => {
  try {
    const home = await $.env.get('HOME')

    return home ? { dir: cacheDir(home), now, isForced: isReadForced, run: argv => $.process.run(argv) } : undefined
  } catch {
    return undefined
  }
}

const loadSource = async ($: EngineInterface, source: Source, remote: string | null, now: number): Promise<Loaded> =>
  source.type === 'github'
    ? loadGithub(source, remote, argv => $.process.run(argv), await githubCache($, now))
    : loadWork(
        (server, tool, args) => $.mcp.call(server, tool, args),
        path => $.fs.read(path),
      )

/**
 * Tries the sources in order and resolves the first that reads, with the
 * failures of the ones before it. `loaded` is undefined when every source failed.
 */
const loadFirst = async ($: EngineInterface, sources: Source[], remote: string | null, now: number) => {
  const failures: Failure[] = []
  for (const source of sources) {
    try {
      // The order is the fallback order, so each source waits for the one before it.
      return { loaded: await loadSource($, source, remote, now), failures }
    } catch (err) {
      failures.push({
        source: labelOf(source),
        error: err instanceof Error && err.message ? err.message : 'the call failed',
        shape: isRecord(err) && isRecord(err.shape) ? err.shape : null,
      })
    }
  }

  return { loaded: undefined, failures }
}

/**
 * Reads the tasks from the project's first working source. Resolves `loaded`
 * when a source read, or `error` with one line for the pane when none did or
 * the config file has a mistake. Records the failures (see recordFailures).
 */
const readTasks = async ($: EngineInterface, now: number): Promise<{ loaded?: Loaded; error: string | null }> => {
  try {
    const { sources, remote } = await readProject($)
    const { loaded, failures } = await loadFirst($, sources, remote, now)
    if (!loaded || failures.some(f => f.shape !== null)) await recordFailures($, now, failures)
    if (loaded) return { loaded, error: null }

    return { error: `Could not read tasks (${failures.map(f => `${f.source}: ${f.error}`).join('; ')}). Retrying in 2 min.` }
  } catch (err) {
    return { error: `${err instanceof Error ? err.message : 'The refresh failed.'} Retrying in 2 min.` }
  }
}

/**
 * Reads the tasks once and writes the pane's view. Never throws: when no
 * source reads, it keeps the last good list and sets `error`.
 */
const refreshOnce = async ($: EngineInterface) => {
  try {
    const now = await $.clock.now()
    const { loaded, error } = await readTasks($, now)
    await update($, view, prev => (loaded ? { loaded, refreshedAt: now, error: null } : { ...(prev ?? EMPTY), error }))
  } catch {
    // The view could not be written either; the next tick tries again.
  }
}

let running: Promise<void> | undefined
let isStale = false
/** Set by a refresh that follows a write: the shared GitHub read may be reused only if it is under 30 seconds old. */
let isForced = false
/** The value of `isForced` that the read in progress started with. */
let isReadForced = false

/**
 * Refreshes the pane. One read runs at a time: a call during a read asks for
 * one more read after it, so a burst of task writes costs two reads, and the
 * last read always starts after the last write. Never throws.
 */
const refresh = ($: EngineInterface, isAfterWrite = false): Promise<void> => {
  if (isAfterWrite) isForced = true
  if (running) {
    isStale = true

    return running
  }
  running = (async () => {
    do {
      isStale = false
      isReadForced = isForced
      isForced = false
      await refreshOnce($)
    } while (isStale)
  })().finally(() => {
    running = undefined
  })

  return running
}

let tick: Timer | undefined

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'orch-tasks',
      description: "Show the orchestrator's tasks from the project's tracker in a pane",
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
    await refresh($, true)

    return { text: 'Orchestrator tasks pane opened and refreshed.' }
  })

  on('tool.call', { tool: WORK_WRITE_TOOLS }, async ($, e, next) => {
    const result = await next(e)
    void refresh($, true)

    return result
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const result = await next(e)
    if (typeof e.command === 'string' && GH_WRITE.test(e.command)) void refresh($, true)

    return result
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const { loaded, refreshedAt, error } = await read($, view)
    const width = Math.max(12, e.props.bodyColumns ?? 40)
    const groups = loaded?.groups ?? []
    const count = groups.reduce((sum, g) => sum + g.tasks.length, 0)
    const isGrouped = groups.length > 1 || loaded?.isAlwaysGrouped === true
    const header = loaded
      ? `${count} ${loaded.noun} · ${loaded.label}${refreshedAt === null ? '' : ` · refreshed ${clockTime(refreshedAt)}`}`
      : 'Loading…'

    return (
      <Box flexDirection="column">
        <Text dimColor>{truncate(header, width)}</Text>
        {error !== null && <Text color="warning">{error}</Text>}
        {loaded && groups.length === 0 && <Text dimColor>{truncate(loaded.emptyText, width)}</Text>}
        {groups.map(group => (
          <Box flexDirection="column">
            {isGrouped && <Text bold>{truncate(group.title, width)}</Text>}
            {group.tasks.length === 0 && <Text dimColor>No open tasks.</Text>}
            {group.tasks.map(task => (
              <Text>
                <Text color={task.tone}>{task.tag}</Text> {task.isBlocked ? '⊘ ' : ''}
                {truncate(task.title, width - task.tag.length - 1 - (task.isBlocked ? 2 : 0))}
              </Text>
            ))}
          </Box>
        ))}
      </Box>
    )
  })
}
