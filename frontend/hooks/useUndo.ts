"use client";

import { useCallback, useRef, useState } from "react";

import { writeStored } from "./usePersistentState";

interface Entry {
  key: string; // the localStorage key the change was made under (drawings or AI overlays of one chart)
  before: unknown;
  after: unknown;
  label: string;
  at: number;
}

const LIMIT = 100;
// Changes to the same key this close together are one step (a drag fires a change per mouse move).
const COALESCE_MS = 700;

/**
 * Undo / redo for drawings and AI overlays. Each change records the stored value before and after it; undo
 * writes `before` back through writeStored, so every chart showing that key (including ones for other symbols)
 * picks it up. `label` says what an undo would take back ("Undo: delete drawing").
 */
export function useUndo() {
  const past = useRef<Entry[]>([]);
  const future = useRef<Entry[]>([]);
  const [, setTick] = useState(0);
  const bump = () => setTick((t) => t + 1);

  const record = useCallback((key: string, before: unknown, after: unknown, label: string) => {
    if (JSON.stringify(before) === JSON.stringify(after)) return;
    const now = Date.now();
    const last = past.current[past.current.length - 1];
    if (last && last.key === key && last.label === label && now - last.at < COALESCE_MS) {
      last.after = after;
      last.at = now;
    } else {
      past.current = [...past.current, { key, before, after, label, at: now }].slice(-LIMIT);
    }
    future.current = [];
    bump();
  }, []);

  const undo = useCallback(() => {
    const e = past.current.pop();
    if (!e) return null;
    writeStored(e.key, e.before);
    future.current.push(e);
    bump();
    return e.label;
  }, []);

  const redo = useCallback(() => {
    const e = future.current.pop();
    if (!e) return null;
    writeStored(e.key, e.after);
    past.current.push({ ...e, at: 0 }); // never coalesce into a redone step
    bump();
    return e.label;
  }, []);

  return {
    record,
    undo,
    redo,
    canUndo: past.current.length > 0,
    canRedo: future.current.length > 0,
    undoLabel: past.current[past.current.length - 1]?.label ?? null,
    redoLabel: future.current[future.current.length - 1]?.label ?? null,
  };
}
