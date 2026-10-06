"use client";

import clsx from "clsx";
import { Bell, BellRing, RotateCcw, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";

import { requestNotificationPermission, type ChannelTestResult } from "@/hooks/useAlerts";
import { describeAlert } from "@/lib/alerts";
import { displaySymbol, formatPrice } from "@/lib/format";
import type { AlertChannels, PriceAlert } from "@/lib/types";

const CHANNEL_NAMES: Record<keyof AlertChannels, string> = { telegram: "Telegram", discord: "Discord" };

interface Props {
  open: boolean;
  alerts: PriceAlert[];
  symbol: string;
  /** Notification channels configured on the backend; null until known. */
  channels: AlertChannels | null;
  error: string | null;
  onTestChannels(): Promise<ChannelTestResult | null>;
  onRemove(id: string): void;
  onRearm(id: string): void;
  onClearTriggered(): void;
  onPickSymbol(symbol: string): void;
  onClose(): void;
}

export default function AlertsPanel(p: Props) {
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">("unsupported");
  const [testing, setTesting] = useState(false);
  const [testNote, setTestNote] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    if (typeof Notification !== "undefined") setPermission(Notification.permission);
    setTestNote(null);
  }, [p.open]);

  if (!p.open) return null;
  const sorted = [...p.alerts].sort(
    (a, b) => Number(b.armed) - Number(a.armed) || Number(b.symbol === p.symbol) - Number(a.symbol === p.symbol) ||
      b.created_at - a.created_at,
  );
  const triggered = p.alerts.filter((a) => !a.armed).length;
  const configured = p.channels
    ? (Object.keys(CHANNEL_NAMES) as (keyof AlertChannels)[]).filter((c) => p.channels?.[c])
    : [];

  const sendTest = async () => {
    setTesting(true);
    setTestNote(null);
    const res = await p.onTestChannels();
    setTesting(false);
    if (!res) return; // the hook reports the error
    const sent = configured.filter((c) => res[c]).map((c) => CHANNEL_NAMES[c]);
    const failed = configured.filter((c) => res[c] === false).map((c) => CHANNEL_NAMES[c]);
    const parts: string[] = [];
    if (sent.length) parts.push(`Sent to ${sent.join(" and ")}.`);
    if (failed.length) parts.push(`${failed.join(" and ")} failed; see the backend log.`);
    setTestNote({ ok: failed.length === 0, text: parts.join(" ") });
  };

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
      {p.channels && (
        <div className="border-b border-line px-3 py-2 text-[11px]">
          {configured.length ? (
            <div className="flex items-center gap-1.5">
              <span className="text-mute">Notify</span>
              {configured.map((c) => (
                <span key={c} className="inline-flex items-center gap-1 rounded border border-line bg-panel2 px-1.5 py-0.5 text-ink">
                  <span className="h-1.5 w-1.5 rounded-full bg-up" />
                  {CHANNEL_NAMES[c]}
                </span>
              ))}
              <div className="flex-1" />
              <button type="button" onClick={sendTest} disabled={testing} className="btn-ghost h-6 px-1.5 text-[11px] disabled:opacity-50">
                {testing ? "Sending…" : "Send test"}
              </button>
            </div>
          ) : (
            <span className="text-mute">Only in this browser — set TELEGRAM_* or DISCORD_WEBHOOK_URL on the backend</span>
          )}
          {testNote && <div className={clsx("mt-1", testNote.ok ? "text-up" : "text-down")}>{testNote.text}</div>}
        </div>
      )}
      {p.error && <div className="border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">{p.error}</div>}
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
      <p className="px-3 py-2 text-[10px] text-mute">Alerts are checked on the server, so they fire even with this tab closed.</p>
    </div>
  );
}
