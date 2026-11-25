import type { Loaded } from '../types'
import { isRecord, tryJson } from './json'

/** Runs a command by its argument vector and resolves how it ended. */
export type Run = (argv: string[]) => Promise<{ exitCode: number; stdout: string; stderr: string }>

const MINUTE = 60 * 1000

/** How long one read of GitHub serves every session. */
export const CACHE_TTL_MS = 10 * MINUTE
/** The shortest gap between two reads when a session asks for a fresh one (after a `gh` write). */
export const FORCED_MIN_GAP_MS = 30 * 1000
/** How long the list of Projects that hold this repository's items is trusted before a full scan. */
export const RESCAN_MS = 24 * 60 * MINUTE
/** Below this many GraphQL points left, no read starts until the limit resets. */
export const LOW_POINTS = 300
const BACKOFF_BASE_MS = 5 * MINUTE
const BACKOFF_MAX_MS = 60 * MINUTE
const RATE_LIMIT_BACKOFF_MS = 30 * MINUTE
const LOCK_STALE_MS = 2 * MINUTE

/** What one read of GitHub leaves in the shared cache file. */
export type Entry = {
  /** When the last good read ended; 0 when there is none. */
  fetchedAt: number
  loaded?: Loaded
  /** Numbers of the Projects that held items of the repository at the last full scan. */
  relevant?: number[]
  /** Numbers of every open Project that the source reads, as of the last read; a Project missing here is new. */
  seen?: number[]
  scannedAt?: number
  /** Reads that failed in a row. */
  failures?: number
  /** No read starts before this time. */
  backoffUntil?: number
  /** The error that set `backoffUntil`. */
  error?: string
}

export type Cached = {
  /** The folder of the cache files, shared by every session of the user. */
  dir: string
  /** The cache file name, without the folder. */
  key: string
  now: number
  /** A write happened that makes the cache stale; skip the TTL (not the back-off). */
  isForced: boolean
  run: Run
}

/**
 * One read of GitHub. `known` and `seen` come from the last read. It lists the
 * Projects every time, and reads only `known` when no Project is new since
 * `seen`; otherwise (or when `known` is absent) it reads every Project and
 * resolves `isFull`.
 */
export type Fetch = (
  known: number[] | undefined,
  seen: number[] | undefined,
) => Promise<{ loaded: Loaded; relevant: number[]; seen: number[]; isFull: boolean }>

export const isRateLimitError = (message: string) => /rate limit|unknown owner type|\b50[23]\b/i.test(message)

const readEntry = async (run: Run, file: string): Promise<Entry> => {
  const { exitCode, stdout } = await run(['cat', file]).catch(() => ({ exitCode: 1, stdout: '', stderr: '' }))
  const parsed = exitCode === 0 ? tryJson(stdout) : undefined
  if (!parsed?.ok || !isRecord(parsed.value) || typeof parsed.value.fetchedAt !== 'number') return { fetchedAt: 0 }

  return parsed.value as Entry
}

/** Writes the entry through a temporary file and a rename, so no reader sees half a file. Best effort. */
const writeEntry = async (run: Run, file: string, entry: Entry) => {
  const script = 'mkdir -p "$(dirname "$1")" && printf %s "$2" > "$1.$$.tmp" && mv "$1.$$.tmp" "$1"'
  await run(['sh', '-c', script, 'sh', file, JSON.stringify(entry)]).catch(() => undefined)
}

/**
 * Takes the lock file with an exclusive create. Resolves `held` only when
 * another session holds a lock younger than two minutes; any other outcome
 * (the shell is missing, say) lets the read go on without a lock.
 */
const takeLock = async (run: Run, lock: string, now: number, retry = true): Promise<'taken' | 'held'> => {
  const script = 'mkdir -p "$(dirname "$1")" && if (set -C; printf %s "$2" > "$1") 2>/dev/null; then echo taken; else echo held; fi'
  const result = await run(['sh', '-c', script, 'sh', lock, String(now)]).catch(() => undefined)
  if (result?.exitCode !== 0 || result.stdout.trim() !== 'held') return 'taken'
  // An empty file is a lock that its owner is still writing, not a stale one.
  const text = (await run(['cat', lock]).catch(() => undefined))?.stdout.trim() ?? ''
  const since = Number(text)
  if (retry && text !== '' && Number.isFinite(since) && now - since > LOCK_STALE_MS) {
    // Remove it only if it still holds the stale time, so a lock that another session just took stays.
    await run(['sh', '-c', '[ "$(cat "$1" 2>/dev/null)" = "$2" ] && rm -f "$1"', 'sh', lock, text]).catch(() => undefined)

    return takeLock(run, lock, now, false)
  }

  return 'held'
}

