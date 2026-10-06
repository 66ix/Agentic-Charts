"use client";

import clsx from "clsx";
import { Activity, BellRing, X } from "lucide-react";

import type { SignalFired } from "@/hooks/useAlerts";
import { describeAlert, signalName } from "@/lib/alerts";
import { displaySymbol, formatPrice } from "@/lib/format";
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
}

export type Toast = PriceToast | MessageToast;

/** The toast for a `signal_fired` event (pass it as useAlerts' second callback). */
export function signalToast(id: string, f: SignalFired): MessageToast {
  return { id, kind: "signal", title: `${displaySymbol(f.alert.symbol)} ${f.alert.interval} · ${signalName(f.alert.signal)}`, text: f.text };
}

export default function AlertToasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss(id: string): void }) {
  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
      {toasts.map((t) => {
        const price = "alert" in t;
        return (
          <div
            key={t.id}
            role="alert"
            className={clsx(
              "pointer-events-auto flex items-start gap-2 rounded-lg border bg-panel px-3 py-2.5 text-[12px] shadow-2xl",
              price ? "border-yellow-400/40" : "border-accent/40",
            )}
          >
            {price ? (
              <BellRing className="mt-0.5 h-4 w-4 shrink-0 text-yellow-300" />
            ) : (
              <Activity className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
            )}
            <div className="min-w-0 flex-1">
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
            </div>
            <button type="button" onClick={() => onDismiss(t.id)} className="btn-ghost h-6 w-6 p-0" aria-label="Dismiss">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
