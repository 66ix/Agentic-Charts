"use client";

import { useEffect, useRef, useState } from "react";

// The last data per key, shared by every panel for this page load: going back to a coin or a tab shows what it had
// at once while the refresh runs.
const lastData = new Map<string, unknown>();
const MAX_KEYS = 300;

function remember(key: string, data: unknown) {
  lastData.delete(key);
  lastData.set(key, data);
  if (lastData.size > MAX_KEYS) lastData.delete(lastData.keys().next().value as string);
}

/**
 * Fetches with `fetcher` whenever `key` changes and then every `intervalMs`; `key = null` pauses it. Data is
 * kept per key, so a new symbol never shows the previous symbol's numbers while it loads, and a failed refresh
 * keeps the last good data next to the error.
 * The last data for a key is shown at once (marked `stale`) while it refreshes.
 */
export function usePolled<T>(key: string | null, fetcher: (signal: AbortSignal) => Promise<T>, intervalMs: number) {
  const [state, setState] = useState<{ key: string | null; data: T | null; error: string | null }>({
    key: null,
    data: null,
    error: null,
  });
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  useEffect(() => {
    if (key === null) return;
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const data = await fetcherRef.current(ctrl.signal);
        if (!ctrl.signal.aborted) {
          remember(key, data);
          setState({ key, data, error: null });
        }
      } catch (err) {
        if (ctrl.signal.aborted || (err as Error).name === "AbortError") return;
        const message = (err as Error).message || "Request failed";
        setState((s) => ({ key, data: s.key === key ? s.data : null, error: message }));
      }
      if (!ctrl.signal.aborted) timer = setTimeout(tick, intervalMs);
    };
    tick();
    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [key, intervalMs]);

  const current = state.key === key;
  const cached = !current && key !== null ? ((lastData.get(key) as T | undefined) ?? null) : null;
  return {
    data: current ? state.data : cached,
    error: current ? state.error : null,
    loading: key !== null && !current,
    /** Data shown from the cache while the refresh runs. */
    stale: cached !== null,
  };
}
