// Read-only Binance account: the API key, the fill import and the positions. Mirrors backend/app/binance_account.py
// and backend/app/binance_import.py. The key and secret go to the backend once and never come back: the browser
// only ever sees the last 4 characters.

import { apiRequest } from "./api";
import { JOURNAL_EVENT } from "./journal";

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

/** GET /api/binance/positions: the user's own positions, and the bots' separately. */
export interface AccountPositions {
  manual: { spot: SpotHolding[]; futures: FuturesPosition[]; cash: { asset: string; qty: number }[] };
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
