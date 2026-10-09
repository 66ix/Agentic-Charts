"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchStatus, type AppStatus } from "@/lib/status";

/** GET /api/status every `intervalMs` (and on `refresh()`); the last good answer stays while a refresh fails. */
export function useAppStatus(intervalMs: number) {
  const [data, setData] = useState<AppStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ctrlRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    ctrlRef.current?.abort();
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    try {
      const s = await fetchStatus(ctrl.signal);
      if (!ctrl.signal.aborted) {
        setData(s);
        setError(null);
      }
    } catch (err) {
      if (!ctrl.signal.aborted && (err as Error).name !== "AbortError") setError((err as Error).message || "Request failed");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), intervalMs);
    return () => {
      clearInterval(id);
      ctrlRef.current?.abort();
    };
  }, [refresh, intervalMs]);

  return { data, error, refresh };
}
