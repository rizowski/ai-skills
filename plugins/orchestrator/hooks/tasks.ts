import type { McpToolResult } from 'claude-code'

import type { Loaded, Priority, TaskGroup, TaskRow, Tone } from '../types'
import { isRecord, tryJson } from './json'

export const PARENT_PREFIX = 'Claude orchestrator:'

const PRIORITY_RANK: Record<string, number> = { Urgent: 0, High: 1, Medium: 2, Low: 3 }

export type RawTask = {
  id: string
  title: string
  priority: Priority
  done: boolean
  isBlocked: boolean
  parentTaskId: string | null
  createdAt: string
}

/** Reads a file's text; rejects when it cannot. */
export type ReadFile = (path: string) => Promise<string>

/**
 * A result no task list could be read from. `message` is one short line for
 * the pane (no task content beyond a few characters); `shape` is the fuller
 * description for the diagnostics file.
 */
export class TaskParseError extends Error {
  shape: Record<string, unknown>

  constructor(summary: string, shape: Record<string, unknown>) {
    super(summary)
    this.shape = shape
  }
}

const asPriority = (value: unknown): Priority =>
  value === 'Urgent' || value === 'High' || value === 'Medium' || value === 'Low' ? value : null

const isTaskLike = (value: unknown) => isRecord(value) && typeof value.id === 'string' && typeof value.title === 'string'

const toRawTask = (value: unknown): RawTask | null => {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.title !== 'string') return null

  return {
    id: value.id,
    title: value.title,
    priority: asPriority(value.priority),
    done: value.done === true,
    isBlocked: value.isBlocked === true,
    parentTaskId: typeof value.parentTaskId === 'string' ? value.parentTaskId : null,
    createdAt: typeof value.createdAt === 'string' ? value.createdAt : '',
  }
}

