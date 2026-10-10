"use client";

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";

export const SYNC_EVENT = "ac-storage";

/** Write a value straight to storage and tell every usePersistentState on that key (e.g. overlays for a chart
 *  the agent is about to switch to). */
export function writeStored(key: string, value: unknown) {
  try {
    if (setItemSafe(key, JSON.stringify(value))) window.dispatchEvent(new CustomEvent(SYNC_EVENT, { detail: key }));
  } catch {
    /* not serialisable */
  }
}

/** Fired (once per page load) when the browser's storage is full and a save was lost even after pruning. */
export const STORAGE_FULL_EVENT = "ac-storage-full";
const TOUCHED_KEY = "ac:touched";
const PRUNE_AFTER_MS = 30 * 86_400_000;
const PRUNABLE = /^ac:overlays:/;
let fullReported = false;

function isQuota(err: unknown): boolean {
  const e = err as { name?: string; code?: number };
  return e?.name === "QuotaExceededError" || e?.name === "NS_ERROR_DOM_QUOTA_REACHED" || e?.code === 22 || e?.code === 1014;
}

/** When each per-chart drawing key was last written, so drawings of charts not opened for a month can go first. */
function touch(key: string) {
  if (!PRUNABLE.test(key)) return;
  try {
    const all = JSON.parse(window.localStorage.getItem(TOUCHED_KEY) || "{}") as Record<string, number>;
    const now = Date.now();
    if (now - (all[key] ?? 0) < 3_600_000) return; // at most hourly per key
    all[key] = now;
    window.localStorage.setItem(TOUCHED_KEY, JSON.stringify(all));
  } catch {
    /* best effort */
  }
}

/** Drop drawings of charts untouched for 30 days (keys never seen are stamped now, so nothing goes on first sight)
 *  and slim stored past chats to their text → whether anything was freed. */
export function pruneStorage(now = Date.now()): boolean {
  let freed = false;
  try {
    const ls = window.localStorage;
    const all = JSON.parse(ls.getItem(TOUCHED_KEY) || "{}") as Record<string, number>;
    for (const k of Object.keys(ls)) {
      if (!PRUNABLE.test(k)) continue;
      if (all[k] == null) all[k] = now;
      else if (now - all[k] > PRUNE_AFTER_MS) {
        ls.removeItem(k);
        delete all[k];
        freed = true;
      }
    }
    for (const k of Object.keys(all)) if (ls.getItem(k) == null) delete all[k];
    ls.setItem(TOUCHED_KEY, JSON.stringify(all));
    const chats = ls.getItem("ac:chats");
    if (chats && chats.length > 200_000) {
      // Past conversations keep their words; the drawings, cards and numbers of old answers are dropped.
      const list = JSON.parse(chats) as { messages?: { id: string; role: string; text: string; symbol?: string; interval?: string; prompt?: string; at?: number }[] }[];
      const slim = list.map((c) => ({
        ...c,
        messages: (c.messages ?? []).map(({ id, role, text, symbol, interval, prompt, at }) => ({ id, role, text, symbol, interval, prompt, at })),
      }));
      ls.setItem("ac:chats", JSON.stringify(slim));
      freed = true;
    }
  } catch {
    /* best effort */
  }
  return freed;
}

/** localStorage.setItem that, when storage is full, prunes and tries once more, then reports the loss once
 *  (STORAGE_FULL_EVENT) instead of failing silently → whether it was saved. */
export function setItemSafe(key: string, raw: string): boolean {
  try {
    window.localStorage.setItem(key, raw);
    touch(key);
    return true;
  } catch (err) {
    if (!isQuota(err)) return false; // storage unavailable (private mode): the state lives in memory
  }
  pruneStorage();
  try {
    window.localStorage.setItem(key, raw);
    touch(key);
    return true;
  } catch {
    if (!fullReported) {
      fullReported = true;
      window.dispatchEvent(new CustomEvent(STORAGE_FULL_EVENT, { detail: key }));
    }
    return false;
  }
}

/** Read a stored value without subscribing to it (e.g. the pins of a chart the user is not looking at). */
export function readStored<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

/** Every key this app saves under, for the backup file and workspaces. */
export function storedKeys(): string[] {
  try {
    return Object.keys(window.localStorage).filter((k) => k.startsWith("ac:"));
  } catch {
    return [];
  }
}

/**
 * useState mirrored to localStorage. Storage can be unavailable (private
 * mode, blocked cookies), so every access is guarded and the state still
 * works in memory. Re-reads when `key` changes (e.g. drawings per symbol).
 *
 * The value is stored together with the key it was loaded for, so a key
 * change never writes the previous key's value under the new key, and the
 * previous value is never shown for the new key while it loads.
 *
 * Hooks sharing a key stay in sync: a write from one (in this tab or another)
 * is picked up by the others, so several charts showing the same symbol agree.
 */
export function usePersistentState<T>(key: string, initial: T, opts: { holdWhile?: (value: T) => boolean } = {}) {
  const holdRef = useRef(opts.holdWhile);
  holdRef.current = opts.holdWhile;
  const [entry, setEntry] = useState<{ key: string | null; value: T }>({ key: null, value: initial });
  const rawRef = useRef<string | null>(null);

  const reload = useCallback(
    (fallback: T) => {
      let next = fallback;
      let raw: string | null = null;
      try {
        raw = window.localStorage.getItem(key);
        if (raw) next = JSON.parse(raw) as T;
      } catch {
        /* storage unavailable or corrupt */
      }
      rawRef.current = raw;
      setEntry({ key, value: next });
    },
    [key],
  );

  useEffect(() => {
    reload(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    const onChange = (e: Event) => {
      const changed = e instanceof StorageEvent ? e.key : (e as CustomEvent<string>).detail;
      if (changed !== key) return;
      let raw: string | null = null;
      try {
        raw = window.localStorage.getItem(key);
      } catch {
        return;
      }
      if (raw !== null && raw !== rawRef.current) reload(initial);
    };
    window.addEventListener(SYNC_EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(SYNC_EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, reload]);

  useEffect(() => {
    if (entry.key !== key) return;
    // `holdWhile`: not written while it says so (an answer still streaming in), only once it settles.
    if (holdRef.current?.(entry.value)) return;
    let raw: string;
    try {
      raw = JSON.stringify(entry.value);
    } catch {
      return;
    }
    if (raw === rawRef.current) return;
    rawRef.current = raw;
    if (setItemSafe(key, raw)) window.dispatchEvent(new CustomEvent(SYNC_EVENT, { detail: key }));
  }, [key, entry]);

  const setState = useCallback(
    (action: SetStateAction<T>) =>
      setEntry((e) => ({
        key: e.key,
        value: typeof action === "function" ? (action as (prev: T) => T)(e.value) : action,
      })),
    [],
  );

  return [entry.key === key ? entry.value : initial, setState, entry.key === key] as const;
}
