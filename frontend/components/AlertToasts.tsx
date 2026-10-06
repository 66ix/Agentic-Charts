"use client";

import { BellRing, X } from "lucide-react";

import { describeAlert } from "@/lib/alerts";
import { displaySymbol, formatPrice } from "@/lib/format";
import type { PriceAlert } from "@/lib/types";

export interface Toast {
  id: string;
  alert: PriceAlert;
  price: number;
}

export default function AlertToasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss(id: string): void }) {
  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <div key={t.id} role="alert" className="pointer-events-auto flex items-start gap-2 rounded-lg border border-yellow-400/40 bg-panel px-3 py-2.5 text-[12px] shadow-2xl">
          <BellRing className="mt-0.5 h-4 w-4 shrink-0 text-yellow-300" />
          <div className="min-w-0 flex-1">
            <div className="font-medium text-ink">
              {displaySymbol(t.alert.symbol)} {describeAlert(t.alert, true)}
            </div>
            <div className="text-mute">
              {t.alert.label} · price {formatPrice(t.price)}
            </div>
          </div>
          <button type="button" onClick={() => onDismiss(t.id)} className="btn-ghost h-6 w-6 p-0" aria-label="Dismiss">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}
