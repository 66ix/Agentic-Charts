import { apiRequest } from "./api";
import { formatPrice } from "./format";
import type { AlertSpec, Drawing, Interval, Overlay, PriceAlert } from "./types";

export const ALERT_COLOR = "#fbbf24";

/** A horizontal ray or rectangle the user drew → an alert spec, or null for other drawings. */
export function alertFromDrawing(d: Drawing): AlertSpec | null {
  if (d.type === "hray" && d.points[0]) {
    const price = d.points[0].price;
    return { kind: "cross", price, price_low: null, price_high: null, label: `Ray ${formatPrice(price)}` };
  }
  if (d.type === "rect" && d.points.length >= 2) {
    const lo = Math.min(d.points[0].price, d.points[1].price);
    const hi = Math.max(d.points[0].price, d.points[1].price);
    return { kind: "zone", price: null, price_low: lo, price_high: hi, label: `Box ${formatPrice(lo)}–${formatPrice(hi)}` };
  }
  return null;
}

export function describeAlert(a: AlertSpec, fired = false): string {
  if (a.kind === "zone" && a.price_low != null && a.price_high != null) {
    return `${fired ? "entered" : "enters"} ${formatPrice(a.price_low)}–${formatPrice(a.price_high)}`;
  }
  return `${fired ? "crossed" : "crosses"} ${a.price != null ? formatPrice(a.price) : "?"}`;
}

/** Armed alerts for the chart's symbol, drawn as amber dotted lines / outlined boxes. Overlay ids are
 *  `alert-<alert id>`, so a dragged line maps back to its alert (see `alertIdFromOverlay`). */
export function alertOverlays(alerts: PriceAlert[], symbol: string): Overlay[] {
  const out: Overlay[] = [];
  for (const a of alerts) {
    if (!a.armed || a.symbol !== symbol) continue;
    if (a.kind === "zone" && a.price_low != null && a.price_high != null) {
      out.push({
        type: "box", id: `alert-${a.id}`, kind: "alert", label: `Alert: ${a.label}`, color: "rgba(251,191,36,0.06)",
        border_color: ALERT_COLOR, price_low: a.price_low, price_high: a.price_high,
      });
    } else if (a.price != null) {
      out.push({
        type: "horizontal_line", id: `alert-${a.id}`, kind: "alert", label: `Alert: ${a.label}`, color: ALERT_COLOR,
        price: a.price, line_style: "dotted", line_width: 1,
      });
    }
  }
  return out;
}

/** The alert id behind an overlay from `alertOverlays`, or null. */
export function alertIdFromOverlay(o: Overlay): string | null {
  return o.kind === "alert" && o.id?.startsWith("alert-") ? o.id.slice(6) : null;
}

// ------------------------------------------------------- price alert edits --

/** PATCH /api/alerts/{id}: only the fields sent change; `expires_at: null` removes the expiry. */
export interface AlertPatch {
  price?: number;
  price_low?: number;
  price_high?: number;
  label?: string;
  note?: string;
  repeat?: boolean;
  expires_at?: number | null;
}

const HOUR = 3_600_000;
/** The expiry picker's presets; "custom" opens a date-time input. */
export const EXPIRY_OPTIONS = [
  { value: "never", label: "Never", ms: null },
  { value: "1h", label: "1 hour", ms: HOUR },
  { value: "4h", label: "4 hours", ms: 4 * HOUR },
  { value: "1d", label: "1 day", ms: 24 * HOUR },
  { value: "1w", label: "1 week", ms: 7 * 24 * HOUR },
  { value: "custom", label: "Custom…", ms: null },
] as const;
export type ExpiryChoice = (typeof EXPIRY_OPTIONS)[number]["value"];

/** "in 3h", "in 2d", "expired" — for the alert row. */
export function expiryLabel(expiresAt: number | null | undefined, now = Date.now()): string | null {
  if (expiresAt == null) return null;
  const left = expiresAt - now;
  if (left <= 0) return "expired";
  if (left < HOUR) return `expires in ${Math.max(1, Math.round(left / 60_000))}m`;
  if (left < 48 * HOUR) return `expires in ${Math.round(left / HOUR)}h`;
  return `expires in ${Math.round(left / (24 * HOUR))}d`;
}

/** ms → the value of an `<input type="datetime-local">` in the browser's zone. */
export function toLocalInput(ms: number): string {
  const d = new Date(ms - new Date(ms).getTimezoneOffset() * 60_000);
  return d.toISOString().slice(0, 16);
}

