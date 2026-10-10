"use client";

import { DESK_CHANGED } from "@/lib/desk";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  clearTriggeredAlerts,
  createAlerts,
  deleteAlert,
  fetchAlerts,
  rearmAlert,
  testAlertChannels,
  updateAlert,
} from "@/lib/api";
import {
  clearAlertHistory,
  createSignalAlerts,
  createZoneTrigger,
  deleteSignalAlert,
  fetchAlertHistory,
  fetchBriefSettings,
  previewBrief,
  previewSignalAlert,
  previewZoneTrigger,
  saveBriefSettings,
  sendBrief,
  updateSignalAlert,
  type AlertHistoryItem,
  type AlertPatch,
  type SignalAlert,
  type SignalAlertPatch,
  type SignalFired,
  type SignalId,
} from "@/lib/alerts";
import { WS_URL } from "@/lib/config";
import type { AlertChannels, AlertSpec, Interval, PriceAlert, ZoneTriggerSpec } from "@/lib/types";

import { usePersistentState } from "./usePersistentState";

/** Alerts from before they moved to the backend: uploaded once, then removed. */
const LEGACY_KEY = "ac:alerts";
/** The backend's last snapshots, shown while it is unreachable. */
const CACHE_KEY = "ac:alerts-cache";
const SIGNAL_CACHE_KEY = "ac:signal-alerts-cache";
const OFFLINE = "Can't reach the alerts service. Showing the last known alerts; reconnecting…";
const HISTORY_LIMIT = 500;

export interface FiredAlert {
  alert: PriceAlert;
  price: number;
}

export type { SignalFired };

export type ChannelTestResult = Partial<Record<keyof AlertChannels, boolean>>;

