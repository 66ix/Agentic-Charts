"use client";

import clsx from "clsx";
import {
  Activity,
  Bell,
  BellRing,
  Check,
  Crosshair,
  Eye,
  Newspaper,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Repeat,
  RotateCcw,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import MarketAlerts from "@/components/MarketAlerts";
import { requestNotificationPermission, type AlertsApi, type ChannelTestResult } from "@/hooks/useAlerts";
import { usePersistentState } from "@/hooks/usePersistentState";
import {
  BRIEF_SECTION_NAMES,
  CONFIRM_OPTIONS,
  EXPIRY_OPTIONS,
  SIGNAL_OPTIONS,
  TRIGGER_INTERVALS,
  browserTimeZone,
  describeAlert,
  describeTrigger,
  expiryLabel,
  formatWhen,
  signalName,
  timeAgo,
  timeZones,
  toLocalInput,
  type AlertHistoryItem,
  type AlertPatch,
  type BriefPreview,
  type BriefSections,
  type BriefSettings,
  type BriefStatus,
  type ChartZone,
  type SignalAlert,
  type SignalId,
  type SignalPreview,
  type TriggerPreview,
} from "@/lib/alerts";
import type { DockPanelProps } from "@/lib/dock";
import { displaySymbol, formatPrice } from "@/lib/format";
import {
  TIMEFRAMES,
  type AlertChannels,
  type AlertSpec,
  type Confirmation,
  type Interval,
  type Overlay,
  type PriceAlert,
  type TriggerInterval,
  type TriggerZoneKind,
  type ZoneTriggerSpec,
} from "@/lib/types";

const CHANNEL_NAMES: Record<keyof AlertChannels, string> = { telegram: "Telegram", discord: "Discord" };
const INPUT =
  "h-7 min-w-0 rounded border border-line bg-panel2 px-2 text-[12px] text-ink outline-none focus:border-accent";
const LABEL = "text-[10px] font-semibold uppercase tracking-wide text-mute";
const PREVIEW_KEY = "alerts:signal-preview";
const TRIGGER_PREVIEW_KEY = "alerts:trigger-preview";

type Tab = "price" | "signals" | "triggers" | "history" | "brief";
const TABS: { id: Tab; label: string }[] = [
  { id: "price", label: "Price" },
  { id: "signals", label: "Signals" },
  { id: "triggers", label: "Triggers" },
  { id: "history", label: "History" },
  { id: "brief", label: "Brief" },
];

/**
 * Props. Mount it as a dock tab with the dock props plus `api` (everything `useAlerts` returns):
 *   `<AlertsPanel {...dockProps} api={alerts} />`
 * The floating panel mounted today (`open`, `onClose` and the separate alert callbacks) keeps working; without
 * `api` only the Price tab shows. With `api`, its values win over the separate props.
 */
export interface AlertsPanelProps extends Partial<DockPanelProps> {
  /** Everything `useAlerts` returns: enables editing, new alerts, signal alerts, history and the brief. */
  api?: AlertsApi;
  alerts?: PriceAlert[];
  /** Notification channels configured on the backend; null until known. */
  channels?: AlertChannels | null;
  error?: string | null;
  onTestChannels?(): Promise<ChannelTestResult | null>;
  onRemove?(id: string): void;
  onRearm?(id: string): void;
  onClearTriggered?(): void;
  /** Zones on the chart (AI zones, drawn rectangles) the Triggers tab offers to watch. */
  zones?: ChartZone[];
  /** Floating mode (as mounted before the dock): rendered only while `open`, with a close button. Leave both
   *  out for a dock tab. */
  open?: boolean;
  onClose?(): void;
}

/** "65,000.5" / "65 000" → 65000.5; NaN when not a positive number. */
function parsePrice(s: string): number {
  const v = Number(s.replace(/[,\s]/g, ""));
  return Number.isFinite(v) && v > 0 ? v : NaN;
}

/** Armed signal alerts, or (`triggers`) armed zone trigger alerts. */
function armedCount(list: SignalAlert[], triggers: boolean): number {
  return list.filter((a) => a.armed && (a.signal === "zone_trigger") === triggers).length;
}

function priceInput(p: number | null | undefined): string {
  return p != null && Number.isFinite(p) ? formatPrice(p).replace(/,/g, "") : "";
}

export default function AlertsPanel(p: AlertsPanelProps) {
  const api = p.api;
  const [tab, setTab] = usePersistentState<Tab>("ac:alerts-tab", "price");
  const floating = p.open !== undefined || p.onClose !== undefined;
  const tabs = api ? TABS : TABS.slice(0, 1);
  const active: Tab = tabs.some((t) => t.id === tab) ? tab : "price";
  const error = api ? api.error : (p.error ?? null);

  if (p.open === false) return null;
  return (
    <div
      className={clsx(
        "flex min-h-0 flex-col text-xs",
        floating
          ? "pointer-events-auto absolute right-3 top-3 z-40 max-h-[75%] w-96 overflow-hidden rounded-xl border border-line bg-panel/95 shadow-2xl backdrop-blur"
          : "h-full bg-panel",
      )}
    >
      <div className="flex items-center gap-1 border-b border-line px-2 py-1.5">
        <Bell className="mx-1 h-4 w-4 shrink-0 text-yellow-300" />
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={clsx(
              "rounded px-2 py-1 text-[12px]",
              active === t.id ? "bg-panel2 font-semibold text-ink" : "text-mute hover:text-ink",
            )}
          >
            {t.label}
            {(t.id === "signals" || t.id === "triggers") && api && armedCount(api.signalAlerts, t.id === "triggers") > 0 && (
              <span className="ml-1 text-[10px] text-accent">{armedCount(api.signalAlerts, t.id === "triggers")}</span>
            )}
          </button>
        ))}
        <div className="flex-1" />
        {p.onClose && (
          <button type="button" onClick={p.onClose} className="btn-ghost h-6 w-6 p-0" aria-label="Close alerts">
            <X className="h-4 w-4" />
          </button>
        )}
      </div>
      {error && (
        <div className="flex items-start gap-2 border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">
          <span className="flex-1">{error}</span>
          {api && !api.offline && (
            <button type="button" onClick={api.clearError} className="shrink-0 hover:text-ink" aria-label="Dismiss">
              <X className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {active === "price" && <PriceTab p={p} />}
        {active === "signals" && api && <SignalsTab p={p} api={api} />}
        {active === "triggers" && api && <TriggersTab p={p} api={api} />}
        {active === "history" && api && <HistoryTab p={p} api={api} />}
        {active === "brief" && api && <BriefTab p={p} api={api} />}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ price tab --

function PriceTab({ p }: { p: AlertsPanelProps }) {
  const api = p.api;
  const source = api?.alerts ?? p.alerts;
  const alerts = useMemo(() => source ?? [], [source]);
  const channels = api ? api.channels : (p.channels ?? null);
  const symbol = p.symbol ?? "";
  const onRemove = api?.remove ?? p.onRemove;
  const onRearm = api?.rearm ?? p.onRearm;
  const onClearTriggered = api?.clearTriggered ?? p.onClearTriggered;
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);

  const sorted = useMemo(
    () =>
      [...alerts].sort(
        (a, b) =>
          Number(b.armed) - Number(a.armed) ||
          Number(b.symbol === symbol) - Number(a.symbol === symbol) ||
          b.created_at - a.created_at,
      ),
    [alerts, symbol],
  );
  const triggered = alerts.filter((a) => !a.armed).length;

  return (
    <div>
      <ChannelBar channels={channels} onTest={api?.testChannels ?? p.onTestChannels} />
      <NotificationPrompt />
      <div className="flex items-center gap-2 border-b border-line px-3 py-1.5">
        {api && symbol && (
          <button
            type="button"
            onClick={() => setCreating((c) => !c)}
            className={clsx("btn-ghost h-6 px-1.5 text-[11px]", creating && "bg-panel2 text-ink")}
          >
            <Plus className="h-3.5 w-3.5" /> New alert
          </button>
        )}
        <div className="flex-1" />
        {triggered > 0 && onClearTriggered && (
          <button type="button" onClick={() => onClearTriggered()} className="btn-ghost h-6 px-1.5 text-[11px]">
            Clear {triggered} fired / expired
          </button>
        )}
      </div>
      {creating && api && symbol && (
        <NewAlertForm symbol={symbol} price={p.price ?? null} onAdd={api.add} onDone={() => setCreating(false)} />
      )}
      {sorted.length === 0 && (
        <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">
          No price alerts yet. Press <span className="text-ink">New alert</span>, ask the agent (&quot;alert me if price
          enters the supply zone&quot;, &quot;alert me at 25.4&quot;), or select a horizontal ray or rectangle and press
          the bell in the toolbar.
        </p>
      )}
      {sorted.map((a) =>
        editing === a.id && api ? (
          <AlertEditor key={a.id} alert={a} onSave={(patch) => api.update(a.id, patch)} onDone={() => setEditing(null)} />
        ) : (
          <AlertRow
            key={a.id}
            alert={a}
            onPick={() => p.onPickSymbol?.(a.symbol)}
            onEdit={api ? () => setEditing(a.id) : undefined}
            onToggleRepeat={api ? () => void api.update(a.id, { repeat: !a.repeat }) : undefined}
            onRearm={onRearm ? () => onRearm(a.id) : undefined}
            onRemove={onRemove ? () => onRemove(a.id) : undefined}
          />
        ),
      )}
      {api && <MarketAlerts />}
      <p className="px-3 py-2 text-[10px] text-mute">Alerts are checked on the server, so they fire even with this tab closed.</p>
    </div>
  );
}

function ChannelBar({ channels, onTest }: { channels: AlertChannels | null; onTest?: () => Promise<ChannelTestResult | null> }) {
  const [testing, setTesting] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  if (!channels) return null;
  const configured = (Object.keys(CHANNEL_NAMES) as (keyof AlertChannels)[]).filter((c) => channels[c]);

  const sendTest = async () => {
    if (!onTest) return;
    setTesting(true);
    setNote(null);
    const res = await onTest();
    setTesting(false);
    if (!res) return; // the hook reports the error
    const sent = configured.filter((c) => res[c]).map((c) => CHANNEL_NAMES[c]);
    const failed = configured.filter((c) => res[c] === false).map((c) => CHANNEL_NAMES[c]);
    const parts: string[] = [];
    if (sent.length) parts.push(`Sent to ${sent.join(" and ")}.`);
    if (failed.length) parts.push(`${failed.join(" and ")} failed; see the backend log.`);
    setNote({ ok: failed.length === 0, text: parts.join(" ") });
  };

  return (
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
          {onTest && (
            <button type="button" onClick={sendTest} disabled={testing} className="btn-ghost h-6 px-1.5 text-[11px] disabled:opacity-50">
              {testing ? "Sending…" : "Send test"}
            </button>
          )}
        </div>
      ) : (
        <span className="text-mute">Only in this browser — set TELEGRAM_* or DISCORD_WEBHOOK_URL on the backend</span>
      )}
      {note && <div className={clsx("mt-1", note.ok ? "text-up" : "text-down")}>{note.text}</div>}
    </div>
  );
}

function NotificationPrompt() {
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">("unsupported");
  useEffect(() => {
    if (typeof Notification !== "undefined") setPermission(Notification.permission);
  }, []);
  if (permission !== "default") return null;
  return (
    <button
      type="button"
      onClick={() => {
        requestNotificationPermission();
        setTimeout(() => setPermission(Notification.permission), 1500);
      }}
      className="block w-full border-b border-line px-3 py-2 text-left text-[11px] text-accent hover:bg-panel2"
    >
      Turn on desktop notifications so alerts reach you in other tabs
    </button>
  );
}

function AlertRow(props: {
  alert: PriceAlert;
  onPick(): void;
  onEdit?: () => void;
  onToggleRepeat?: () => void;
  onRearm?: () => void;
  onRemove?: () => void;
}) {
  const a = props.alert;
  const expiry = a.armed ? expiryLabel(a.expires_at) : null;
  const status = a.expired
    ? "expired"
    : a.triggered_at
      ? `fired ${formatWhen(a.triggered_at)}${a.triggered_price != null ? ` at ${formatPrice(a.triggered_price)}` : ""}`
      : null;
  return (
    <div className={clsx("group flex items-start gap-2 border-b border-line/60 px-3 py-2 text-[12px]", !a.armed && "opacity-60")}>
      {a.armed ? (
        <Bell className="mt-0.5 h-3.5 w-3.5 shrink-0 text-yellow-300" />
      ) : (
        <BellRing className="mt-0.5 h-3.5 w-3.5 shrink-0 text-mute" />
      )}
      <div className="min-w-0 flex-1">
        <button type="button" onClick={props.onPick} className="font-medium text-ink hover:text-accent">
          {displaySymbol(a.symbol)}
        </button>{" "}
        <span className="text-mute">{describeAlert(a)}</span>
        <div className="truncate text-[11px] text-mute">
          {[a.label, status, (a.fire_count ?? 0) > 0 ? `${a.fire_count}× fired` : null, expiry]
            .filter(Boolean)
            .join(" · ")}
        </div>
        {a.note && <div className="mt-0.5 line-clamp-2 text-[11px] italic text-mute">{a.note}</div>}
      </div>
      {props.onToggleRepeat && (
        <button
          type="button"
          onClick={props.onToggleRepeat}
          className={clsx("btn-ghost h-6 w-6 p-0", a.repeat ? "text-accent" : "opacity-50")}
          title={a.repeat ? "Repeats: stays armed after firing (at most every 5 min). Click to fire once." : "Fires once. Click to keep it armed after firing."}
        >
          <Repeat className="h-3.5 w-3.5" />
        </button>
      )}
      {props.onEdit && (
        <button type="button" onClick={props.onEdit} className="btn-ghost h-6 w-6 p-0" title="Edit">
          <Pencil className="h-3.5 w-3.5" />
        </button>
      )}
      {!a.armed && props.onRearm && (
        <button type="button" onClick={props.onRearm} className="btn-ghost h-6 w-6 p-0" title="Re-arm">
          <RotateCcw className="h-3.5 w-3.5" />
        </button>
      )}
      {props.onRemove && (
        <button type="button" onClick={props.onRemove} className="btn-ghost h-6 w-6 p-0 hover:text-down" title="Delete alert">
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}

/** Never / 1 h / 4 h / 1 day / 1 week / a date and time. `value` is ms or null (never). */
function ExpiryPicker({ value, onChange }: { value: number | null; onChange(v: number | null): void }) {
  const [custom, setCustom] = useState(value != null);
  const choice = value == null && !custom ? "never" : "custom";
  return (
    <div className="flex min-w-0 items-center gap-1">
      <select
        className={clsx(INPUT, "w-24")}
        value={choice}
        onChange={(e) => {
          const opt = EXPIRY_OPTIONS.find((o) => o.value === e.target.value);
          if (!opt || opt.value === "never") {
            setCustom(false);
            onChange(null);
          } else if (opt.value === "custom") {
            setCustom(true);
            onChange(value ?? Date.now() + 24 * 3_600_000);
          } else {
            setCustom(true);
            onChange(Date.now() + (opt.ms ?? 0));
          }
        }}
      >
        {EXPIRY_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.value === "custom" && value != null ? "Until…" : o.label}
          </option>
        ))}
      </select>
      {choice === "custom" && (
        <input
          type="datetime-local"
          className={clsx(INPUT, "flex-1")}
          value={value != null ? toLocalInput(value) : ""}
          onChange={(e) => {
            const ms = new Date(e.target.value).getTime();
            if (Number.isFinite(ms)) onChange(ms);
          }}
        />
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex min-w-0 flex-col gap-0.5">
      <span className={LABEL}>{label}</span>
      {children}
    </label>
  );
}

function NewAlertForm(props: {
  symbol: string;
  price: number | null;
  onAdd(specs: AlertSpec[], symbol: string): Promise<boolean>;
  onDone(): void;
}) {
  const live = props.price;
  const [kind, setKind] = useState<"cross" | "zone">("cross");
  const [price, setPrice] = useState(priceInput(live));
  const [low, setLow] = useState(priceInput(live != null ? live * 0.995 : null));
  const [high, setHigh] = useState(priceInput(live != null ? live * 1.005 : null));
  const [label, setLabel] = useState("");
  const [note, setNote] = useState("");
  const [repeat, setRepeat] = useState(false);
  const [expires, setExpires] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async () => {
    let spec: AlertSpec;
    if (kind === "cross") {
      const v = parsePrice(price);
      if (Number.isNaN(v)) return setProblem("Enter a price above zero.");
      spec = { kind, price: v, price_low: null, price_high: null, label: label.trim() || `Price ${formatPrice(v)}` };
    } else {
      const lo = parsePrice(low);
      const hi = parsePrice(high);
      if (Number.isNaN(lo) || Number.isNaN(hi)) return setProblem("Enter both edges of the zone.");
      const [a, b] = lo <= hi ? [lo, hi] : [hi, lo];
      spec = { kind, price: null, price_low: a, price_high: b, label: label.trim() || `Zone ${formatPrice(a)}–${formatPrice(b)}` };
    }
    if (expires != null && expires <= Date.now()) return setProblem("The expiry time has already passed.");
    setProblem(null);
    setBusy(true);
    const ok = await props.onAdd([{ ...spec, note: note.trim(), repeat, expires_at: expires }], props.symbol);
    setBusy(false);
    if (ok) props.onDone();
  };

  return (
    <div className="space-y-2 border-b border-line bg-panel2/40 px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span className="font-medium text-ink">{displaySymbol(props.symbol)}</span>
        <Segmented
          value={kind}
          options={[
            { value: "cross", label: "Price level" },
            { value: "zone", label: "Zone" },
          ]}
          onChange={(v) => setKind(v as "cross" | "zone")}
        />
        {live != null && <span className="ml-auto text-[11px] text-mute">now {formatPrice(live)}</span>}
      </div>
      {kind === "cross" ? (
        <Field label="Alert when price crosses">
          <input className={INPUT} inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
        </Field>
      ) : (
        <div className="grid grid-cols-2 gap-2">
          <Field label="Zone low">
            <input className={INPUT} inputMode="decimal" value={low} onChange={(e) => setLow(e.target.value)} />
          </Field>
          <Field label="Zone high">
            <input className={INPUT} inputMode="decimal" value={high} onChange={(e) => setHigh(e.target.value)} />
          </Field>
        </div>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Field label="Label">
          <input className={INPUT} value={label} maxLength={200} placeholder="optional" onChange={(e) => setLabel(e.target.value)} />
        </Field>
        <Field label="Expires">
          <ExpiryPicker value={expires} onChange={setExpires} />
        </Field>
      </div>
      <Field label="Note">
        <input className={INPUT} value={note} maxLength={500} placeholder="e.g. take profit, move stop" onChange={(e) => setNote(e.target.value)} />
      </Field>
      <Checkbox checked={repeat} onChange={setRepeat} label="Keep it armed after it fires (at most once every 5 minutes)" />
      {problem && <div className="text-[11px] text-down">{problem}</div>}
      <div className="flex justify-end gap-1.5">
        <button type="button" onClick={props.onDone} className="btn-ghost h-7 px-2 text-[12px]">
          Cancel
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={busy}
          className="h-7 rounded bg-accent px-3 text-[12px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Create alert"}
        </button>
      </div>
    </div>
  );
}

function AlertEditor(props: { alert: PriceAlert; onSave(patch: AlertPatch): Promise<boolean>; onDone(): void }) {
  const a = props.alert;
  const [price, setPrice] = useState(priceInput(a.price));
  const [low, setLow] = useState(priceInput(a.price_low));
  const [high, setHigh] = useState(priceInput(a.price_high));
  const [label, setLabel] = useState(a.label);
  const [note, setNote] = useState(a.note ?? "");
  const [repeat, setRepeat] = useState(Boolean(a.repeat));
  const [expires, setExpires] = useState<number | null>(a.expires_at ?? null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const save = async () => {
    const patch: AlertPatch = {};
    if (a.kind === "cross") {
      const v = parsePrice(price);
      if (Number.isNaN(v)) return setProblem("Enter a price above zero.");
      if (v !== a.price) patch.price = v;
    } else {
      const lo = parsePrice(low);
      const hi = parsePrice(high);
      if (Number.isNaN(lo) || Number.isNaN(hi)) return setProblem("Enter both edges of the zone.");
      if (lo !== a.price_low || hi !== a.price_high) Object.assign(patch, { price_low: lo, price_high: hi });
    }
    if (label.trim() !== a.label) patch.label = label.trim();
    if (note.trim() !== (a.note ?? "")) patch.note = note.trim();
    if (repeat !== Boolean(a.repeat)) patch.repeat = repeat;
    if (expires !== (a.expires_at ?? null)) {
      if (expires != null && expires <= Date.now()) return setProblem("The expiry time has already passed.");
      patch.expires_at = expires;
    }
    setProblem(null);
    if (!Object.keys(patch).length) return props.onDone();
    setBusy(true);
    const ok = await props.onSave(patch);
    setBusy(false);
    if (ok) props.onDone();
  };

  return (
    <div className="space-y-2 border-b border-line bg-panel2/40 px-3 py-2.5">
      <div className="text-[12px]">
        <span className="font-medium text-ink">{displaySymbol(a.symbol)}</span>{" "}
        <span className="text-mute">edit {a.kind === "zone" ? "zone" : "level"}</span>
      </div>
      {a.kind === "cross" ? (
        <Field label="Price">
          <input className={INPUT} inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
        </Field>
      ) : (
        <div className="grid grid-cols-2 gap-2">
          <Field label="Zone low">
            <input className={INPUT} inputMode="decimal" value={low} onChange={(e) => setLow(e.target.value)} />
          </Field>
          <Field label="Zone high">
            <input className={INPUT} inputMode="decimal" value={high} onChange={(e) => setHigh(e.target.value)} />
          </Field>
        </div>
      )}
      <div className="grid grid-cols-2 gap-2">
        <Field label="Label">
          <input className={INPUT} value={label} maxLength={200} onChange={(e) => setLabel(e.target.value)} />
        </Field>
        <Field label="Expires">
          <ExpiryPicker value={expires} onChange={setExpires} />
        </Field>
      </div>
      <Field label="Note">
        <input className={INPUT} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <Checkbox checked={repeat} onChange={setRepeat} label="Keep it armed after it fires (at most once every 5 minutes)" />
      {a.expired && <div className="text-[11px] text-mute">This alert expired. Pick a new expiry (or Never) to arm it again.</div>}
      {problem && <div className="text-[11px] text-down">{problem}</div>}
      <div className="flex justify-end gap-1.5">
        <button type="button" onClick={props.onDone} className="btn-ghost h-7 px-2 text-[12px]">
          Cancel
        </button>
        <button
          type="button"
          onClick={save}
          disabled={busy}
          className="h-7 rounded bg-accent px-3 text-[12px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- small parts --

function Segmented({ value, options, onChange }: { value: string; options: { value: string; label: string }[]; onChange(v: string): void }) {
  return (
    <div className="inline-flex rounded border border-line bg-panel2 p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={clsx("rounded px-2 py-0.5 text-[11px]", value === o.value ? "bg-panel text-ink" : "text-mute hover:text-ink")}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Checkbox({ checked, onChange, label }: { checked: boolean; onChange(v: boolean): void; label: ReactNode }) {
  return (
    <label className="flex cursor-pointer items-start gap-2 text-[11px] text-ink">
      <input type="checkbox" className="mt-0.5 accent-blue-500" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

function IntervalSelect({ value, onChange }: { value: Interval; onChange(v: Interval): void }) {
  return (
    <select className={INPUT} value={value} onChange={(e) => onChange(e.target.value as Interval)}>
      {TIMEFRAMES.map((t) => (
        <option key={t.value} value={t.value}>
          {t.label}
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------- signals tab --

type CoinScope = "chart" | "watchlist" | "pick";

function SignalsTab({ p, api }: { p: AlertsPanelProps; api: AlertsApi }) {
  const symbol = p.symbol ?? "BTCUSDT";
  const watchlist = useMemo(() => p.watchlist ?? [], [p.watchlist]);
  const [scope, setScope] = useState<CoinScope>("chart");
  const [picked, setPicked] = useState<string[]>([]);
  const [interval, setInterval_] = useState<Interval>(p.interval ?? "4h");
  const [signal, setSignal] = usePersistentState<SignalId>("ac:signal-alert-kind", "kimi_buy");
  const [repeat, setRepeat] = useState(true);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [preview, setPreview] = useState<SignalPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const previewCtrl = useRef<AbortController | null>(null);
  // Refs, so an unstable callback from the parent never re-runs the cleanup effect below (and wipes the preview).
  const overlaysRef = useRef(p.onChartOverlays);
  overlaysRef.current = p.onChartOverlays;
  const markedSymbol = useRef<string | null>(null);

  const coins = useMemo(() => {
    if (scope === "chart") return [symbol];
    if (scope === "watchlist") return watchlist.slice(0, 40);
    return picked.slice(0, 40);
  }, [scope, symbol, watchlist, picked]);
  const pickable = useMemo(() => Array.from(new Set([symbol, ...watchlist])), [symbol, watchlist]);
  const hint = SIGNAL_OPTIONS.find((s) => s.id === signal)?.hint;

  const clearPreview = useCallback(() => {
    previewCtrl.current?.abort();
    setPreview(null);
    setPreviewError(null);
    if (markedSymbol.current) overlaysRef.current?.(PREVIEW_KEY, markedSymbol.current, []);
    markedSymbol.current = null;
  }, []);

  // Drop the preview (and its chart markers) when the question changes or the tab closes.
  useEffect(() => clearPreview, [clearPreview, signal, interval, scope]);

  const runPreview = async () => {
    const coin = coins[0] ?? symbol;
    clearPreview();
    const ctrl = new AbortController();
    previewCtrl.current = ctrl;
    setPreviewing(true);
    setPreviewError(null);
    try {
      const res = await api.previewSignal(coin, interval, signal, 300, ctrl.signal);
      setPreview(res);
      const onChartOverlays = overlaysRef.current;
      if (onChartOverlays && coin === symbol && interval === p.interval) {
        const long = !["kimi_sell", "rsi_bear_div", "sweep_high", "new_supply", "bos_bear", "rsi_overbought"].includes(signal);
        const markers: Overlay[] = res.hits.slice(0, 50).map((h, i) => ({
          type: "marker", id: `signal-preview-${i}`, kind: "signal_preview", time: h.time, price: h.price,
          position: long ? "below" : "above", shape: long ? "arrowUp" : "arrowDown", label: signalName(signal),
          color: "#60a5fa",
        }));
        onChartOverlays(PREVIEW_KEY, coin, markers);
        markedSymbol.current = coin;
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") setPreviewError((err as Error).message);
    } finally {
      if (previewCtrl.current === ctrl) setPreviewing(false);
    }
  };

  const create = async () => {
    if (!coins.length) return;
    setBusy(true);
    setDone(null);
    const made = await api.addSignal({ symbols: coins, interval, signal, repeat, note: note.trim() });
    setBusy(false);
    if (made) {
      setDone(`Watching ${made.length === 1 ? displaySymbol(made[0].symbol) : `${made.length} coins`} for ${signalName(signal)}.`);
      setNote("");
    }
  };

  const list = useMemo(() => api.signalAlerts.filter((a) => a.signal !== "zone_trigger"), [api.signalAlerts]);
  return (
    <div>
      <div className="space-y-2 border-b border-line px-3 py-2.5">
        <div className={LABEL}>New signal alert</div>
        <Field label="Signal">
          <select className={INPUT} value={signal} onChange={(e) => setSignal(e.target.value as SignalId)}>
            {SIGNAL_OPTIONS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
        {hint && <div className="text-[11px] text-mute">{hint}, judged when each candle closes.</div>}
        <div className="grid grid-cols-[1fr_auto] items-end gap-2">
          <Field label="Coins">
            <Segmented
              value={scope}
              options={[
                { value: "chart", label: displaySymbol(symbol) },
                { value: "watchlist", label: `Watchlist (${watchlist.length})` },
                { value: "pick", label: "Pick…" },
              ]}
              onChange={(v) => setScope(v as CoinScope)}
            />
          </Field>
          <Field label="Timeframe">
            <IntervalSelect value={interval} onChange={setInterval_} />
          </Field>
        </div>
        {scope === "pick" && (
          <div className="flex max-h-28 flex-wrap gap-1 overflow-y-auto">
            {pickable.map((s) => {
              const on = picked.includes(s);
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => setPicked((cur) => (on ? cur.filter((x) => x !== s) : [...cur, s]))}
                  className={clsx(
                    "rounded border px-1.5 py-0.5 text-[11px]",
                    on ? "border-accent bg-accent/15 text-ink" : "border-line text-mute hover:text-ink",
                  )}
                >
                  {displaySymbol(s)}
                </button>
              );
            })}
          </div>
        )}
        {scope === "watchlist" && !watchlist.length && <div className="text-[11px] text-mute">Your watchlist is empty.</div>}
        <Field label="Note">
          <input className={INPUT} value={note} maxLength={500} placeholder="optional, added to the message" onChange={(e) => setNote(e.target.value)} />
        </Field>
        <Checkbox checked={repeat} onChange={setRepeat} label="Alert me every time (off: only the first time, then switch off)" />
        <div className="flex items-center gap-1.5">
          <button type="button" onClick={runPreview} disabled={previewing} className="btn-ghost h-7 px-2 text-[12px] disabled:opacity-50">
            <Eye className="h-3.5 w-3.5" />
            {previewing ? "Checking…" : "Preview"}
          </button>
          <div className="flex-1" />
          <button
            type="button"
            onClick={create}
            disabled={busy || !coins.length}
            className="h-7 rounded bg-accent px-3 text-[12px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
          >
            {busy ? "Saving…" : coins.length > 1 ? `Create ${coins.length} alerts` : "Create alert"}
          </button>
        </div>
        {done && <div className="text-[11px] text-up">{done}</div>}
        {previewError && <div className="text-[11px] text-down">{previewError}</div>}
        {preview && <PreviewResult preview={preview} onClose={clearPreview} />}
      </div>

      {list.length === 0 ? (
        <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">
          No signal alerts yet. Pick a signal above, e.g. Kimi B+ on BTC 4h, an RSI divergence on your whole watchlist,
          or a sweep of a low. They are checked on the server whenever a candle closes.
        </p>
      ) : (
        list.map((a) => (
          <SignalRow
            key={a.id}
            alert={a}
            onPick={() => p.onPickSymbol?.(a.symbol, a.interval)}
            onToggle={() => void api.updateSignal(a.id, { armed: !a.armed })}
            onToggleRepeat={() => void api.updateSignal(a.id, { repeat: !a.repeat })}
            onRemove={() => void api.removeSignal(a.id)}
          />
        ))
      )}
    </div>
  );
}

function PreviewResult({ preview, onClose }: { preview: SignalPreview; onClose(): void }) {
  const tf = TIMEFRAMES.find((t) => t.value === preview.interval)?.label ?? preview.interval;
  return (
    <div className="rounded border border-line bg-panel2/60 p-2 text-[11px]">
      <div className="mb-1 flex items-start gap-2">
        <span className="flex-1 text-ink">
          {preview.hits.length
            ? `${preview.name} fired ${preview.hits.length} time${preview.hits.length === 1 ? "" : "s"} in the last ${preview.bars} ${tf} candles on ${displaySymbol(preview.symbol)}`
            : `${preview.name} did not fire in the last ${preview.bars} ${tf} candles on ${displaySymbol(preview.symbol)}`}
        </span>
        <button type="button" onClick={onClose} className="text-mute hover:text-ink" aria-label="Close preview">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <ul className="max-h-40 space-y-0.5 overflow-y-auto">
        {preview.hits.slice(0, 12).map((h) => (
          <li key={`${h.time}-${h.text}`} className="flex gap-2">
            <span className="shrink-0 text-mute">{formatWhen(h.time * 1000)}</span>
            <span className="text-ink">{h.text}</span>
          </li>
        ))}
      </ul>
      {preview.note && <div className="mt-1 text-mute">{preview.note}</div>}
    </div>
  );
}

function SignalRow(props: { alert: SignalAlert; onPick(): void; onToggle(): void; onToggleRepeat(): void; onRemove(): void }) {
  const a = props.alert;
  const tf = TIMEFRAMES.find((t) => t.value === a.interval)?.label ?? a.interval;
  return (
    <div className={clsx("flex items-start gap-2 border-b border-line/60 px-3 py-2 text-[12px]", !a.armed && "opacity-60")}>
      <button
        type="button"
        onClick={props.onToggle}
        className={clsx("btn-ghost mt-[-2px] h-6 w-6 shrink-0 p-0", a.armed ? "text-up" : "text-mute")}
        title={a.armed ? "On: click to pause" : "Off: click to switch on"}
      >
        <Power className="h-3.5 w-3.5" />
      </button>
      <div className="min-w-0 flex-1">
        <button type="button" onClick={props.onPick} className="font-medium text-ink hover:text-accent">
          {displaySymbol(a.symbol)} {tf}
        </button>{" "}
        <span className="text-mute">{a.trigger ? describeTrigger(a) : signalName(a.signal)}</span>
        <div className="truncate text-[11px] text-mute">
          {a.last_fired_at
            ? `Last fired ${timeAgo(a.last_fired_at)} · ${a.fire_count}× in total`
            : "Not fired yet"}
          {!a.repeat && " · once"}
        </div>
        {a.last_text && <div className="line-clamp-2 text-[11px] text-mute">{a.last_text}</div>}
        {a.last_stop != null && <div className="text-[11px] text-mute">Suggested stop {formatPrice(a.last_stop)}</div>}
        {a.note && <div className="line-clamp-2 text-[11px] italic text-mute">{a.note}</div>}
      </div>
      <button
        type="button"
        onClick={props.onToggleRepeat}
        className={clsx("btn-ghost h-6 w-6 p-0", a.repeat ? "text-accent" : "opacity-50")}
        title={a.repeat ? "Alerts every time. Click to alert only once." : "Alerts once, then switches off. Click to alert every time."}
      >
        <Repeat className="h-3.5 w-3.5" />
      </button>
      <button type="button" onClick={props.onRemove} className="btn-ghost h-6 w-6 p-0 hover:text-down" title="Delete">
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

// --------------------------------------------------------------- triggers tab --

type ZoneSource = "detected" | "chart" | "prices";
type Side = "auto" | "long" | "short";

const ZONE_KIND_OPTIONS: { value: TriggerZoneKind; label: string }[] = [
  { value: "demand", label: "Demand" },
  { value: "supply", label: "Supply" },
  { value: "support", label: "Support" },
  { value: "resistance", label: "Resistance" },
  { value: "any", label: "Nearest zone" },
];

const tfLabel = (tf: string) => TIMEFRAMES.find((t) => t.value === tf)?.label ?? tf;

/** Lower-timeframe confirmation inside a higher-timeframe zone: the form, a preview, and the armed triggers. */
function TriggersTab({ p, api }: { p: AlertsPanelProps; api: AlertsApi }) {
  const symbol = p.symbol ?? "BTCUSDT";
  const zones = useMemo(() => p.zones ?? [], [p.zones]);
  const [interval, setInterval_] = usePersistentState<TriggerInterval>("ac:trigger-interval", "5m");
  const [confirm, setConfirm] = usePersistentState<Confirmation>("ac:trigger-confirm", "any");
  const [source, setSource] = useState<ZoneSource>(zones.length ? "chart" : "detected");
  const [zoneTf, setZoneTf] = useState<Interval>("4h");
  const [kind, setKind] = useState<TriggerZoneKind>("demand");
  const [fresh, setFresh] = useState(true);
  const [zoneKey, setZoneKey] = useState<string>("");
  const [low, setLow] = useState("");
  const [high, setHigh] = useState("");
  const [side, setSide] = useState<Side>("auto");
  const [cooldown, setCooldown] = useState("60");
  const [repeat, setRepeat] = useState(true);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [preview, setPreview] = useState<TriggerPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const previewCtrl = useRef<AbortController | null>(null);
  const overlaysRef = useRef(p.onChartOverlays);
  overlaysRef.current = p.onChartOverlays;
  const markedSymbol = useRef<string | null>(null);

  const chartZone = zones.find((z) => z.key === zoneKey) ?? zones[0] ?? null;
  const hint = CONFIRM_OPTIONS.find((c) => c.id === confirm)?.hint;

  const clearPreview = useCallback(() => {
    previewCtrl.current?.abort();
    setPreview(null);
    if (markedSymbol.current) overlaysRef.current?.(TRIGGER_PREVIEW_KEY, markedSymbol.current, []);
    markedSymbol.current = null;
  }, []);

  useEffect(() => clearPreview, [clearPreview, interval, confirm, source, zoneTf, kind, zoneKey, symbol]);

  /** The form → a spec, or a message saying what is missing. */
  const buildSpec = (): ZoneTriggerSpec | string => {
    const minutes = Number(cooldown);
    if (!Number.isFinite(minutes) || minutes < 0 || minutes > 1440) return "Cooldown is 0 to 1440 minutes.";
    const direction = side === "auto" ? null : side;
    let zone: ZoneTriggerSpec["zone"];
    if (source === "detected") {
      const ltf = TIMEFRAMES.findIndex((t) => t.value === interval);
      const htf = TIMEFRAMES.findIndex((t) => t.value === zoneTf);
      if (htf <= ltf) return "Pick a zone timeframe above the trigger timeframe.";
      zone = { source: "detected", timeframe: zoneTf, kind, fresh_only: fresh };
    } else if (source === "chart") {
      if (!chartZone) return "No zone on the chart. Ask the agent for zones or draw a rectangle.";
      zone = {
        source: "fixed", price_low: chartZone.low, price_high: chartZone.high,
        direction: direction ?? chartZone.direction, label: chartZone.label.slice(0, 120),
      };
    } else {
      const lo = parsePrice(low);
      const hi = parsePrice(high);
      if (Number.isNaN(lo) || Number.isNaN(hi)) return "Enter both edges of the zone.";
      const [a, b] = lo <= hi ? [lo, hi] : [hi, lo];
      zone = { source: "fixed", price_low: a, price_high: b, direction, label: `Zone ${formatPrice(a)}–${formatPrice(b)}` };
    }
    return { symbol, interval, zone, confirm, cooldown_min: Math.round(minutes), repeat, note: note.trim() };
  };

  const runPreview = async () => {
    const spec = buildSpec();
    if (typeof spec === "string") return setProblem(spec);
    setProblem(null);
    clearPreview();
    const ctrl = new AbortController();
    previewCtrl.current = ctrl;
    setPreviewing(true);
    try {
      const res = await api.previewTrigger(spec, 300, ctrl.signal);
      setPreview(res);
      const onChartOverlays = overlaysRef.current;
      if (onChartOverlays && p.interval === interval) {
        const long = res.zone?.direction !== "short";
        const marks: Overlay[] = res.hits.slice(0, 50).map((h, i) => ({
          type: "marker", id: `trigger-preview-${i}`, kind: "signal_preview", time: h.time, price: h.price,
          position: long ? "below" : "above", shape: long ? "arrowUp" : "arrowDown", label: "Trigger", color: "#60a5fa",
        }));
        if (res.zone) {
          marks.push({
            type: "box", id: "trigger-preview-zone", kind: "trigger_zone", label: `${res.zone.label} (trigger zone)`,
            color: "rgba(96,165,250,0.08)", border_color: "#60a5fa", price_low: res.zone.low, price_high: res.zone.high,
          });
        }
        onChartOverlays(TRIGGER_PREVIEW_KEY, symbol, marks);
        markedSymbol.current = symbol;
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") setProblem((err as Error).message);
    } finally {
      if (previewCtrl.current === ctrl) setPreviewing(false);
    }
  };

  const create = async () => {
    const spec = buildSpec();
    if (typeof spec === "string") return setProblem(spec);
    setProblem(null);
    setBusy(true);
    setDone(null);
    const made = await api.addTrigger(spec);
    setBusy(false);
    if (made) {
      setDone(`Watching ${displaySymbol(made.symbol)} ${tfLabel(made.interval)}: ${describeTrigger(made)}.`);
      setNote("");
    }
  };

  const list = useMemo(() => api.signalAlerts.filter((a) => a.signal === "zone_trigger"), [api.signalAlerts]);
  return (
    <div>
      <div className="space-y-2 border-b border-line px-3 py-2.5">
        <div className={LABEL}>New trigger alert on {displaySymbol(symbol)}</div>
        <div className="grid grid-cols-[auto_1fr] items-end gap-2">
          <Field label="Trigger timeframe">
            <Segmented
              value={interval}
              options={TRIGGER_INTERVALS.map((t) => ({ value: t, label: tfLabel(t) }))}
              onChange={(v) => setInterval_(v as TriggerInterval)}
            />
          </Field>
          <Field label="Confirmation">
            <select className={INPUT} value={confirm} onChange={(e) => setConfirm(e.target.value as Confirmation)}>
              {CONFIRM_OPTIONS.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>
        </div>
        {hint && <div className="text-[11px] text-mute">{hint}, inside (or just after touching) the zone.</div>}
        <Field label="Zone">
          <Segmented
            value={source}
            options={[
              { value: "chart", label: `On the chart (${zones.length})` },
              { value: "detected", label: "Detected" },
              { value: "prices", label: "Prices" },
            ]}
            onChange={(v) => setSource(v as ZoneSource)}
          />
        </Field>
        {source === "detected" && (
          <div className="space-y-1.5">
            <div className="grid grid-cols-2 gap-2">
              <Field label="Nearest">
                <select className={INPUT} value={kind} onChange={(e) => setKind(e.target.value as TriggerZoneKind)}>
                  {ZONE_KIND_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="On timeframe">
                <IntervalSelect value={zoneTf} onChange={setZoneTf} />
              </Field>
            </div>
            {(kind === "demand" || kind === "supply") && (
              <Checkbox checked={fresh} onChange={setFresh} label="Fresh zones only (tested at most once)" />
            )}
            <div className="text-[11px] text-mute">Looked up again each time that timeframe closes, so the zone follows the market.</div>
          </div>
        )}
        {source === "chart" &&
          (zones.length ? (
            <Field label="Zone on the chart">
              <select className={INPUT} value={chartZone?.key ?? ""} onChange={(e) => setZoneKey(e.target.value)}>
                {zones.map((z) => (
                  <option key={z.key} value={z.key}>
                    {z.label} · {formatPrice(z.low)}–{formatPrice(z.high)}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <div className="text-[11px] text-mute">
              No zones on the chart. Ask the agent for zones, draw a rectangle, or use Detected / Prices.
            </div>
          ))}
        {source === "prices" && (
          <div className="grid grid-cols-2 gap-2">
            <Field label="Zone low">
              <input className={INPUT} inputMode="decimal" value={low} onChange={(e) => setLow(e.target.value)} />
            </Field>
            <Field label="Zone high">
              <input className={INPUT} inputMode="decimal" value={high} onChange={(e) => setHigh(e.target.value)} />
            </Field>
          </div>
        )}
        <div className="grid grid-cols-2 gap-2">
          {source !== "detected" && (
            <Field label="Direction">
              <select className={INPUT} value={side} onChange={(e) => setSide(e.target.value as Side)}>
                <option value="auto">From the zone / price</option>
                <option value="long">Long (bullish trigger)</option>
                <option value="short">Short (bearish trigger)</option>
              </select>
            </Field>
          )}
          <Field label="Cooldown (minutes)">
            <input className={INPUT} inputMode="numeric" value={cooldown} onChange={(e) => setCooldown(e.target.value)} />
          </Field>
        </div>
        <Field label="Note">
          <input className={INPUT} value={note} maxLength={500} placeholder="optional, added to the message" onChange={(e) => setNote(e.target.value)} />
        </Field>
        <Checkbox checked={repeat} onChange={setRepeat} label="Stay armed for the next touch (off: alert once, then switch off)" />
        {problem && <div className="text-[11px] text-down">{problem}</div>}
        <div className="flex items-center gap-1.5">
          <button type="button" onClick={runPreview} disabled={previewing} className="btn-ghost h-7 px-2 text-[12px] disabled:opacity-50">
            <Eye className="h-3.5 w-3.5" />
            {previewing ? "Checking…" : "Preview"}
          </button>
          <div className="flex-1" />
          <button
            type="button"
            onClick={create}
            disabled={busy}
            className="h-7 rounded bg-accent px-3 text-[12px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
          >
            {busy ? "Saving…" : "Create trigger"}
          </button>
        </div>
        {done && <div className="text-[11px] text-up">{done}</div>}
        {preview && <TriggerPreviewResult preview={preview} onClose={clearPreview} />}
      </div>

      {list.length === 0 ? (
        <p className="px-3 py-4 text-[12px] leading-relaxed text-mute">
          No trigger alerts yet. A trigger fires once per touch of a zone, when a lower-timeframe candle confirms the
          reaction (CHoCH, sweep or engulfing close), with a suggested stop under the swing. Create one above, press{" "}
          <span className="text-ink">Alert on 5m confirmation</span> on a trade plan, or ask the agent (&quot;alert me
          when 1m shows a CHoCH inside the 4h demand&quot;).
        </p>
      ) : (
        list.map((a) => (
          <SignalRow
            key={a.id}
            alert={a}
            onPick={() => p.onPickSymbol?.(a.symbol, a.interval)}
            onToggle={() => void api.updateSignal(a.id, { armed: !a.armed })}
            onToggleRepeat={() => void api.updateSignal(a.id, { repeat: !a.repeat })}
            onRemove={() => void api.removeSignal(a.id)}
          />
        ))
      )}
    </div>
  );
}

function TriggerPreviewResult({ preview, onClose }: { preview: TriggerPreview; onClose(): void }) {
  const tf = tfLabel(preview.interval);
  return (
    <div className="rounded border border-line bg-panel2/60 p-2 text-[11px]">
      <div className="mb-1 flex items-start gap-2">
        <span className="flex-1 text-ink">
          {preview.zone
            ? `${preview.zone.label} ${formatPrice(preview.zone.low)}–${formatPrice(preview.zone.high)}: ` +
              (preview.hits.length
                ? `fired ${preview.hits.length} time${preview.hits.length === 1 ? "" : "s"} in the last ${preview.bars} ${tf} candles`
                : `no trigger in the last ${preview.bars} ${tf} candles`)
            : "No zone found right now."}
        </span>
        <button type="button" onClick={onClose} className="text-mute hover:text-ink" aria-label="Close preview">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <ul className="max-h-40 space-y-0.5 overflow-y-auto">
        {preview.hits.slice(0, 12).map((h) => (
          <li key={`${h.time}-${h.text}`} className="flex gap-2">
            <span className="shrink-0 text-mute">{formatWhen(h.time * 1000)}</span>
            <span className="text-ink">{h.text}</span>
          </li>
        ))}
      </ul>
      {preview.note && <div className="mt-1 text-mute">{preview.note}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- history tab --

const KIND_ICON = { price: BellRing, signal: Activity, brief: Newspaper, trade: Crosshair } as const;
const KIND_COLOR = { price: "text-yellow-300", signal: "text-accent", brief: "text-mute", trade: "text-up" } as const;

function HistoryTab({ p, api }: { p: AlertsPanelProps; api: AlertsApi }) {
  const [confirm, setConfirm] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | AlertHistoryItem["kind"]>("all");
  const items = filter === "all" ? api.history : api.history.filter((h) => h.kind === filter);

  return (
    <div>
      <div className="flex items-center gap-1.5 border-b border-line px-3 py-1.5">
        <Segmented
          value={filter}
          options={[
            { value: "all", label: "All" },
            { value: "price", label: "Price" },
            { value: "signal", label: "Signals" },
            { value: "brief", label: "Briefs" },
          ]}
          onChange={(v) => setFilter(v as typeof filter)}
        />
        <div className="flex-1" />
        <button type="button" onClick={() => void api.refreshHistory()} className="btn-ghost h-6 w-6 p-0" title="Refresh">
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
        {api.history.length > 0 &&
          (confirm ? (
            <button
              type="button"
              onClick={() => {
                setConfirm(false);
                void api.clearHistory();
              }}
              onBlur={() => setConfirm(false)}
              className="btn-ghost h-6 px-1.5 text-[11px] text-down"
            >
              Confirm clear
            </button>
          ) : (
            <button type="button" onClick={() => setConfirm(true)} className="btn-ghost h-6 px-1.5 text-[11px]">
              Clear
            </button>
          ))}
      </div>
      {items.length === 0 && (
        <p className="px-3 py-4 text-[12px] text-mute">Nothing has fired yet. Price alerts, signal alerts and briefs show up here.</p>
      )}
      {items.map((h) => {
        const Icon = KIND_ICON[h.kind] ?? Bell;
        const long = h.text.length > 160 || h.text.includes("\n");
        const expanded = open === h.id;
        return (
          <div key={h.id} className="flex items-start gap-2 border-b border-line/60 px-3 py-2 text-[12px]">
            <Icon className={clsx("mt-0.5 h-3.5 w-3.5 shrink-0", KIND_COLOR[h.kind])} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-1.5">
                {h.symbol && (
                  <button type="button" onClick={() => p.onPickSymbol?.(h.symbol)} className="font-medium text-ink hover:text-accent">
                    {displaySymbol(h.symbol)}
                  </button>
                )}
                <span className="truncate text-mute">{h.title}</span>
                <span className="ml-auto shrink-0 text-[10px] text-mute" title={new Date(h.time).toLocaleString()}>
                  {formatWhen(h.time)}
                </span>
              </div>
              <div
                className={clsx(
                  "whitespace-pre-wrap break-words text-[11px] text-ink/90",
                  long && !expanded && "line-clamp-3",
                  h.kind === "brief" && expanded && "font-mono text-[10px]",
                )}
              >
                {h.text}
              </div>
              {long && (
                <button type="button" onClick={() => setOpen(expanded ? null : h.id)} className="text-[11px] text-accent hover:underline">
                  {expanded ? "Show less" : "Show all"}
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------------ brief tab --

const DEFAULT_BRIEF: BriefSettings = {
  enabled: false,
  times: ["08:00"],
  timezone: "UTC",
  symbols: [],
  interval: "4h",
  sections: { zones: true, kimi: true, derivatives: true, events: true, levels: true },
};

const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

function BriefTab({ p, api }: { p: AlertsPanelProps; api: AlertsApi }) {
  const watchlist = useMemo(() => (p.watchlist ?? []).slice(0, 40), [p.watchlist]);
  const [status, setStatus] = useState<BriefStatus | null>(null);
  const [form, setForm] = useState<BriefSettings>(DEFAULT_BRIEF);
  const [useWatchlist, setUseWatchlist] = usePersistentState("ac:brief-use-watchlist", true);
  const [newTime, setNewTime] = useState("13:30");
  const [busy, setBusy] = useState<"load" | "save" | "preview" | "send" | null>("load");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [preview, setPreview] = useState<BriefPreview | null>(null);
  const zones = useMemo(() => timeZones(), []);
  const { brief } = api;

  useEffect(() => {
    let live = true;
    brief
      .load()
      .then((s) => {
        if (!live) return;
        setStatus(s);
        const tz = browserTimeZone();
        // A brief that was never set up starts in the browser's time zone.
        const fresh = !s.settings.enabled && s.last_sent_at == null && s.settings.timezone === "UTC";
        setForm({ ...s.settings, timezone: fresh ? tz : s.settings.timezone });
      })
      .catch((err: Error) => live && setMessage({ ok: false, text: `Could not load the brief settings: ${err.message}` }))
      .finally(() => live && setBusy(null));
    return () => {
      live = false;
    };
  }, [brief]);

  const symbols = useWatchlist && watchlist.length ? watchlist : form.symbols;
  const toSave: BriefSettings = { ...form, symbols };
  const dirty = !status || JSON.stringify(status.settings) !== JSON.stringify(toSave);
  const sectionsChanged = status && JSON.stringify(status.settings.sections) !== JSON.stringify(form.sections);
  const channels = status?.channels;
  const hasChannel = Boolean(channels?.telegram || channels?.discord);

  const act = async (kind: "save" | "preview" | "send") => {
    setBusy(kind);
    setMessage(null);
    try {
      if (kind === "save") {
        const s = await brief.save(toSave);
        setStatus(s);
        setForm(s.settings);
        setMessage({ ok: true, text: s.settings.enabled ? `Saved. Next brief at ${s.settings.times.join(", ")} (${s.settings.timezone}).` : "Saved. The brief is off." });
      } else if (kind === "preview") {
        setPreview(await brief.preview(symbols.length ? symbols : undefined, form.interval));
      } else {
        const res = await brief.send(symbols.length ? symbols : undefined, form.interval);
        setPreview(res);
        const sent = Object.entries(res.results).filter(([, ok]) => ok).map(([c]) => CHANNEL_NAMES[c as keyof AlertChannels] ?? c);
        const failed = Object.entries(res.results).filter(([, ok]) => !ok).map(([c]) => CHANNEL_NAMES[c as keyof AlertChannels] ?? c);
        setMessage({
          ok: failed.length === 0,
          text: [sent.length ? `Sent to ${sent.join(" and ")}.` : "", failed.length ? `${failed.join(" and ")} failed; see the backend log.` : ""]
            .filter(Boolean)
            .join(" "),
        });
      }
    } catch (err) {
      setMessage({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(null);
    }
  };

  const setSection = (k: keyof BriefSections, v: boolean) => setForm((f) => ({ ...f, sections: { ...f.sections, [k]: v } }));
  const addTime = () => {
    if (!/^\d{2}:\d{2}$/.test(newTime) || form.times.includes(newTime) || form.times.length >= 8) return;
    setForm((f) => ({ ...f, times: [...f.times, newTime].sort() }));
  };

  if (busy === "load") return <p className="px-3 py-4 text-[12px] text-mute">Loading…</p>;
  return (
    <div className="space-y-3 px-3 py-2.5">
      <p className="text-[11px] leading-relaxed text-mute">
        A message to Telegram / Discord at the times you choose: price and 24h change per coin, change since the last
        brief, trend and RSI, the nearest zone, Kimi&apos;s latest signal and forecast, funding and open interest, key
        levels, and the day&apos;s economic events.
      </p>
      {status && !hasChannel && (
        <div className="rounded border border-yellow-400/30 bg-yellow-400/5 px-2 py-1.5 text-[11px] text-yellow-200">
          No channel configured: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or DISCORD_WEBHOOK_URL on the backend to receive the brief. Preview works without one.
        </div>
      )}
      <Checkbox
        checked={form.enabled}
        onChange={(v) => setForm((f) => ({ ...f, enabled: v }))}
        label={<span className="font-medium">Send the brief automatically</span>}
      />
      <div>
        <div className={LABEL}>Send at (local time)</div>
        <div className="mt-1 flex flex-wrap items-center gap-1">
          {form.times.map((t) => (
            <span key={t} className="inline-flex items-center gap-1 rounded border border-line bg-panel2 px-1.5 py-0.5 text-[12px] text-ink">
              {t}
              {form.times.length > 1 && (
                <button type="button" onClick={() => setForm((f) => ({ ...f, times: f.times.filter((x) => x !== t) }))} className="text-mute hover:text-down" aria-label={`Remove ${t}`}>
                  <X className="h-3 w-3" />
                </button>
              )}
            </span>
          ))}
          <input type="time" className={clsx(INPUT, "w-24")} value={newTime} onChange={(e) => setNewTime(e.target.value)} />
          <button type="button" onClick={addTime} className="btn-ghost h-7 px-1.5 text-[11px]" title="Add another send time, e.g. for each session">
            <Plus className="h-3.5 w-3.5" /> Add
          </button>
        </div>
      </div>
      <div className="grid grid-cols-[1fr_auto] gap-2">
        <Field label="Time zone">
          <select className={INPUT} value={form.timezone} onChange={(e) => setForm((f) => ({ ...f, timezone: e.target.value }))}>
            {(zones.includes(form.timezone) ? zones : [form.timezone, ...zones]).map((z) => (
              <option key={z} value={z}>
                {z}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Timeframe">
          <IntervalSelect value={form.interval} onChange={(v) => setForm((f) => ({ ...f, interval: v }))} />
        </Field>
      </div>
      <div className="space-y-1">
        <div className={LABEL}>Coins</div>
        <Checkbox
          checked={useWatchlist}
          onChange={setUseWatchlist}
          label={watchlist.length ? `Use my watchlist (${watchlist.length} coins)` : "Use my watchlist (it is empty)"}
        />
        <div className="text-[11px] text-mute">
          {symbols.length
            ? symbols.map(displaySymbol).join(", ")
            : `Default list: ${(status?.default_symbols ?? []).map(displaySymbol).join(", ")}`}
        </div>
        {useWatchlist && status && !sameList(status.settings.symbols, symbols) && (
          <div className="text-[11px] text-yellow-200">Your watchlist changed since the last save. Save to update the brief.</div>
        )}
      </div>
      <div className="space-y-1">
        <div className={LABEL}>Sections</div>
        <div className="grid grid-cols-2 gap-1">
          {(Object.keys(BRIEF_SECTION_NAMES) as (keyof BriefSections)[]).map((k) => (
            <Checkbox key={k} checked={form.sections[k]} onChange={(v) => setSection(k, v)} label={BRIEF_SECTION_NAMES[k]} />
          ))}
        </div>
        {sectionsChanged && <div className="text-[11px] text-mute">Section changes show in the preview after you save.</div>}
      </div>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => act("save")}
          disabled={busy !== null || !dirty}
          className="inline-flex h-7 items-center gap-1 rounded bg-accent px-3 text-[12px] font-medium text-white hover:bg-accent/90 disabled:opacity-50"
        >
          <Check className="h-3.5 w-3.5" />
          {busy === "save" ? "Saving…" : dirty ? "Save" : "Saved"}
        </button>
        <button type="button" onClick={() => act("preview")} disabled={busy !== null} className="btn-ghost h-7 px-2 text-[12px] disabled:opacity-50">
          <Eye className="h-3.5 w-3.5" />
          {busy === "preview" ? "Building…" : "Preview"}
        </button>
        <button
          type="button"
          onClick={() => act("send")}
          disabled={busy !== null || !hasChannel}
          className="btn-ghost h-7 px-2 text-[12px] disabled:opacity-50"
          title={hasChannel ? "Send it to Telegram / Discord now" : "No Telegram or Discord channel configured on the backend"}
        >
          <Send className="h-3.5 w-3.5" />
          {busy === "send" ? "Sending…" : "Send now"}
        </button>
      </div>
      {(busy === "preview" || busy === "send") && (
        <div className="text-[11px] text-mute">Scanning each coin (Kimi takes a few seconds per coin)…</div>
      )}
      {message && <div className={clsx("text-[11px]", message.ok ? "text-up" : "text-down")}>{message.text}</div>}
      {status?.last_sent_at && <div className="text-[11px] text-mute">Last sent {formatWhen(status.last_sent_at)}.</div>}
      {preview && (
        <div className="rounded border border-line bg-panel2/60">
          <div className="flex items-center gap-2 border-b border-line px-2 py-1 text-[11px] text-mute">
            <span className="flex-1">
              {preview.messages.length > 1 ? `${preview.messages.length} messages` : "1 message"} · built {formatWhen(Date.parse(preview.generated_at))}
              {preview.data_source === "synthetic" && " · demo data"}
            </span>
            <button type="button" onClick={() => setPreview(null)} className="hover:text-ink" aria-label="Close preview">
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words p-2 font-mono text-[10px] leading-4 text-ink">{preview.text}</pre>
        </div>
      )}
    </div>
  );
}