export function timeAgo(ms: number, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86_400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86_400)}d ago`;
}

export function formatWhen(ms: number): string {
  return new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// ------------------------------------------------------------ signal alerts --

export type SignalId =
  | "kimi_buy"
  | "kimi_sell"
  | "kimi_any"
  | "rsi_bull_div"
  | "rsi_bear_div"
  | "sweep_low"
  | "sweep_high"
  | "new_demand"
  | "new_supply"
  | "bos_bull"
  | "bos_bear"
  | "rsi_overbought"
  | "rsi_oversold";

/** Plain-English names, in the order the picker lists them. Mirrors SIGNALS in backend/app/signal_alerts.py. */
export const SIGNAL_OPTIONS: { id: SignalId; name: string; hint: string }[] = [
  { id: "kimi_buy", name: "Kimi B+ (buy)", hint: "Kimi Cooked prints a B+ divergence buy label" },
  { id: "kimi_sell", name: "Kimi B- (sell)", hint: "Kimi Cooked prints a B- divergence sell label" },
  { id: "kimi_any", name: "Any Kimi label", hint: "Kimi Cooked prints any label: B+, B-, U, Dn, B+? or B-?" },
  { id: "rsi_bull_div", name: "RSI bullish divergence", hint: "Price makes a lower low while RSI makes a higher low" },
  { id: "rsi_bear_div", name: "RSI bearish divergence", hint: "Price makes a higher high while RSI makes a lower high" },
  { id: "sweep_low", name: "Sweep of a low", hint: "A wick takes out a swing low and the candle closes back above it" },
  { id: "sweep_high", name: "Sweep of a high", hint: "A wick takes out a swing high and the candle closes back below it" },
  { id: "new_demand", name: "New demand zone", hint: "A fresh demand zone forms: a tight base, then a strong move up" },
  { id: "new_supply", name: "New supply zone", hint: "A fresh supply zone forms: a tight base, then a strong move down" },
  { id: "bos_bull", name: "Bullish structure break", hint: "A candle closes above the last swing high (BOS or CHoCH)" },
  { id: "bos_bear", name: "Bearish structure break", hint: "A candle closes below the last swing low (BOS or CHoCH)" },
  { id: "rsi_overbought", name: "RSI crosses above 70", hint: "RSI moves up into overbought" },
  { id: "rsi_oversold", name: "RSI crosses below 30", hint: "RSI moves down into oversold" },
];

export function signalName(id: string): string {
  return SIGNAL_OPTIONS.find((s) => s.id === id)?.name ?? id;
}

/** A stored signal alert. Mirrors SignalAlert in backend/app/signal_alerts.py. */
export interface SignalAlert {
  id: string;
  symbol: string;
  interval: Interval;
  signal: SignalId;
  armed: boolean;
  /** Stay armed after firing (default true). */
  repeat: boolean;
  note: string;
  created_at: number; // ms
  last_fired_at: number | null; // ms
  fire_count: number;
  /** Open time (UNIX s) of the candle it last fired on. */
  last_bar: number | null;
  last_text: string;
  last_price: number | null;
}

export interface SignalAlertPatch {
  armed?: boolean;
  repeat?: boolean;
  note?: string;
}

/** `/ws/alerts` {type: "signal_fired"}: `time` is the candle's open time (UNIX s). */
export interface SignalFired {
  alert: SignalAlert;
  text: string;
  price: number;
  time: number;
}

export interface SignalPreview {
  symbol: string;
  interval: Interval;
  signal: SignalId;
  name: string;
  bars: number;
  data_source: string;
  /** Newest first. `time` is the candle's open time (UNIX s). */
  hits: { time: number; price: number; text: string }[];
  note?: string;
}

export function fetchSignalAlerts(signal?: AbortSignal) {
  return apiRequest<{ alerts: SignalAlert[]; signals: { id: SignalId; name: string; description: string }[] }>(
    "/api/signal-alerts",
    { signal },
  );
}

/** One alert per symbol (1–40). A coin that already has this exact alert gets it back, re-armed. */
export function createSignalAlerts(body: {
  symbols: string[];
  interval: Interval;
  signal: SignalId;
  repeat?: boolean;
  note?: string;
}) {
  return apiRequest<{ alerts: SignalAlert[] }>("/api/signal-alerts", { method: "POST", body: JSON.stringify(body) });
}

export function updateSignalAlert(id: string, patch: SignalAlertPatch) {
  return apiRequest<{ alert: SignalAlert }>(`/api/signal-alerts/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function deleteSignalAlert(id: string) {
  return apiRequest<{ ok: boolean }>(`/api/signal-alerts/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** When the signal would have fired on the last `bars` closed candles. Takes a few seconds (Kimi: longer). */
export function previewSignalAlert(symbol: string, interval: Interval, signal: SignalId, bars = 300, abort?: AbortSignal) {
  const q = new URLSearchParams({ symbol, interval, signal, bars: String(bars) });
  return apiRequest<SignalPreview>(`/api/signal-alerts/preview?${q}`, { signal: abort, timeoutMs: 90_000 });
}

// ------------------------------------------------------------------- history --

export type AlertHistoryKind = "price" | "signal" | "brief";

/** One fire. Mirrors AlertHistory items in backend/app/alerts.py. */
export interface AlertHistoryItem {
  id: string;
  time: number; // ms
  kind: AlertHistoryKind;
  /** Empty for a brief. */
  symbol: string;
  title: string;
  text: string;
  price?: number;
  alert_id?: string;
}

export function fetchAlertHistory(limit = 200, signal?: AbortSignal) {
  return apiRequest<{ items: AlertHistoryItem[] }>(`/api/alerts/history?limit=${limit}`, { signal });
}

export function clearAlertHistory() {
  return apiRequest<{ removed: number }>("/api/alerts/history", { method: "DELETE" });
}

// --------------------------------------------------------------------- brief --

export interface BriefSections {
  zones: boolean;
  kimi: boolean;
  derivatives: boolean;
  events: boolean;
  levels: boolean;
}

/** Mirrors BriefSettings in backend/app/brief.py. */
export interface BriefSettings {
  enabled: boolean;
  /** Local send times, "HH:MM". */
  times: string[];
  /** IANA zone, e.g. "Europe/London". */
  timezone: string;
  /** Empty = the server's default watchlist. */
  symbols: string[];
  interval: Interval;
  sections: BriefSections;
}

export interface BriefStatus {
  settings: BriefSettings;
  channels: { telegram: boolean; discord: boolean };
  last_sent_at: number | null; // ms
  default_symbols: string[];
}

export interface BriefPreview {
  text: string;
  /** The text split into the messages a channel receives. */
  messages: string[];
  generated_at: string; // ISO
  symbols: string[];
  interval: Interval;
  prices: Record<string, number>;
  data_source: string;
}

export const BRIEF_SECTION_NAMES: Record<keyof BriefSections, string> = {
  zones: "Zones",
  kimi: "Kimi signals",
  derivatives: "Funding & OI",
  events: "Economic events",
  levels: "Key levels",
};

export function fetchBriefSettings(signal?: AbortSignal) {
  return apiRequest<BriefStatus>("/api/brief/settings", { signal });
}

export function saveBriefSettings(settings: BriefSettings) {
  return apiRequest<BriefStatus>("/api/brief/settings", { method: "PUT", body: JSON.stringify(settings) });
}

function briefQuery(symbols?: string[], interval?: Interval) {
  const q = new URLSearchParams();
  if (symbols?.length) q.set("symbols", symbols.slice(0, 40).join(","));
  if (interval) q.set("interval", interval);
  const s = q.toString();
  return s ? `?${s}` : "";
}

/** Builds the brief now without sending it; `symbols` / `interval` override the saved settings. */
export function previewBrief(symbols?: string[], interval?: Interval, signal?: AbortSignal) {
  return apiRequest<BriefPreview>(`/api/brief/preview${briefQuery(symbols, interval)}`, { signal, timeoutMs: 180_000 });
}

/** Sends the brief to Telegram / Discord now. Rejects (400) when no channel is configured. */
export function sendBrief(symbols?: string[], interval?: Interval) {
  return apiRequest<BriefPreview & { results: Record<string, boolean> }>(
    `/api/brief/send${briefQuery(symbols, interval)}`,
    { method: "POST", timeoutMs: 180_000 },
  );
}

/** The browser's IANA time zone, e.g. "Europe/London". */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** IANA zones for the picker (the browser's list when it has one). */
export function timeZones(): string[] {
  try {
    const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
    const zones = intl.supportedValuesOf?.("timeZone");
    if (zones?.length) return zones.includes("UTC") ? zones : ["UTC", ...zones];
  } catch {
    /* older browser */
  }
  return ["UTC", "Europe/London", "Europe/Berlin", "America/New_York", "America/Chicago", "America/Los_Angeles",
    "Asia/Tokyo", "Asia/Singapore", "Asia/Hong_Kong", "Asia/Dubai", "Australia/Sydney"];
}
