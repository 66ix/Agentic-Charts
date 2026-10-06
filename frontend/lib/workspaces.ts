import { readStored, writeStored } from "@/hooks/usePersistentState";

/**
 * Saved layouts ("workspaces"): which charts are open and how, the indicators, the side panel and the layer
 * toggles, under a name. Loading one writes those keys back, and every hook on them re-reads. Drawings, AI levels,
 * alerts and chats are per coin and are not part of a layout.
 */
export const WORKSPACE_KEYS = [
  "ac:cells",
  "ac:grid",
  "ac:active-cell",
  "ac:indicators",
  "ac:indicator-settings",
  "ac:layout",
  "ac:layers",
  "ac:dock",
  "ac:compare",
] as const;

export interface SavedWorkspace {
  id: string;
  name: string;
  savedAt: number;
  state: Record<string, unknown>;
}

export const WORKSPACES_KEY = "ac:workspaces";
export const CURRENT_WORKSPACE_KEY = "ac:workspace-current";

export function snapshot(): Record<string, unknown> {
  const state: Record<string, unknown> = {};
  for (const k of WORKSPACE_KEYS) {
    const v = readStored<unknown>(k, undefined);
    if (v !== undefined) state[k] = v;
  }
  return state;
}

export function applyWorkspace(w: SavedWorkspace) {
  for (const [k, v] of Object.entries(w.state)) writeStored(k, v);
  writeStored(CURRENT_WORKSPACE_KEY, w.id);
}

/** Save the current layout over `id`, or as a new one named `name` (which becomes the current layout). */
export function saveWorkspace(list: SavedWorkspace[], opts: { id?: string; name?: string }): SavedWorkspace[] {
  const now = Date.now();
  if (opts.id && list.some((w) => w.id === opts.id)) {
    return list.map((w) => (w.id === opts.id ? { ...w, savedAt: now, state: snapshot() } : w));
  }
  const w: SavedWorkspace = {
    id: Math.random().toString(36).slice(2, 10),
    name: (opts.name ?? "").trim() || `Layout ${list.length + 1}`,
    savedAt: now,
    state: snapshot(),
  };
  writeStored(CURRENT_WORKSPACE_KEY, w.id);
  return [...list, w];
}
