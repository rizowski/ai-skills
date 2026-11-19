/** Whether the value is an object or an array, so its keys can be read. */
export const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null

/** Parses JSON text without throwing: `ok` says whether `value` is the parsed result. */
export const tryJson = (text: string): { ok: true; value: unknown } | { ok: false } => {
  try {
    return { ok: true, value: JSON.parse(text) }
  } catch {
    return { ok: false }
  }
}
