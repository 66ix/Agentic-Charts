"use client";

import { useEffect, useRef, useState } from "react";

/**
 * useState mirrored to localStorage. Storage can be unavailable (private
 * mode, blocked cookies), so every access is guarded and the state still
 * works in memory. Re-reads when `key` changes (e.g. drawings per symbol).
 */
export function usePersistentState<T>(key: string, initial: T) {
  const [state, setState] = useState<T>(initial);
  const loadedKey = useRef<string | null>(null);

  useEffect(() => {
    let next = initial;
    try {
      const raw = window.localStorage.getItem(key);
      if (raw) next = JSON.parse(raw) as T;
    } catch {
      /* storage unavailable or corrupt */
    }
    loadedKey.current = key;
    setState(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    if (loadedKey.current !== key) return;
    try {
      window.localStorage.setItem(key, JSON.stringify(state));
    } catch {
      /* ignore quota / privacy errors */
    }
  }, [key, state]);

  return [state, setState] as const;
}
