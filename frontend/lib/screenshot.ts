import { apiRequest } from "./api";
import type { Interval, Overlay } from "./types";

/** Mirrors the /api/screenshot response in backend/app/main.py. */
export interface ScreenshotResult {
  /** The coin the image shows, when it could be loaded; null when neither it nor the open chart could. */
  symbol: string | null;
  interval: Interval;
  /** The drawings read off the image, as overlays (kind "screenshot"). */
  overlays: Overlay[];
  /** The levels the app's own detectors find on the real candles. */
  detected: Overlay[];
  summary: string;
  engine: string;
  data_source: string | null;
}

const MAX_SIDE = 1800;

/**
 * An image file or pasted image as a data URL, scaled down so its longest side is at most MAX_SIDE pixels (enough
 * to read the axis labels) and sent as PNG when small, JPEG otherwise.
 */
export async function imageToDataUrl(file: Blob): Promise<string> {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error("That file is not an image the browser can open"));
      el.src = url;
    });
    const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
    canvas.getContext("2d")!.drawImage(img, 0, 0, canvas.width, canvas.height);
    const png = canvas.toDataURL("image/png");
    return png.length < 3_000_000 ? png : canvas.toDataURL("image/jpeg", 0.9);
  } finally {
    URL.revokeObjectURL(url);
  }
}

export function readScreenshot(image: string, symbol: string, interval: Interval, signal?: AbortSignal) {
  return apiRequest<ScreenshotResult>("/api/screenshot", {
    method: "POST",
    body: JSON.stringify({ image, symbol, interval }),
    signal,
    timeoutMs: 120_000,
  });
}

/** The first image in a paste or drop, if any. */
export function imageFrom(data: DataTransfer | null): File | null {
  if (!data) return null;
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind === "file" && item.type.startsWith("image/")) return item.getAsFile();
  }
  return Array.from(data.files ?? []).find((f) => f.type.startsWith("image/")) ?? null;
}
