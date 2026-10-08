"use client";

import { ChevronLeft, ChevronRight, Loader2, Pause, Play, X } from "lucide-react";

export interface ReplayState {
  /** Open times of every candle when replay started. */
  bars: number[];
  /** Index of the last candle shown. */
  i: number;
  playing: boolean;
  /** Candles per second while playing. */
  speed: number;
}

const SPEEDS = [1, 2, 5, 10];

function when(t: number) {
  return new Date(t * 1000).toLocaleString(undefined, { year: "2-digit", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** Replay controls: step, play, scrub, draw the levels as they were at this point, exit. */
export default function ReplayBar(p: {
  state: ReplayState;
  levelsBusy: boolean;
  levelsNote: string | null;
  onChange(next: ReplayState): void;
  onLevels(): void;
  onExit(): void;
}) {
  const { state: s } = p;
  const last = s.bars.length - 1;
  const go = (i: number) => p.onChange({ ...s, i: Math.max(1, Math.min(last, i)), playing: false });
  return (
    <div className="pointer-events-auto absolute bottom-24 left-1/2 z-30 flex max-w-[95%] -translate-x-1/2 flex-wrap items-center gap-1.5 rounded-lg border border-line bg-panel/95 px-2 py-1.5 text-[11px] shadow-xl backdrop-blur">
      <span className="font-semibold text-accent">Replay</span>
      <button type="button" className="btn-ghost h-6 w-6 p-0" aria-label="Back one candle" title="Back one candle" onClick={() => go(s.i - 1)}>
        <ChevronLeft className="h-4 w-4" />
      </button>
      <button
        type="button"
        className="btn-ghost h-6 w-6 p-0"
        aria-label={s.playing ? "Pause" : "Play"}
        onClick={() => p.onChange({ ...s, playing: !s.playing && s.i < last })}
      >
        {s.playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
      </button>
      <button type="button" className="btn-ghost h-6 w-6 p-0" aria-label="Forward one candle" title="Forward one candle" onClick={() => go(s.i + 1)}>
        <ChevronRight className="h-4 w-4" />
      </button>
      <select
        aria-label="Speed"
        className="h-6 rounded border border-line bg-transparent px-0.5 text-[11px] outline-none"
        value={s.speed}
        onChange={(e) => p.onChange({ ...s, speed: Number(e.target.value) })}
      >
        {SPEEDS.map((v) => (
          <option key={v} value={v}>
            {v}x
          </option>
        ))}
      </select>
      <input
        type="range"
        aria-label="Replay position"
        className="w-40 accent-accent"
        min={1}
        max={last}
        value={s.i}
        onChange={(e) => go(Number(e.target.value))}
      />
      <span className="w-28 font-mono text-mute">{when(s.bars[s.i])}</span>
      <button
        type="button"
        className="btn-ghost h-6 border border-line px-1.5 text-[11px]"
        disabled={p.levelsBusy}
        title="Draw the support/resistance and supply/demand zones the detectors saw with only the candles up to here"
        onClick={p.onLevels}
      >
        {p.levelsBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : "Levels at this point"}
      </button>
      {p.levelsNote && <span className="text-mute">{p.levelsNote}</span>}
      <button type="button" className="btn-ghost h-6 w-6 p-0" aria-label="Exit replay" title="Back to live" onClick={p.onExit}>
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