function beep(freq = 880) {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = freq;
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

function notify(title: string, body: string, tag: string) {
  try {
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      new Notification(title, { body, tag });
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
  repeat: a.repeat ?? false,
  expires_at: a.expires_at ?? null,
  note: a.note ?? "",
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

interface WsMessage {
  type?: string;
  alerts?: unknown[];
  alert?: PriceAlert | SignalAlert;
  price?: number;
  text?: string;
  time?: number;
  item?: AlertHistoryItem;
  trade_id?: string;
}

/**
 * Price alerts, signal alerts, the alert history and the brief, all stored and checked by the backend so they
 * fire with the tab closed (and reach Telegram / Discord when configured). `/ws/alerts` pushes a snapshot of each
 * alert list on every change, which becomes our state, a `fired` / `signal_fired` event per fire, which beeps,
 * shows a desktop notification and calls `onFire` / `onSignal` for the toast, and each new history item. The
 * last snapshots are cached in localStorage for display while offline.
 */
/** `onEvent` gets each history item as it happens (desk calls, trade advice, market alerts…), for toasts. */
export function useAlerts(
  onFire: (fired: FiredAlert[]) => void,
  onSignal?: (fired: SignalFired) => void,
  onEvent?: (item: AlertHistoryItem) => void,
) {
  const [alerts, setAlerts] = usePersistentState<PriceAlert[]>(CACHE_KEY, []);
  const [signalAlerts, setSignalAlerts] = usePersistentState<SignalAlert[]>(SIGNAL_CACHE_KEY, []);
  const [history, setHistory] = useState<AlertHistoryItem[]>([]);
  const [channels, setChannels] = useState<AlertChannels | null>(null);
  const [offline, setOffline] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const onFireRef = useRef(onFire);
  onFireRef.current = onFire;
  const onSignalRef = useRef(onSignal);
  onSignalRef.current = onSignal;
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const addHistory = useCallback((items: AlertHistoryItem[]) => {
    setHistory((h) => {
      const seen = new Set(h.map((x) => x.id));
      const fresh = items.filter((x) => !seen.has(x.id));
      return fresh.length ? [...fresh, ...h].sort((a, b) => b.time - a.time).slice(0, HISTORY_LIMIT) : h;
    });
  }, []);

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
        fetchAlertHistory(200)
          .then((res) => !disposed && addHistory(res.items))
          .catch(() => {
            /* history fills from live events */
          });
      };
      ws.onmessage = (ev) => {
        let msg: WsMessage;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === "snapshot" && Array.isArray(msg.alerts)) {
          const list = msg.alerts as PriceAlert[];
          setAlerts(list);
          migrate(list);
        } else if (msg.type === "signal_snapshot" && Array.isArray(msg.alerts)) {
          setSignalAlerts(msg.alerts as SignalAlert[]);
        } else if (msg.type === "fired" && msg.alert) {
          const alert = msg.alert as PriceAlert;
          const price = Number(msg.price);
          beep();
          notify(`${alert.symbol} alert`, `${alert.label || "Price alert"}: price ${price}`, alert.id);
          onFireRef.current([{ alert, price }]);
        } else if (msg.type === "signal_fired" && msg.alert) {
          const fired: SignalFired = {
            alert: msg.alert as SignalAlert,
            text: msg.text ?? "",
            price: Number(msg.price),
            time: Number(msg.time),
          };
          beep(660);
          notify(`${fired.alert.symbol} ${fired.alert.interval} signal`, fired.text, `signal-${fired.alert.id}`);
          onSignalRef.current?.(fired);
        } else if (msg.type === "trade_advice" && msg.text) {
          // The trade manager (Live trades tab) has advice on an open trade.
          beep(880);
          notify("Live trade", String(msg.text), `trade-${msg.trade_id}`);
        } else if (msg.type === "history" && msg.item) {
          addHistory([msg.item]);
          onEventRef.current?.(msg.item);
        } else if (msg.type === "desk" || msg.type === "desk_scored") {
          // The desk made, filled or closed a call: its tab and the charts refresh now, not on their next poll.
          window.dispatchEvent(new CustomEvent(DESK_CHANGED));
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
  }, [setAlerts, setSignalAlerts, addHistory]);

  const run = useCallback(async (failure: string, fn: () => Promise<void>) => {
    try {
      await fn();
      setActionError(null);
      return true;
    } catch (err) {
      setActionError(`${failure}: ${(err as Error).message}`);
      return false;
    }
  }, []);

  // --------------------------------------------------------------- price alerts

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

  /** Edit an alert, e.g. after dragging its line: `update(id, { price })`. Resolves to false (and sets `error`)
   *  when the backend refused the edit. Moving a level never fires the alert. */
  const update = useCallback(
    (id: string, patch: AlertPatch) =>
      run("Could not update the alert", async () => {
        const { alert } = await updateAlert(id, patch);
        setAlerts((as) => as.map((a) => (a.id === id ? alert : a)));
      }),
    [run, setAlerts],
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

  // -------------------------------------------------------------- signal alerts

  /** One signal alert per symbol → the saved alerts, or null (and `error`) on failure. */
  const addSignal = useCallback(
    async (body: { symbols: string[]; interval: Interval; signal: SignalId; repeat?: boolean; note?: string }) => {
      requestNotificationPermission();
      try {
        const res = await createSignalAlerts(body);
        setSignalAlerts((as) => [...res.alerts, ...as.filter((a) => !res.alerts.some((x) => x.id === a.id))]);
        setActionError(null);
        return res.alerts;
      } catch (err) {
        setActionError(`Signal alert not saved: ${(err as Error).message}`);
        return null;
      }
    },
    [setSignalAlerts],
  );

  /** A lower-timeframe confirmation inside a zone → the saved alert, or null (and `error`) on failure. */
  const addTrigger = useCallback(
    async (spec: ZoneTriggerSpec) => {
      requestNotificationPermission();
      try {
        const { alert } = await createZoneTrigger(spec);
        setSignalAlerts((as) => [alert, ...as.filter((a) => a.id !== alert.id)]);
        setActionError(null);
        return alert;
      } catch (err) {
        setActionError(`Trigger alert not saved: ${(err as Error).message}`);
        return null;
      }
    },
    [setSignalAlerts],
  );

  const updateSignal = useCallback(
    (id: string, patch: SignalAlertPatch) =>
      run("Could not update the signal alert", async () => {
        const { alert } = await updateSignalAlert(id, patch);
        setSignalAlerts((as) => as.map((a) => (a.id === id ? alert : a)));
      }),
    [run, setSignalAlerts],
  );

  const removeSignal = useCallback(
    (id: string) =>
      run("Could not delete the signal alert", async () => {
        try {
          await deleteSignalAlert(id);
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 404)) throw err;
        }
        setSignalAlerts((as) => as.filter((a) => a.id !== id));
      }),
    [run, setSignalAlerts],
  );

  // -------------------------------------------------------------------- history

  const refreshHistory = useCallback(
    () =>
      run("Could not load the alert history", async () => {
        const res = await fetchAlertHistory(200);
        setHistory(res.items);
      }),
    [run],
  );

  const clearHistory = useCallback(
    () =>
      run("Could not clear the history", async () => {
        await clearAlertHistory();
        setHistory([]);
      }),
    [run],
  );

  /** Brief calls; they throw ApiError, so the Brief tab can show the reason next to its buttons. */
  const brief = useMemo(
    () => ({ load: fetchBriefSettings, save: saveBriefSettings, preview: previewBrief, send: sendBrief }),
    [],
  );

  const error = actionError ?? (offline ? OFFLINE : null);
  return {
    alerts,
    add,
    update,
    remove,
    rearm,
    clearTriggered,
    channels,
    testChannels,
    error,
    /** Dismiss the current action error. */
    clearError: () => setActionError(null),
    offline,
    signalAlerts,
    addSignal,
    updateSignal,
    removeSignal,
    previewSignal: previewSignalAlert,
    addTrigger,
    previewTrigger: previewZoneTrigger,
    history,
    refreshHistory,
    clearHistory,
    brief,
  };
}

/** Everything `useAlerts` returns; the Alerts panel takes it as its `api` prop. */
export type AlertsApi = ReturnType<typeof useAlerts>;
