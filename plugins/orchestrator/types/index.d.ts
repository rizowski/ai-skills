export type Priority = 'Urgent' | 'High' | 'Medium' | 'Low' | null

export type TaskRow = { id: string; title: string; priority: Priority; isBlocked: boolean }

export type TaskGroup = { parentId: string; parentTitle: string; tasks: TaskRow[] }

/**
 * What the pane draws. `groups` is the last good list; `error` is set when the
 * latest refresh failed and cleared by the next one that succeeds.
 */
export type OrchView = {
  groups: TaskGroup[]
  hasLoaded: boolean
  refreshedAt: number | null
  error: string | null
}

declare module 'claude-code' {
  interface PluginState {
    orchestrator: { view: Shaped<OrchView> }
  }
}
