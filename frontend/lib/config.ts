export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
export const WS_URL = (process.env.NEXT_PUBLIC_WS_URL ?? API_URL.replace(/^http/, "ws")).replace(/\/$/, "");

export const DEFAULT_SYMBOL = "INJUSDT";
export const DEFAULT_INTERVAL = "4h" as const;
export const HISTORY_BARS = 500;
