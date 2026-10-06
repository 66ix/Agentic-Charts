"use client";

import clsx from "clsx";
import { Bell, BellRing, RotateCcw, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";

import { requestNotificationPermission } from "@/hooks/useAlerts";
import { describeAlert } from "@/lib/alerts";
import { displaySymbol, formatPrice } from "@/lib/format";
import type { PriceAlert } from "@/lib/types";

interface Props {
  open: boolean;
  alerts: PriceAlert[];
  symbol: string;
  onRemove(id: string): void;
  onRearm(id: string): void;
  onClearTriggered(): void;
  onPickSymbol(symbol: string): void;
  onClose(): void;
}

export default function AlertsPanel(p: Props) {
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">("unsupported");
  useEffect(() => {
    if (typeof Notification !== "undefined") setPermission(Notification.permission);
  }, [p.open]);

  if (!p.open) return null;
  const sorted = [...p.alerts].sort(
    (a, b) => Number(b.armed) - Number(a.armed) || Number(b.symbol === p.symbol) - Number(a.symbol === p.symbol) ||
      b.created_at - a.created_at,
  );
  const triggered = p.alerts.filter((a) => !a.armed).length;

  return (
    <div className="pointer-events-auto absolute right-3 top-3 z-40 flex max-h-[70%] w-80 flex-col overflow-hidden rounded-xl border border-line bg-panel/95 shadow-2xl backdrop-blur">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-xs">
        <Bell className="h-4 w-4 text-yellow-300" />
        <span className="font-semibold text-ink">Price alerts</span>
        <div className="flex-1" />
        {triggered > 0 && (
          <button type="button" onClick={p.onClearTriggered} className="btn-ghost h-6 px-1.5 text-[11px]">
            Clear {triggered} triggered
          </button>
        )}
        <button type="button" onClick={p.onClose} className="btn-ghost h-6 w-6 p-0" aria-label="Close alerts">
          <X className="h-4 w-4" />
        </button>
      </div>
      {permission === "default" && (
        <button
          type="button"
          onClick={() => {
            requestNotificationPermission();
            setTimeout(() => setPermission(Notification.permission), 1500);
          }}
          className="border-b border-line px-3 py-2 text-left text-[11px] text-accent hover:bg-panel2"
        >
          Turn on desktop notifications so alerts reach you in other tabs
        </button>
      )}
      <div className="overflow-y-auto">
        {sorted.length === 0 && (
          <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">
            No alerts yet. Ask the agent (&quot;alert me if price enters the supply zone&quot;, &quot;alert me at
            25.4&quot;), or select a horizontal ray or rectangle and press the bell in the toolbar.
          </p>
        )}
        {sorted.map((a) => (
          <div key={a.id} className={clsx("flex items-start gap-2 border-b border-line/60 px-3 py-2 text-[12px]", !a.armed && "opacity-60")}>
            {a.armed ? <Bell className="mt-0.5 h-3.5 w-3.5 shrink-0 text-yellow-300" /> : <BellRing className="mt-0.5 h-3.5 w-3.5 shrink-0 text-mute" />}
            <div className="min-w-0 flex-1">
              <button type="button" onClick={() => p.onPickSymbol(a.symbol)} className="font-medium text-ink hover:text-accent">
                {displaySymbol(a.symbol)}
              </button>{" "}
              <span className="text-mute">{describeAlert(a)}</span>
              <div className="truncate text-[11px] text-mute">
                {a.label}
                {a.triggered_at &&
                  ` · fired ${new Date(a.triggered_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}` +
                    (a.triggered_price != null ? ` at ${formatPrice(a.triggered_price)}` : "")}
              </div>
            </div>
            {!a.armed && (
              <button type="button" onClick={() => p.onRearm(a.id)} className="btn-ghost h-6 w-6 p-0" title="Re-arm">
                <RotateCcw className="h-3.5 w-3.5" />
              </button>
            )}
            <button type="button" onClick={() => p.onRemove(a.id)} className="btn-ghost h-6 w-6 p-0 hover:text-down" title="Delete alert">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>
      <p className="px-3 py-2 text-[10px] text-mute">Alerts are checked while this app is open in a browser tab.</p>
    </div>
  );
}
