import type { Loaded, TaskGroup, TaskRow } from '../types'
import type { GithubSource } from './config'
import { readCached } from './cache'
import type { Cached, Run } from './cache'
import { isRecord, tryJson } from './json'

export const GITHUB_LABEL = 'GitHub'

export type { Run }

type Item = { number: number; title: string; status: string; repository: string }

/**
 * Reads `owner/name` out of a git remote URL on any host, in the https or
 * the ssh form. Resolves undefined when the URL has neither form.
 */
const repoOfRemote = (remote: string | null | undefined) => {
  const found = /[/:]([^/:]+)\/([^/]+?)(?:\.git)?\/?$/.exec(remote ?? '')

  return found?.[1] === undefined || found[2] === undefined ? undefined : { owner: found[1], name: found[2] }
}

/** Runs `gh` and parses its JSON; throws with the first line of stderr when it fails. */
const ghJson = async (run: Run, args: string[]): Promise<unknown> => {
  const command = `gh ${args.slice(0, 2).join(' ')}`
  const { exitCode, stdout, stderr } = await run(['gh', ...args])
  if (exitCode !== 0) {
    const why = stderr.trim().split('\n')[0]?.slice(0, 120) ?? ''
    throw new Error(`${command} failed${why === '' ? '' : `: ${why}`}`)
  }
  const parsed = tryJson(stdout)
  if (!parsed.ok) throw new Error(`${command} did not answer JSON`)

  return parsed.value
}

const toProject = (value: unknown) =>
  isRecord(value) && typeof value.number === 'number' && typeof value.title === 'string' && value.closed !== true
    ? [{ number: value.number, title: value.title }]
    : []

const toItem = (value: unknown): Item[] => {
  if (!isRecord(value) || !isRecord(value.content)) return []
  const { number, title, repository } = value.content

  return typeof number === 'number' && typeof title === 'string' && typeof repository === 'string'
    ? [{ number, title, repository, status: typeof value.status === 'string' ? value.status : '' }]
    : []
}

/** Groups the items by Status in the order of `statuses`, each group sorted by issue number. */
const groupByStatus = (items: Item[], statuses: string[]): TaskGroup[] =>
  statuses
    .map(status => ({
      id: status,
      title: status,
      tasks: items
        .filter(item => item.status === status)
        .sort((a, b) => a.number - b.number)
        .map(
          ({ number, title, repository }): TaskRow => ({
            id: `${repository}#${number}`,
            title,
            tag: `#${number}`,
            tone: 'subtle',
            isBlocked: false,
          }),
        )
        .filter((row, i, rows) => rows.findIndex(r => r.id === row.id) === i),
    }))
    .filter(group => group.tasks.length > 0)

/** The cache a read goes through; `key` is built here from the source and the repository. */
export type GithubCache = Omit<Cached, 'key'>

const cacheKey = (source: GithubSource, owner: string, name: string | undefined) =>
  `github-${[owner, name ?? '', (source.projects ?? ['*']).join(','), source.statuses.join(',')].join('__').replace(/[^A-Za-z0-9._,-]/g, '_')}.json`

/**
 * Reads the issues and pull requests of this repository from the owner's
 * GitHub Projects, with the `gh` CLI, and keeps those whose Status is listed.
 * `known` lists the Project numbers that held items of this repository, and
 * `lastSeen` the Projects that existed at the last read. `gh project list` runs
 * every time; only the `known` Projects are read unless one is new. Throws when `gh` fails,
 * when no owner is known, or when a listed Project title does not exist.
 */
const readProjects = async (source: GithubSource, remote: string | null | undefined, run: Run, known: number[] | undefined, lastSeen: number[] | undefined) => {
  const repo = repoOfRemote(remote)
  const owner = source.owner ?? repo?.owner
  if (owner === undefined) throw new Error('no owner: the repository has no origin remote')

  const listed = await ghJson(run, ['project', 'list', '--owner', owner, '--format', 'json', '--limit', '100'])
  const open = isRecord(listed) && Array.isArray(listed.projects) ? listed.projects.flatMap(toProject) : []
  const missing = (source.projects ?? []).filter(title => !open.some(p => p.title === title))
  if (missing.length > 0) throw new Error(`${owner} has no open Project titled "${missing.join('", "')}"`)
  const wanted = source.projects === undefined ? open : open.filter(p => source.projects?.includes(p.title))
  const seen = wanted.map(p => p.number)
  // A Project that was not seen at the last read may hold items of this repository: read them all.
  const isFull = known === undefined || lastSeen === undefined || seen.some(number => !lastSeen.includes(number))
  const projects = isFull ? wanted : wanted.filter(p => known.includes(p.number))

  const lists = await Promise.all(
    projects.map(p =>
      ghJson(run, ['project', 'item-list', String(p.number), '--owner', owner, '--format', 'json', '--limit', '500']),
    ),
  )
  const here = repo === undefined ? undefined : `${repo.owner}/${repo.name}`.toLowerCase()
  const perProject = lists.map(list =>
    (isRecord(list) && Array.isArray(list.items) ? list.items.flatMap(toItem) : []).filter(
      item => here === undefined || item.repository.toLowerCase() === here,
    ),
  )
  const items = perProject.flat()
  const relevant = projects.filter((_, i) => (perProject[i]?.length ?? 0) > 0).map(p => p.number)

  const shown = source.statuses.join(' or ')

  return {
    relevant,
    seen,
    isFull,
    loaded: {
      label: GITHUB_LABEL,
      groups: groupByStatus(items, source.statuses),
      noun: `in ${shown}`,
      emptyText: `No issue in ${shown} on GitHub.`,
      isAlwaysGrouped: true,
    } satisfies Loaded,
  }
}

/**
 * Reads the tasks of the GitHub source. With `cache`, every session shares one
 * read (see readCached); without it, every call reads GitHub.
 */
export const loadGithub = async (
  source: GithubSource,
  remote: string | null | undefined,
  run: Run,
  cache?: GithubCache,
): Promise<Loaded> => {
  if (cache === undefined) return (await readProjects(source, remote, run, undefined, undefined)).loaded
  const repo = repoOfRemote(remote)
  const owner = source.owner ?? repo?.owner
  if (owner === undefined) throw new Error('no owner: the repository has no origin remote')

  return readCached({ ...cache, key: cacheKey(source, owner, repo?.name) }, (known, seen) =>
    readProjects(source, remote, run, known, seen),
  )
}
