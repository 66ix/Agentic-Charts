// Read-only Binance account: the API key, the fill import and the positions. Mirrors backend/app/binance_account.py
// and backend/app/binance_import.py. The key and secret go to the backend once and never come back: the browser
// only ever sees the last 4 characters.

import { apiRequest } from "./api";
import { JOURNAL_EVENT } from "./journal";
import type { HorizontalLineOverlay } from "./types";

export type FillKind = "manual" | "bot" | "unknown";
export type AccountMarket = "spot" | "futures";

/** GET /api/binance/key. Never contains the secret or the full key. */
export interface BinanceKeyStatus {
  configured: boolean;
  /** "env" = BINANCE_API_KEY / BINANCE_API_SECRET on the server (can't be changed from the app), "file" = entered here. */
  source: "env" | "file" | null;
  key_last4: string | null;
  masked: string | null;
  added_at: number | null;
  /** The last time Binance was asked what the key may do. */
  checked_at: number | null;
  /** true = read-only; false = refused (see problems); null = not checked yet. */
  ok: boolean | null;
  /** e.g. "allows withdrawals", "does not have reading enabled". */
  problems: string[];
  /** Binance's flags, e.g. { enableReading: true, enableWithdrawals: false, ... }. */
  permissions: Record<string, boolean> | null;
  error: string | null;
}

export interface ImportSettings {
  /** Re-import every N minutes (0 = off). */
  auto_minutes: number;
  /** Spot pairs to import besides the wallet's coins and the grid bots' pairs. */
  symbols: string[];
  futures: boolean;
  /** How far back the first USD-M futures import goes. */
  lookback_days: number;
  /** Coins never requested from Binance and left out of holdings (airdropped dust like ETHW). */
  hidden_assets?: string[];
}

export interface ImportRun {
  at: number;
  auto: boolean;
  new_fills?: number;
  symbols?: number;
  journal?: { added: number; updated: number; removed: number };
  notes?: string[];
  error: string | null;
  seconds?: number;
}

/** GET /api/binance/import */
export interface ImportStatus {
  settings: ImportSettings;
  last_import: ImportRun | null;
  fills: number;
  by_kind: Record<FillKind, number>;
  running: boolean;
  key: BinanceKeyStatus;
  auto_minutes_options: number[];
  /** Pairs Binance said don't exist: never requested again. */
  invalid_symbols?: string[];
  /** Always false: Binance has no public API for Spot Grid bots. */
  spot_grid_api: boolean;
  spot_grid_note: string;
}

/** One fill on the user's account, with how it was classified and why. */
export interface AccountFill {
  /** "spot:BTCUSDT:<trade id>" or "futures:BTCUSDT:<trade id>". */
  key: string;
  market: AccountMarket;
  symbol: string;
  trade_id: number;
  order_id: number;
  time: number;
  time_ms: number;
  side: "buy" | "sell";
  price: number;
  qty: number;
  quote_qty: number;
  commission: number;
  commission_asset: string;
  maker: boolean;
  realized_pnl: number | null;
  position_side: string;
  client_order_id: string | null;
  kind: FillKind;
  bot_id: string | null;
  reason: string;
  /** The user set the kind. */
  overridden: boolean;
}

/** A round trip (flat → position → flat) rebuilt from fills of one kind. */
export interface RoundTrip {
  external_id: string;
  market: AccountMarket;
  symbol: string;
  position_side: string;
  direction: "long" | "short";
  status: "open" | "closed";
  opened_at: number;
  closed_at: number | null;
  qty: number;
  entry_price: number;
  exit_price: number | null;
  realized_pnl: number;
  fees: number;
  quote_asset: string;
  fills: string[];
  kind: FillKind;
  bot_id: string | null;
  notes: string[];
}

export interface SpotHolding {
  /** "holding:spot:BTC", for overrides. */
  key: string;
  asset: string;
  symbol: string;
  /** Everything in the spot wallet. */
  qty: number;
  /** Less what fills matched to a grid bot account for. */
  own_qty: number;
  /** From the user's own buys; null when none were imported. */
  avg_entry: number | null;
  from_fills_qty: number;
  /** Coins with no imported buy (bought before the history, deposits). */
  untracked_qty: number;
  price: number | null;
  value: number | null;
  unrealized_pnl: number | null;
  kind: FillKind;
  bot_id: string | null;
  /** The user set the kind. */
  overridden: boolean;
  reason: string;
}

export interface FuturesPosition {
  /** "position:futures:BTCUSDT:BOTH", for overrides. */
  key: string;
  symbol: string;
  position_side: string;
  side: "long" | "short";
  qty: number;
  entry_price: number;
  mark_price: number | null;
  unrealized_pnl: number;
  leverage: string | number | null;
  liquidation_price: number | null;
  kind: FillKind;
  bot_id: string | null;
  overridden: boolean;
  reason: string;
}

export interface WalletBalance {
  wallet: string;
  usdt: number;
  active: boolean;
}

