"use client";

import { useEffect, useState } from "react";
import { DESK_CHANGED, fetchDesk, fetchDeskCalls, type DeskCall } from "@/lib/desk";
import { usePolled } from "./usePolled";

/** The desk's running calls (not the watched zones), polled once a minute for every chart, and at once when the desk
 *  changes a call; plus whether its confidence is calibrated yet. */
export function useDeskCalls(): { calls: DeskCall[]; calibrated: boolean } {
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    const bump = () => setNonce((n) => n + 1);
    window.addEventListener(DESK_CHANGED, bump);
    return () => window.removeEventListener(DESK_CHANGED, bump);
  }, []);
  const calls = usePolled(`desk:active:${nonce}`, (s) => fetchDeskCalls("active", s, 200), 60_000);
  const state = usePolled(`desk:state:${nonce}`, (s) => fetchDesk(s), 300_000);
  return { calls: calls.data?.calls ?? [], calibrated: !!state.data?.summary.calibration.enough };
}
