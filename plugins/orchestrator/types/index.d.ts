export type Priority = 'Urgent' | 'High' | 'Medium' | 'Low' | null

export type Tone = 'error' | 'warning' | 'subtle'

/** One pane row. `tag` is the short lead (a priority, an issue number), drawn in `tone`. */
export type TaskRow = { id: string; title: string; tag: string; tone: Tone; isBlocked: boolean }

export type TaskGroup = { id: string; title: string; tasks: TaskRow[] }

/** What one source read: its groups and the words the pane uses for them. */
export type Loaded = {
  /** The source's name in the header, for example `Work` or `GitHub`. */
  label: string
  groups: TaskGroup[]
  /** The words after the count in the header, for example `open`. */
  noun: string
  /** The one line drawn when there are no groups. */
  emptyText: string
  /** Whether a group title is drawn even when there is one group. */
  isAlwaysGrouped: boolean
}

/**
 * What the pane draws. `loaded` is the last good read; `error` is set when the
 * latest refresh failed and cleared by the next one that succeeds.
 */
export type OrchView = {
  loaded: Loaded | null
  refreshedAt: number | null
  error: string | null
}

declare module 'claude-code' {
  interface PluginState {
    orchestrator: { view: Shaped<OrchView> }
  }
}
