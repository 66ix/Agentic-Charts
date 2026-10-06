"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  clearTriggeredAlerts,
  createAlerts,
  deleteAlert,
  fetchAlerts,
  rearmAlert,
  testAlertChannels,
} from "@/lib/api";
import { WS_URL } from "@/lib/config";
import type { AlertChannels, AlertSpec, PriceAlert } from "@/lib/types";

import { usePersistentState } from "./usePersistentState";

/** Alerts from before they moved to the backend: uploaded once, then removed. */
const LEGACY_KEY = "ac:alerts";
/** The backend's last snapshot, shown while it is unreachable. */
const CACHE_KEY = "ac:alerts-cache";
const OFFLINE = "Can't reach the alerts service. Showing the last known alerts; reconnecting…";

export interface FiredAlert {
  alert: PriceAlert;
  price: number;
}

export type ChannelTestResult = Partial<Record<keyof AlertChannels, boolean>>;

function beep() {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    osc.connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.4);
    osc.onended = () => ctx.close();
  } catch {
    /* audio unavailable */
  }
}

function notify(a: PriceAlert, price: number) {
  try {
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      new Notification(`${a.symbol} alert`, { body: `${a.label}: price ${price}`, tag: a.id });
    }
  } catch {
    /* notifications unavailable */
  }
}

export function requestNotificationPermission() {
  try {
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      void Notification.requestPermission();
    }
  } catch {
    /* ignore */
  }
}

const toSpec = (a: AlertSpec): AlertSpec => ({
  kind: a.kind,
  price: a.price ?? null,
  price_low: a.price_low ?? null,
  price_high: a.price_high ?? null,
  label: a.label ?? "",
});

/** Reads and removes the old client-side alerts; `restore` puts them back if the upload fails. */
function takeLegacyAlerts(): { alerts: PriceAlert[]; restore(): void } {
  try {
    const raw = window.localStorage.getItem(LEGACY_KEY);
    if (!raw) return { alerts: [], restore() {} };
    window.localStorage.removeItem(LEGACY_KEY);
    const parsed: unknown = JSON.parse(raw);
    const restore = () => {
      try {
        window.localStorage.setItem(LEGACY_KEY, raw);
      } catch {
        /* storage unavailable */
      }
    };
    return { alerts: Array.isArray(parsed) ? (parsed as PriceAlert[]) : [], restore };
  } catch {
    return { alerts: [], restore() {} };
  }
}

async function uploadLegacy(alerts: PriceAlert[]) {
  const bySymbol = new Map<string, AlertSpec[]>();
  for (const a of alerts) {
    if (a.armed && a.symbol) bySymbol.set(a.symbol, [...(bySymbol.get(a.symbol) ?? []), toSpec(a)]);
  }
  for (const [symbol, specs] of bySymbol) await createAlerts(symbol, specs.slice(0, 50));
}

/**
 * Price alerts, stored and checked by the backend so they fire with the tab
 * closed (and reach Telegram / Discord when configured). `/ws/alerts` pushes a
 * snapshot on every change, which becomes our state, and a `fired` event per
 * alert, which beeps, shows a desktop notification and calls `onFire` for the
 * toast. The last snapshot is cached in localStorage for display while offline.
 */
export function useAlerts(onFire: (fired: FiredAlert[]) => void) {
  const [alerts, setAlerts] = usePersistentState<PriceAlert[]>(CACHE_KEY, []);
  const [channels, setChannels] = useState<AlertChannels | null>(null);
  const [offline, setOffline] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const onFireRef = useRef(onFire);
  onFireRef.current = onFire;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let pingTimer: ReturnType<typeof setInterval> | undefined;
    let disposed = false;
    let migrated = false;

    // First snapshot: move alerts saved by the old client-side version to the server, once.
    const migrate = (serverAlerts: PriceAlert[]) => {
      if (migrated) return;
      migrated = true;
      const legacy = takeLegacyAlerts();
      if (!legacy.alerts.length || serverAlerts.length) return;
      uploadLegacy(legacy.alerts).catch((err: Error) => {
        legacy.restore();
        if (!disposed) setActionError(`Could not move your saved alerts to the server: ${err.message}`);
      });
    };

    const connect = () => {
      if (disposed) return;
      ws = new WebSocket(`${WS_URL}/ws/alerts`);
      ws.onopen = () => {
        retry = 0;
        setOffline(false);
        pingTimer = setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send('{"type":"ping"}'), 25_000);
        fetchAlerts()
          .then((res) => !disposed && setChannels(res.channels))
          .catch(() => {
            /* the snapshot still arrives; channels stay unknown */
          });
      };
      ws.onmessage = (ev) => {
        let msg: { type?: string; alerts?: PriceAlert[]; alert?: PriceAlert; price?: number };
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "snapshot" && Array.isArray(msg.alerts)) {
          setAlerts(msg.alerts);
          migrate(msg.alerts);
        } else if (msg.type === "fired" && msg.alert) {
          const price = Number(msg.price);
          beep();
          notify(msg.alert, price);
          onFireRef.current([{ alert: msg.alert, price }]);
        }
      };
      ws.onclose = () => {
        clearInterval(pingTimer);
        if (disposed) return;
        setOffline(true);
        retryTimer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 30_000));
      };
    };
    connect();

    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearInterval(pingTimer);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }, [setAlerts]);

  const run = useCallback(async (failure: string, fn: () => Promise<void>) => {
    try {
      await fn();
      setActionError(null);
    } catch (err) {
      setActionError(`${failure}: ${(err as Error).message}`);
    }
  }, []);

  /** Resolves to false (and sets `error`) when the backend did not save the alerts. */
  const add = useCallback(
    async (specs: AlertSpec[], symbol: string) => {
      if (!specs.length) return true;
      requestNotificationPermission();
      try {
        const res = await createAlerts(symbol, specs.map(toSpec));
        // The WebSocket snapshot may already have them (and newer state); only add what's missing.
        setAlerts((as) => [...as, ...res.alerts.filter((a) => !as.some((x) => x.id === a.id))]);
        setActionError(null);
        return true;
      } catch (err) {
        setActionError(`Alert not saved: ${(err as Error).message}`);
        return false;
      }
    },
    [setAlerts],
  );

  const remove = useCallback(
    (id: string) =>
      run("Could not delete the alert", async () => {
        try {
          await deleteAlert(id);
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 404)) throw err; // already gone
        }
        setAlerts((as) => as.filter((a) => a.id !== id));
      }),
    [run, setAlerts],
  );

  const rearm = useCallback(
    (id: string) =>
      run("Could not re-arm the alert", async () => {
        const { alert } = await rearmAlert(id);
        setAlerts((as) => as.map((a) => (a.id === id ? alert : a)));
      }),
    [run, setAlerts],
  );

  const clearTriggered = useCallback(
    () =>
      run("Could not clear triggered alerts", async () => {
        await clearTriggeredAlerts();
        setAlerts((as) => as.filter((a) => a.armed));
      }),
    [run, setAlerts],
  );

  /** Sends a test message to the configured channels → which delivered it, or null on error. */
  const testChannels = useCallback(async (): Promise<ChannelTestResult | null> => {
    try {
      const res = await testAlertChannels();
      setActionError(null);
      return res.results;
    } catch (err) {
      setActionError(`Test failed: ${(err as Error).message}`);
      return null;
    }
  }, []);

  const error = actionError ?? (offline ? OFFLINE : null);
  return { alerts, add, remove, rearm, clearTriggered, channels, testChannels, error };
}
