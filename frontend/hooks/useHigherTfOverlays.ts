"use client";

import { useEffect, useState } from "react";

import { readStored, SYNC_EVENT } from "@/hooks/usePersistentState";
import { higherTfOverlays } from "@/lib/layers";
import type { Overlay } from "@/lib/types";

/** The agent's zones and levels from the timeframes above `interval` on `symbol`, kept up to date as the agent
 *  draws on any of them (see higherTfOverlays). Read after mount, so the server render and the first client render
 *  agree. */
export function useHigherTfOverlays(symbol: string, interval: string): Overlay[] {
  const [out, setOut] = useState<Overlay[]>([]);
  useEffect(() => {
    const prefix = `ac:overlays:${symbol}:`;
    const load = () => setOut(higherTfOverlays(symbol, interval, (key) => readStored<Overlay[]>(key, [])));
    const onChange = (e: Event) => {
      const key = e instanceof StorageEvent ? e.key : (e as CustomEvent<string>).detail;
      if (key?.startsWith(prefix)) load();
    };
    load();
    window.addEventListener(SYNC_EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(SYNC_EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, [symbol, interval]);
  return out;
}