/** The GraphQL points left and when they reset. The REST `rate_limit` endpoint costs no points. */
const graphqlLimit = async (run: Run) => {
  const { exitCode, stdout } = await run(['gh', 'api', 'rate_limit', '--jq', '.resources.graphql']).catch(() => ({
    exitCode: 1,
    stdout: '',
  }))
  const parsed = exitCode === 0 ? tryJson(stdout) : undefined
  if (!parsed?.ok || !isRecord(parsed.value)) return undefined
  const { remaining, reset } = parsed.value

  return typeof remaining === 'number' && typeof reset === 'number' ? { remaining, resetAt: reset * 1000 } : undefined
}

const hhmm = (ms: number) => {
  const d = new Date(ms)

  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

const backoffError = (entry: Entry) => `${entry.error ?? 'GitHub did not answer'}. Not asking again until ${hhmm(entry.backoffUntil ?? 0)}`

/**
 * Serves one read of GitHub to every session. The result lives in a shared
 * file for ten minutes. A session that finds it stale takes a lock, reads
 * once and writes it, so N sessions cost one read, not N. After a failure, or
 * when few GraphQL points are left, no session reads until a back-off ends
 * (5 minutes, doubling to 60; until the reset for a rate limit). Throws
 * when there is no fresh result and no read may start.
 */
export const readCached = async (cache: Cached, fetch: Fetch): Promise<Loaded> => {
  const { dir, key, now, isForced, run } = cache
  const file = `${dir}/${key}`
  const first = await readEntry(run, file)
  if ((first.backoffUntil ?? 0) > now) throw new Error(backoffError(first))
  const maxAge = isForced ? FORCED_MIN_GAP_MS : CACHE_TTL_MS
  if (first.loaded && now - first.fetchedAt < maxAge) return first.loaded

  const lock = `${file}.lock`
  if ((await takeLock(run, lock, now)) === 'held') {
    const other = await readEntry(run, file)
    if (other.loaded) return other.loaded
    throw new Error('Another session is reading GitHub. Waiting for it')
  }
  try {
    const entry = await readEntry(run, file)
    if ((entry.backoffUntil ?? 0) > now) throw new Error(backoffError(entry))
    if (entry.loaded && now - entry.fetchedAt < maxAge) return entry.loaded

    const limit = await graphqlLimit(run)
    if (limit !== undefined && limit.remaining < LOW_POINTS) {
      const low = { ...entry, error: `Only ${limit.remaining} GitHub GraphQL points are left`, backoffUntil: limit.resetAt + 30 * 1000 }
      await writeEntry(run, file, low)
      throw new Error(backoffError(low))
    }

    const isScanFresh = entry.relevant !== undefined && now - (entry.scannedAt ?? 0) < RESCAN_MS
    try {
      const isPartial = isScanFresh && !isForced
      let result
      try {
        result = await fetch(isPartial ? entry.relevant : undefined, entry.seen)
      } catch (err) {
        // A listed Project may be closed or deleted. Try a full scan once before the read counts as failed.
        const message = err instanceof Error ? err.message : ''
        if (!isPartial || isRateLimitError(message)) throw err
        result = await fetch(undefined, undefined)
      }
      const scannedAt = result.isFull ? now : entry.scannedAt
      await writeEntry(run, file, { fetchedAt: now, loaded: result.loaded, relevant: result.relevant, seen: result.seen, scannedAt, failures: 0 })

      return result.loaded
    } catch (err) {
      const message = err instanceof Error && err.message ? err.message : 'the call failed'
      const failures = (entry.failures ?? 0) + 1
      const delay = isRateLimitError(message)
        ? Math.max(RATE_LIMIT_BACKOFF_MS, (limit?.remaining === 0 ? limit.resetAt - now : 0))
        : Math.min(BACKOFF_BASE_MS * 2 ** (failures - 1), BACKOFF_MAX_MS)
      await writeEntry(run, file, { ...entry, failures, error: message, backoffUntil: now + delay })
      throw err
    }
  } finally {
    await run(['rm', '-f', lock]).catch(() => undefined)
  }
}
