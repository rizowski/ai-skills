import { isRecord, tryJson } from './json'

/** The project file that picks the pane's task sources, relative to the repository root. */
export const CONFIG_FILE = '.claude/orchestrator-tasks.json'

const DEFAULT_STATUSES = ['Doing', 'Review']

type WorkSource = { type: 'work' }

export type GithubSource = {
  type: 'github'
  /** The user or organization that owns the Projects; absent, the owner of the `origin` remote. */
  owner?: string
  /** Project titles to read; absent, every open Project of the owner. */
  projects?: string[]
  /** The Status values to list, in the order drawn. */
  statuses: string[]
}

export type Source = WorkSource | GithubSource

/** The sources a project without a config file gets: Work, then GitHub. */
export const DEFAULT_SOURCES: Source[] = [{ type: 'work' }, { type: 'github', statuses: DEFAULT_STATUSES }]

const isNames = (value: unknown): value is string[] =>
  Array.isArray(value) && value.length > 0 && value.every(v => typeof v === 'string' && v !== '')

const toSource = (value: unknown, at: string): Source => {
  if (!isRecord(value) || Array.isArray(value)) throw new Error(`${at} must be an object`)
  if (value.type === 'work') return { type: 'work' }
  if (value.type !== 'github') throw new Error(`${at}.type must be "work" or "github"`)
  if (value.owner !== undefined && (typeof value.owner !== 'string' || value.owner === '')) {
    throw new Error(`${at}.owner must be a name`)
  }
  if (value.projects !== undefined && !isNames(value.projects)) throw new Error(`${at}.projects must be a list of titles`)
  if (value.statuses !== undefined && !isNames(value.statuses)) throw new Error(`${at}.statuses must be a list of names`)

  return {
    type: 'github',
    ...(value.owner === undefined ? {} : { owner: value.owner }),
    ...(value.projects === undefined ? {} : { projects: value.projects }),
    statuses: value.statuses ?? DEFAULT_STATUSES,
  }
}

/**
 * Reads the sources out of the config file's text. Throws with one short
 * reason when the text is not JSON or a field has the wrong form, so a typing
 * error in the file shows in the pane and is never read as "use the defaults".
 */
export const parseConfig = (text: string): Source[] => {
  const parsed = tryJson(text)
  if (!parsed.ok) throw new Error('it is not JSON')
  const { value } = parsed
  if (!isRecord(value) || !Array.isArray(value.sources) || value.sources.length === 0) {
    throw new Error('"sources" must be a list with one source or more')
  }

  return value.sources.map((source, i) => toSource(source, `sources[${i}]`))
}