/** Coins in Simple Earn (flexible or locked): held, but not in the spot wallet. */
export interface EarnPosition {
  key: string;
  product: "flexible" | "locked";
  asset: string;
  symbol: string;
  qty: number;
  /** Annual rate, in percent. */
  apr_pct: number | null;
  price: number | null;
  value: number | null;
  /** Flexible: rewards paid so far, in the coin. */
  rewards_total?: number | null;
  can_redeem?: boolean;
  /** Locked: the term and when it ends (UNIX s). */
  duration_days?: number | null;
  redeem_at?: number | null;
  auto_renew?: boolean;
}

/** One coin you own, the spot wallet and Simple Earn together (binance_import.merge_holdings). */
export interface MergedHolding {
  /** "holding:spot:INJ": the same key as the spot holding, so a managed trade survives a move to Earn. */
  key: string;
  asset: string;
  symbol: string;
  qty: number;
  spot_qty: number;
  earn_qty: number;
  locked_qty: number;
  /** Earliest end of a locked Earn term (UNIX s). */
  redeem_at: number | null;
  /** Coins Earn paid as rewards: counted in the value, not the cost. */
  rewards_qty: number;
  avg_entry: number | null;
  price: number | null;
  value: number | null;
  unrealized_pnl: number | null;
  sources: ("spot" | "earn")[];
  /** False when Binance has no USDT pair for it (nothing to chart, price or alert on). */
  tradable?: boolean;
}

/** GET /api/binance/positions: the user's own positions, and the bots' separately. */
export interface AccountPositions {
  manual: {
    spot: SpotHolding[];
    futures: FuturesPosition[];
    cash: { asset: string; qty: number }[];
    /** Absent from servers older than the Simple Earn support. */
    earn?: EarnPosition[];
    /** Every coin owned, spot and Earn merged; absent from older servers. */
    holdings?: MergedHolding[];
  };
  bots: {
    /** The "Trading Bots" wallet's total (Binance reports only the total, not the coins). */
    wallet: WalletBalance | null;
    /** Spot holdings marked as a bot's, and the coins fills matched to tracked bots account for. */
    spot: (SpotHolding | { bot_id: string; asset: string; symbol: string; qty: number; reason: string })[];
    futures: FuturesPosition[];
    /** The app's tracked grid bots with their simulated holdings. */
    tracked: { bot_id: string; name: string; symbol: string; base_held: number | null; quote_held: number | null; value: number | null; simulated: boolean }[];
  };
  wallets: WalletBalance[];
  notes: string[];
  updated_at: number;
}

const SLOW = 180_000;

function journalChanged() {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(JOURNAL_EVENT));
}

// ------------------------------------------------------------------ key --

export function fetchBinanceKey(signal?: AbortSignal) {
  return apiRequest<BinanceKeyStatus>("/api/binance/key", { signal });
}

/** Checked with Binance first; refused (422, with the reason) unless the key is read-only. */
export function saveBinanceKey(api_key: string, api_secret: string) {
  return apiRequest<BinanceKeyStatus>("/api/binance/key", {
    method: "PUT",
    body: JSON.stringify({ api_key, api_secret }),
    timeoutMs: 30_000,
  });
}

export function testBinanceKey() {
  return apiRequest<BinanceKeyStatus>("/api/binance/key/test", { method: "POST", timeoutMs: 30_000 });
}

export function removeBinanceKey() {
  return apiRequest<BinanceKeyStatus>("/api/binance/key", { method: "DELETE" });
}

// --------------------------------------------------------------- import --

export function fetchImportStatus(signal?: AbortSignal) {
  return apiRequest<ImportStatus>("/api/binance/import", { signal });
}

/** Hide a coin from holdings (and never request it from Binance), or show it again. */
export async function setAssetHidden(asset: string, hidden: boolean) {
  const st = await fetchImportStatus();
  const now = st.settings.hidden_assets ?? [];
  const next = hidden ? [...new Set([...now, asset.toUpperCase()])] : now.filter((a) => a !== asset.toUpperCase());
  return saveImportSettings({ ...st.settings, hidden_assets: next });
}

export function saveImportSettings(settings: ImportSettings) {
  return apiRequest<ImportStatus>("/api/binance/import/settings", { method: "PUT", body: JSON.stringify(settings) });
}

/** Fetch new fills, classify them and sync the journal (the first import of a busy account can take a while). */
export async function runImport(symbols: string[] = []) {
  const out = await apiRequest<ImportRun>("/api/binance/import", {
    method: "POST",
    body: JSON.stringify({ symbols }),
    timeoutMs: SLOW,
  });
  journalChanged();
  return out;
}

export function fetchAccountFills(filter: { symbol?: string; kind?: FillKind; market?: AccountMarket; limit?: number } = {}, signal?: AbortSignal) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(filter)) if (v != null && v !== "") q.set(k, String(v));
  const qs = q.toString();
  return apiRequest<{ fills: AccountFill[] }>(`/api/binance/fills${qs ? `?${qs}` : ""}`, { signal });
}

