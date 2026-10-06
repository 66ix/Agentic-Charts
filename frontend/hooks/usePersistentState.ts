"use client";

import { useCallback, useEffect, useState, type SetStateAction } from "react";

/**
 * useState mirrored to localStorage. Storage can be unavailable (private
 * mode, blocked cookies), so every access is guarded and the state still
 * works in memory. Re-reads when `key` changes (e.g. drawings per symbol).
 *
 * The value is stored together with the key it was loaded for, so a key
 * change never writes the previous key's value under the new key, and the
 * previous value is never shown for the new key while it loads.
 */
export function usePersistentState<T>(key: string, initial: T) {
  const [entry, setEntry] = useState<{ key: string | null; value: T }>({ key: null, value: initial });

  useEffect(() => {
    let next = initial;
    try {
      const raw = window.localStorage.getItem(key);
      if (raw) next = JSON.parse(raw) as T;
    } catch {
      /* storage unavailable or corrupt */
    }
    setEntry({ key, value: next });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    if (entry.key !== key) return;
    try {
      window.localStorage.setItem(key, JSON.stringify(entry.value));
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