const SAVED_TO = /saved to:?\s+(\/[^\s"']+?)(?=[.,;)]?(?:\s|$))/

/**
 * Finds the task array in a parsed value: the array itself (tasks, or MCP
 * content blocks whose text holds them), the array under `tasks`, `result`,
 * `data`, `structuredContent` or `content`, or a string holding JSON.
 */
const findInValue = (value: unknown, depth: number): unknown[] | undefined => {
  if (depth > 6) return undefined
  if (Array.isArray(value)) {
    if (value.length === 0 || value.some(isTaskLike)) return value
    const texts = value.flatMap(b => (isRecord(b) && b.type === 'text' && typeof b.text === 'string' ? [b.text] : []))

    return texts.length > 0 ? findInText(texts.join(''), depth + 1) : undefined
  }
  if (typeof value === 'string') return findInText(value, depth + 1)
  if (!isRecord(value)) return undefined

  return ['tasks', 'result', 'data', 'structuredContent', 'content']
    .map(key => (key in value ? findInValue(value[key], depth + 1) : undefined))
    .find(found => found !== undefined)
}

const findInText = (text: string, depth: number): unknown[] | undefined => {
  if (depth > 6) return undefined
  const whole = tryJson(text.trim())
  if (whole.ok) return findInValue(whole.value, depth)
  const slices: [number, number][] = [
    [text.indexOf('{'), text.lastIndexOf('}')],
    [text.indexOf('['), text.lastIndexOf(']')],
  ]

  return slices
    .filter(([from, to]) => from >= 0 && to > from)
    .map(([from, to]) => tryJson(text.slice(from, to + 1)))
    .map(parsed => (parsed.ok ? findInValue(parsed.value, depth) : undefined))
    .find(found => found !== undefined)
}

const oneLine = (text: string, length: number) => text.replace(/\s+/g, ' ').slice(0, length).replace(/"/g, "'")

const structuredSummary = (structured: unknown) => {
  if (structured === undefined) return { short: 'no', full: null }
  if (Array.isArray(structured)) return { short: `array(${structured.length})`, full: { array: structured.length } }
  if (isRecord(structured)) {
    const keys = Object.keys(structured)

    return { short: `{${keys.slice(0, 4).join(',')}}`, full: { keys } }
  }

  return { short: typeof structured, full: { type: typeof structured } }
}

/** Describes a result's shape: `summary` short enough for the pane, `shape` fuller for the diagnostics file. */
export const describeShape = (result: McpToolResult) => {
  const blocks = (result.content ?? []).map(block => {
    const text = typeof block.text === 'string' ? block.text : undefined

    return {
      type: block.type,
      length: text?.length,
      head: text?.slice(0, 200),
      tail: text !== undefined && text.length > 200 ? text.slice(-100) : undefined,
    }
  })
  const structured = structuredSummary(result.structuredContent)
  const firstText = blocks.find(b => b.head !== undefined)?.head ?? ''
  const blockList =
    blocks.length === 0 ? 'none' : blocks.map(b => (b.length === undefined ? b.type : `${b.type}(${b.length})`)).join(',')

  return {
    summary: `blocks: ${blockList} | structured: ${structured.short} | starts: "${oneLine(firstText, 20)}"`,
    shape: { isError: result.isError, blocks, structuredContent: structured.full },
  }
}

const toRawTasks = (found: unknown[]) => found.map(toRawTask).filter((t): t is RawTask => t !== null)

/**
 * Reads the task list out of a `list_my_tasks` result. Tries, in order:
 * `structuredContent`; all text blocks joined; each text block alone (each
 * also as double-encoded JSON or JSON inside other text); a "saved to <path>"
 * notice, reading that file with `readFile`. Throws a TaskParseError naming
 * the result's shape when nothing yields a list.
 */
export const parseTasks = async (result: McpToolResult, readFile?: ReadFile): Promise<RawTask[]> => {
  const { summary, shape } = describeShape(result)
  if (result.isError) throw new TaskParseError(`Work returned an error; ${summary}`, shape)

  const texts = (result.content ?? []).flatMap(b => (b.type === 'text' && typeof b.text === 'string' ? [b.text] : []))
  const found =
    (result.structuredContent === undefined ? undefined : findInValue(result.structuredContent, 0)) ??
    (texts.length > 0 ? findInText(texts.join(''), 0) : undefined) ??
    texts.map(text => findInText(text, 0)).find(f => f !== undefined)
  if (found) return toRawTasks(found)

  const savedPath = texts.map(t => SAVED_TO.exec(t)?.[1]).find(p => p !== undefined)
  if (savedPath === undefined || readFile === undefined) {
    throw new TaskParseError(`no task list; ${summary}`, savedPath === undefined ? shape : { ...shape, savedPath })
  }

  let fileText: string
  try {
    fileText = await readFile(savedPath)
  } catch (err) {
    const why = err instanceof Error ? oneLine(err.message, 60) : 'unknown error'
    throw new TaskParseError(`result saved to a file the mod cannot read (${why}); ${summary}`, {
      ...shape,
      savedPath,
      readError: why,
    })
  }
  const fromFile = findInText(fileText, 0)
  if (fromFile) return toRawTasks(fromFile)

  throw new TaskParseError(`saved file did not parse (${fileText.length} chars, starts "${oneLine(fileText, 20)}")`, {
    ...shape,
    savedPath,
    file: { length: fileText.length, head: fileText.slice(0, 200), tail: fileText.slice(-100) },
  })
}

const byPriorityThenAge = (a: RawTask, b: RawTask) =>
  (PRIORITY_RANK[a.priority ?? ''] ?? 4) - (PRIORITY_RANK[b.priority ?? ''] ?? 4) ||
  a.createdAt.localeCompare(b.createdAt)

export const PRIORITY_TAG: Record<string, string> = { Urgent: 'URG', High: 'HIGH', Medium: 'MED', Low: 'LOW' }

export const tagOf = (priority: Priority) => (PRIORITY_TAG[priority ?? ''] ?? '--').padEnd(4)

const toneOf = (priority: Priority): Tone => (priority === 'Urgent' ? 'error' : priority === 'High' ? 'warning' : 'subtle')

/**
 * Groups the open subtasks under each open top-level task whose title starts with
 * `Claude orchestrator:`, sorted Urgent, High, Medium, Low, none, then oldest first.
 */
export const groupOrchestratorTasks = (tasks: RawTask[]): TaskGroup[] =>
  tasks
    .filter(t => t.parentTaskId === null && !t.done && t.title.startsWith(PARENT_PREFIX))
    .map(parent => ({
      id: parent.id,
      title: parent.title,
      tasks: tasks
        .filter(t => t.parentTaskId === parent.id && !t.done)
        .sort(byPriorityThenAge)
        .map(
          ({ id, title, priority, isBlocked }): TaskRow => ({
            id,
            title,
            tag: tagOf(priority),
            tone: toneOf(priority),
            isBlocked,
          }),
        ),
    }))

export const WORK_LABEL = 'Work'

/** Calls one tool of an MCP server and resolves its result. */
export type CallTool = (server: string, tool: string, args: Record<string, unknown>) => Promise<McpToolResult>

/**
 * Reads the orchestrator's open tasks from the Work MCP server. Throws when
 * the server does not answer, and a TaskParseError when its answer holds no task list.
 */
export const loadWork = async (callTool: CallTool, readFile: ReadFile): Promise<Loaded> => ({
  label: WORK_LABEL,
  groups: groupOrchestratorTasks(await parseTasks(await callTool('Work', 'list_my_tasks', {}), readFile)),
  noun: 'open',
  emptyText: 'No "Claude orchestrator:" task in Work.',
  isAlwaysGrouped: false,
})

export const truncate = (text: string, width: number) =>
  text.length <= width ? text : `${text.slice(0, Math.max(1, width - 1))}…`

export const clockTime = (ms: number) => {
  const d = new Date(ms)

  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