export function fetchRoundTrips(kind?: FillKind, signal?: AbortSignal) {
  return apiRequest<{ trades: RoundTrip[] }>(`/api/binance/trades${kind ? `?kind=${kind}` : ""}`, { signal });
}

/** Override the classification of fills, a holding or a position (kind null restores the app's own). Persisted on
 *  the server; the journal is re-synced (only manual closed trades are in it). */
export async function classifyAccount(keys: string[], kind: FillKind | null, botId?: string | null) {
  const out = await apiRequest<{ journal: { added: number; updated: number; removed: number }; updated: number }>(
    "/api/binance/classify",
    { method: "POST", body: JSON.stringify({ keys, kind, bot_id: botId ?? null }), timeoutMs: 60_000 },
  );
  journalChanged();
  return out;
}

export function fetchAccountPositions(refresh = false, signal?: AbortSignal) {
  return apiRequest<AccountPositions>(`/api/binance/positions${refresh ? "?refresh=true" : ""}`, { signal, timeoutMs: 60_000 });
}

/** One calendar day of realized PnL (backend/app/pnl_calendar.py), in USD. */
export interface PnlDay {
  /** "2026-10-07", in the browser's time zone. */
  date: string;
  pnl: number;
  spot: number;
  futures: number;
  /** Fills that closed (part of) a position that day. */
  closes: number;
}

/** GET /api/binance/pnl-calendar: realized PnL per day from the imported fills. */
export interface PnlCalendar {
  days: PnlDay[];
  total: number;
  win_days: number;
  loss_days: number;
  best: PnlDay | null;
  worst: PnlDay | null;
  currency: "USD";
  notes: string[];
  kind: FillKind | null;
  fills: number;
  last_import: number | null;
}

export function fetchPnlCalendar(kind: FillKind | null, signal?: AbortSignal) {
  const q = new URLSearchParams({ tz_offset: String(new Date().getTimezoneOffset()) });
  if (kind) q.set("kind", kind);
  return apiRequest<PnlCalendar>(`/api/binance/pnl-calendar?${q}`, { signal });
}

/** Holdings watch (backend/app/holdings_watch.py): sell-or-trim alerts kept on the coins you hold. */
export interface HoldingsWatchSettings {
  enabled: boolean;
  interval: "1h" | "4h" | "1d";
  include_bots: boolean;
  min_value: number;
}

export interface HoldingsWatchStatus {
  settings: HoldingsWatchSettings;
  last: {
    at: number;
    coins: { symbol: string; value: number | null; source: "spot" | "grid bot" }[];
    added: string[];
    removed: string[];
    notes: string[];
    error: string | null;
  } | null;
  bot_note: string;
}

export function fetchHoldingsWatch(signal?: AbortSignal) {
  return apiRequest<HoldingsWatchStatus>("/api/holdings-watch", { signal });
}

export function saveHoldingsWatch(settings: HoldingsWatchSettings) {
  return apiRequest<HoldingsWatchStatus>("/api/holdings-watch", { method: "PUT", body: JSON.stringify(settings), timeoutMs: 60_000 });
}

export function runHoldingsWatch() {
  return apiRequest<HoldingsWatchStatus>("/api/holdings-watch/run", { method: "POST", timeoutMs: 60_000 });
}

/** Your own position on `symbol` as chart lines: the spot average entry, a futures entry and its liquidation. */
export function positionOverlays(pos: AccountPositions | null, symbol: string): HorizontalLineOverlay[] {
  if (!pos) return [];
  const out: HorizontalLineOverlay[] = [];
  const spot = pos.manual.spot.find((h) => h.symbol === symbol && h.avg_entry && (h.value ?? 0) >= 5);
  if (spot?.avg_entry) {
    const pct = spot.price ? (spot.price / spot.avg_entry - 1) * 100 : null;
    out.push({
      type: "horizontal_line",
      id: `my-entry-${symbol}`,
      kind: "my_entry",
      label: pct == null ? "My avg entry" : `My avg entry (${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%)`,
      price: spot.avg_entry,
      color: "#facc15",
      line_style: "dashed",
      line_width: 1,
    });
  }
  for (const f of pos.manual.futures.filter((p) => p.symbol === symbol)) {
    out.push({
      type: "horizontal_line",
      id: `my-fut-${f.key}`,
      kind: "my_entry",
      label: `My ${f.side} entry${f.leverage ? ` ${f.leverage}x` : ""}`,
      price: f.entry_price,
      color: f.side === "long" ? "#22c55e" : "#ef4444",
      line_style: "dashed",
      line_width: 1,
    });
    if (f.liquidation_price) {
      out.push({
        type: "horizontal_line",
        id: `my-liq-${f.key}`,
        kind: "my_entry",
        label: `My ${f.side} liquidation`,
        price: f.liquidation_price,
        color: "#f97316",
        line_style: "dotted",
        line_width: 1,
      });
    }
  }
  return out;
}
