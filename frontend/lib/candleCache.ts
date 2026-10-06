import type { Candle, Interval } from "./types";

/**
 * The last candles seen per symbol and interval, kept in IndexedDB so a reload
 * can draw the chart before the network answers. Storage can be unavailable
 * (private mode, blocked site data, SSR) or hang on open in some browsers, so
 * every call resolves to null or does nothing instead of throwing.
 */

const DB_NAME = "agentic-charts";
const STORE = "candles";
const MAX_BARS = 1500;
const OPEN_TIMEOUT_MS = 2000;

export interface CachedCandles {
  candles: Candle[];
  source: string;
  savedAt: number; // ms since epoch
}

let dbPromise: Promise<IDBDatabase | null> | null = null;

function openDb(): Promise<IDBDatabase | null> {
  dbPromise ??= new Promise<IDBDatabase | null>((resolve) => {
    const timer = setTimeout(() => resolve(null), OPEN_TIMEOUT_MS);
    const done = (db: IDBDatabase | null) => {
      clearTimeout(timer);
      resolve(db);
    };
    try {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => req.result.createObjectStore(STORE);
      req.onsuccess = () => {
        const db = req.result;
        db.onversionchange = () => {
          db.close(); // let a newer tab upgrade the schema; the next call reopens
          dbPromise = null;
        };
        done(db);
      };
      req.onerror = () => done(null);
    } catch {
      done(null); // no indexedDB (SSR) or access denied
    }
  });
  return dbPromise;
}

const key = (symbol: string, interval: Interval) => `${symbol}:${interval}`;

const isCandle = (c: Candle) =>
  typeof c === "object" &&
  c !== null &&
  [c.time, c.open, c.high, c.low, c.close, c.volume].every((n) => typeof n === "number" && Number.isFinite(n));

export async function readCandles(symbol: string, interval: Interval): Promise<CachedCandles | null> {
  const db = await openDb();
  if (!db) return null;
  return new Promise((resolve) => {
    try {
      const req = db.transaction(STORE, "readonly").objectStore(STORE).get(key(symbol, interval));
      req.onsuccess = () => {
        const v = req.result as CachedCandles | undefined;
        // A malformed entry would make the chart throw on every retry, so check it before use.
        const ok = v && typeof v.source === "string" && Array.isArray(v.candles) && v.candles.every(isCandle);
        resolve(ok ? v : null);
      };
      req.onerror = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
}

export async function writeCandles(symbol: string, interval: Interval, candles: Candle[], source: string) {
  // Copy now: the chart keeps appending live bars to the array it passed in.
  const entry: CachedCandles = { candles: candles.slice(-MAX_BARS), source, savedAt: Date.now() };
  const db = await openDb();
  if (!db) return;
  return new Promise<void>((resolve) => {
    try {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(entry, key(symbol, interval));
      tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
    } catch {
      resolve(); // closed by a version change, quota, private mode
    }
  });
}

/** `fresh` (oldest→newest) replaces cached bars from its first time on and extends past them. */
export function mergeCandles(cached: Candle[], fresh: Candle[]): Candle[] {
  if (!fresh.length) return cached.slice();
  const from = fresh[0].time;
  return [...cached.filter((c) => c.time < from), ...fresh];
}
