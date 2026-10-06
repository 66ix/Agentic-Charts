"use client";

import { useCallback, useEffect, useRef } from "react";

import { evaluateAlert, newAlert } from "@/lib/alerts";
import { WS_URL } from "@/lib/config";
import type { AlertSpec, PriceAlert } from "@/lib/types";

import { usePersistentState } from "./usePersistentState";

export interface FiredAlert {
  alert: PriceAlert;
  price: number;
}

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

/**
 * Price alerts, stored in localStorage and checked against live 1m candles.
 * One WebSocket per symbol with armed alerts (the backend shares the upstream
 * Binance stream), so alerts fire for any symbol while the app is open.
 */
export function useAlerts(onFire: (fired: FiredAlert[]) => void) {
  const [alerts, setAlerts] = usePersistentState<PriceAlert[]>("ac:alerts", []);
  const alertsRef = useRef(alerts);
  alertsRef.current = alerts;
  const onFireRef = useRef(onFire);
  onFireRef.current = onFire;

  const onPrice = useCallback(
    (symbol: string, price: number) => {
      let changed = false;
      const fired: FiredAlert[] = [];
      const next = alertsRef.current.map((a) => {
        if (a.symbol !== symbol || !a.armed) return a;
        const r = evaluateAlert(a, price);
        if (r.alert !== a) changed = true;
        if (r.fired) fired.push({ alert: r.alert, price });
        return r.alert;
      });
      if (!changed) return;
      alertsRef.current = next;
      setAlerts(next);
      if (fired.length) {
        beep();
        fired.forEach((f) => notify(f.alert, price));
        onFireRef.current(fired);
      }
    },
    [setAlerts],
  );

  // Keep one socket per watched symbol.
  const watched = [...new Set(alerts.filter((a) => a.armed).map((a) => a.symbol))].sort().join(",");
  useEffect(() => {
    if (!watched) return;
    const sockets = new Map<string, WebSocket>();
    const timers: ReturnType<typeof setTimeout>[] = [];
    let disposed = false;

    const open = (symbol: string) => {
      if (disposed) return;
      const ws = new WebSocket(`${WS_URL}/ws/klines?symbol=${encodeURIComponent(symbol)}&interval=1m`);
      sockets.set(symbol, ws);
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "kline" && msg.candle) onPrice(symbol, Number(msg.candle.close));
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        if (!disposed) timers.push(setTimeout(() => open(symbol), 5000));
      };
    };
    watched.split(",").forEach(open);
    return () => {
      disposed = true;
      timers.forEach(clearTimeout);
      sockets.forEach((ws) => {
        ws.onclose = null;
        ws.close();
      });
    };
  }, [watched, onPrice]);

  const add = useCallback(
    (specs: AlertSpec[], symbol: string) => {
      if (!specs.length) return;
      requestNotificationPermission();
      setAlerts((as) => [...as, ...specs.map((s) => newAlert(s, symbol))].slice(-50));
    },
    [setAlerts],
  );
  const remove = useCallback((id: string) => setAlerts((as) => as.filter((a) => a.id !== id)), [setAlerts]);
  const rearm = useCallback(
    (id: string) =>
      setAlerts((as) =>
        as.map((a) => (a.id === id ? { ...a, armed: true, last_side: undefined, triggered_at: undefined } : a)),
      ),
    [setAlerts],
  );
  const clearTriggered = useCallback(() => setAlerts((as) => as.filter((a) => a.armed)), [setAlerts]);

  return { alerts, add, remove, rearm, clearTriggered };
}
