import { displaySymbol, formatPrice } from "./format";
import type { TradePlan } from "./types";

export interface SnapshotText {
  symbol: string;
  interval: string;
  /** The agent's latest answer about this chart, if any. */
  summary?: string;
  plan?: TradePlan;
  /** Labels of the levels on the chart, most important first. */
  levels?: string[];
}

const PAD = 20;
const FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";

function wrap(ctx: CanvasRenderingContext2D, text: string, width: number): string[] {
  const out: string[] = [];
  for (const para of text.split(/\n+/)) {
    let line = "";
    for (const word of para.split(/\s+/).filter(Boolean)) {
      const next = line ? `${line} ${word}` : word;
      if (ctx.measureText(next).width > width && line) {
        out.push(line);
        line = word;
      } else line = next;
    }
    if (line) out.push(line);
  }
  return out;
}

function theme() {
  const css = getComputedStyle(document.body);
  return {
    bg: css.backgroundColor && css.backgroundColor !== "rgba(0, 0, 0, 0)" ? css.backgroundColor : "#0b0f17",
    ink: css.color || "#e5e7eb",
  };
}

/**
 * The chart image with a title bar on top (coin, timeframe, time) and, under it, the agent's answer, the trade
 * plan and the levels on the chart, so one picture carries the whole read when it is shared.
 */
export function composeSnapshot(chart: HTMLCanvasElement, t: SnapshotText): HTMLCanvasElement {
  const { bg, ink } = theme();
  const W = chart.width;
  const scale = Math.max(1, W / 1200);
  const size = (px: number) => Math.round(px * scale);
  const measure = document.createElement("canvas").getContext("2d")!;
  const textW = W - 2 * size(PAD);

  const blocks: Array<{ font: string; color: string; lines: string[]; gap: number }> = [];
  const body = `${size(15)}px ${FONT}`;
  const small = `${size(13)}px ${FONT}`;
  const add = (font: string, color: string, text: string, gap = size(10)) => {
    measure.font = font;
    blocks.push({ font, color, lines: wrap(measure, text, textW), gap });
  };
  if (t.summary) add(body, ink, t.summary);
  if (t.plan) {
    const p = t.plan;
    const tg = p.targets.map((x) => formatPrice(x.price)).join(" / ");
    add(`600 ${body}`, p.direction === "long" ? "#22c55e" : "#ef4444",
      `${p.direction === "long" ? "Long" : "Short"} plan: entry ${formatPrice(p.entry)}, stop ${formatPrice(p.stop)}, targets ${tg}`);
  }
  if (t.levels?.length) add(small, "#94a3b8", `On the chart: ${t.levels.join(" · ")}`);

  const titleH = size(44);
  const lineH = (font: string) => Math.round(parseInt(font.replace(/^\D*/, ""), 10) * 1.4);
  const textH = blocks.reduce((h, b) => h + b.lines.length * lineH(b.font) + b.gap, 0);
  const footH = size(28);
  const out = document.createElement("canvas");
  out.width = W;
  out.height = titleH + chart.height + (blocks.length ? textH + size(PAD) : 0) + footH;
  const ctx = out.getContext("2d")!;
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, out.width, out.height);

  ctx.textBaseline = "middle";
  ctx.fillStyle = ink;
  ctx.font = `600 ${size(18)}px ${FONT}`;
  const title = `${displaySymbol(t.symbol)} · ${t.interval}`;
  ctx.fillText(title, size(PAD), titleH / 2);
  ctx.font = small;
  ctx.fillStyle = "#94a3b8";
  const when = new Date().toISOString().slice(0, 16).replace("T", " ") + " UTC";
  ctx.fillText(when, W - size(PAD) - ctx.measureText(when).width, titleH / 2);
  ctx.drawImage(chart, 0, titleH);

  let y = titleH + chart.height + size(PAD);
  ctx.textBaseline = "top";
  for (const b of blocks) {
    ctx.font = b.font;
    ctx.fillStyle = b.color;
    for (const line of b.lines) {
      ctx.fillText(line, size(PAD), y);
      y += lineH(b.font);
    }
    y += b.gap;
  }
  ctx.font = `${size(11)}px ${FONT}`;
  ctx.fillStyle = "#64748b";
  ctx.textBaseline = "middle";
  ctx.fillText("Agentic Charts · not financial advice", size(PAD), out.height - footH / 2);
  return out;
}

/**
 * Shares the snapshot where the device can (phones), otherwise downloads it and copies it to the clipboard.
 * Resolves with what happened, for the note shown to the user.
 */
export async function shareSnapshot(canvas: HTMLCanvasElement, name: string): Promise<string> {
  const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) return "Couldn't make the snapshot";
  const file = new File([blob], name, { type: "image/png" });
  const coarse = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
  if (coarse && navigator.canShare?.({ files: [file] })) {
    try {
      await navigator.share({ files: [file], title: name });
      return "Snapshot shared";
    } catch {
      /* cancelled: fall through to saving it */
    }
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    return "Snapshot saved and copied";
  } catch {
    return "Snapshot saved";
  }
}
