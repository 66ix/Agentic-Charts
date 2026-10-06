"use client";

import { useEffect, useState } from "react";

import { fetchMetrics } from "@/lib/api";
import type { MarketMetrics } from "@/lib/types";

export function useMarketMetrics(pollMs = 30_000) {
  const [data, setData] = useState<MarketMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        setData(await fetchMetrics(ctrl.signal));
        setError(null);
      } catch (err) {
        if ((err as Error).name !== "AbortError") setError((err as Error).message);
      }
      timer = setTimeout(tick, pollMs);
    };
    tick();
    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [pollMs]);

  return { data, error };
}
