"use client";

import clsx from "clsx";
import { Activity, BellRing, BrainCircuit, Crosshair, Gauge, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import type { SignalFired } from "@/hooks/useAlerts";
import { describeAlert, signalName } from "@/lib/alerts";
import { displaySymbol, formatPrice } from "@/lib/format";
import type { EventToast } from "@/lib/notifyClient";
import type { PriceAlert } from "@/lib/types";

/** A fired price alert (what the workspace builds today: `{id, alert, price}`). */
export interface PriceToast {
  id: string;
  alert: PriceAlert;
  price: number;
}

/** Anything else worth a toast: a signal alert that fired, a brief that went out. */
export interface MessageToast {
  id: string;
  kind: "signal" | "brief";
  title: string;
  text: string;
  symbol?: string;
  interval?: string;
}

export type Toast = PriceToast | MessageToast | EventToast;

/** Toasts other than your own price alerts go away by themselves after this long (paused while hovered). */
const AUTO_DISMISS_MS = 12_000;

/** The toast for a `signal_fired` event (pass it as useAlerts' second callback). */
export function signalToast(id: string, f: SignalFired): MessageToast {
  return {
    id,
    kind: "signal",
    title: `${displaySymbol(f.alert.symbol)} ${f.alert.interval} · ${signalName(f.alert.signal)}`,
    text: f.text,
    symbol: f.alert.symbol,
    interval: f.alert.interval,
  };
}

/** Where a click on the toast goes: the coin (and timeframe) and the tab it belongs to. */
export interface ToastTarget {
  symbol?: string;
  interval?: string;
  tab?: string;
}

function targetOf(t: Toast): ToastTarget {
  if ("alert" in t) return { symbol: t.alert.symbol, tab: "alerts" };
  if ("tab" in t) return { symbol: t.symbol, tab: t.tab };
  return { symbol: t.symbol, interval: t.interval, tab: t.kind === "signal" ? "alerts" : undefined };
}

const ICONS = { desk: BrainCircuit, trade: Crosshair, metric: Gauge, signal: Activity, brief: Activity };

function ToastCard({ t, onDismiss, onOpen }: { t: Toast; onDismiss(id: string): void; onOpen?(to: ToastTarget): void }) {
  const price = "alert" in t;
  const [hover, setHover] = useState(false);
  const dismiss = useRef(onDismiss);
  useEffect(() => {
    dismiss.current = onDismiss;
  });
  useEffect(() => {
    if (price || hover) return;
    const id = setTimeout(() => dismiss.current(t.id), AUTO_DISMISS_MS);
    return () => clearTimeout(id);
  }, [price, hover, t.id]);
  const Icon = price ? BellRing : ICONS[t.kind];
  const target = targetOf(t);
  const clickable = !!onOpen && (!!target.symbol || !!target.tab);
  return (
    <div
      role="alert"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      className={clsx(
        "pointer-events-auto flex items-start gap-2 rounded-lg border bg-panel px-3 py-2.5 text-[12px] shadow-2xl",
        price ? "border-yellow-400/40" : !price && t.kind === "desk" ? "border-violet-400/40" : "border-accent/40",
      )}
    >
      <Icon className={clsx("mt-0.5 h-4 w-4 shrink-0", price ? "text-yellow-300" : t.kind === "desk" ? "text-violet-300" : "text-accent")} />
      <button
        type="button"
        disabled={!clickable}
        onClick={() => {
          onOpen?.(target);
          onDismiss(t.id);
        }}
        className={clsx("min-w-0 flex-1 text-left", clickable && "cursor-pointer hover:opacity-90")}
        title={clickable ? "Open it" : undefined}
      >
        {price ? (
          <>
            <div className="font-medium text-ink">
              {displaySymbol(t.alert.symbol)} {describeAlert(t.alert, true)}
            </div>
            <div className="text-mute">
              {t.alert.label} · price {formatPrice(t.price)}
              {t.alert.repeat ? " · stays armed" : ""}
            </div>
            {t.alert.note && <div className="mt-0.5 text-mute">{t.alert.note}</div>}
          </>
        ) : (
          <>
            <div className="font-medium text-ink">{t.title}</div>
            <div className="line-clamp-4 whitespace-pre-line break-words text-mute">{t.text}</div>
          </>
        )}
      </button>
      <button type="button" onClick={() => onDismiss(t.id)} className="btn-ghost h-6 w-6 p-0" aria-label="Dismiss">
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export default function AlertToasts({
  toasts,
  onDismiss,
  onOpen,
}: {
  toasts: Toast[];
  onDismiss(id: string): void;
  /** Clicking a toast opens its coin and tab. */
  onOpen?(to: ToastTarget): void;
}) {
  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <ToastCard key={t.id} t={t} onDismiss={onDismiss} onOpen={onOpen} />
      ))}
    </div>
  );
}
