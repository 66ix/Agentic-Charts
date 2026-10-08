"use client";

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";

export const SYNC_EVENT = "ac-storage";

/** Write a value straight to storage and tell every usePersistentState on that key (e.g. overlays for a chart
 *  the agent is about to switch to). */
export function writeStored(key: string, value: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    window.dispatchEvent(new CustomEvent(SYNC_EVENT, { detail: key }));
  } catch {
    /* ignore quota / privacy errors */
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
export function usePersistentState<T>(key: string, initial: T) {
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
    try {
      const raw = JSON.stringify(entry.value);
      if (raw === rawRef.current) return;
      rawRef.current = raw;
      window.localStorage.setItem(key, raw);
      window.dispatchEvent(new CustomEvent(SYNC_EVENT, { detail: key }));
    } catch {
      /* ignore quota / privacy errors */
    }
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
