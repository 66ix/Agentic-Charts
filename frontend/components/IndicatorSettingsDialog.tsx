"use client";

import { DEFAULT_INDICATOR_SETTINGS, type IndicatorSettings } from "@/lib/types";

import Dialog, { ColorInput, NumberInput, Row, Section } from "./Dialog";

interface Props {
  open: boolean;
  settings: IndicatorSettings;
  onChange(next: IndicatorSettings): void;
  onClose(): void;
}

const clampInt = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, Math.round(v)));

/** Lengths and colours of the built-in indicators. Every chart uses the same settings. */
export default function IndicatorSettingsDialog({ open, settings: s, onChange, onClose }: Props) {
  const set = <K extends keyof IndicatorSettings>(k: K, v: IndicatorSettings[K]) => onChange({ ...s, [k]: v });
  return (
    <Dialog open={open} title="Indicator settings" width={460} onClose={onClose}>
      <Section title="Moving averages">
        <Row label="First EMA">
          <NumberInput value={s.ema1.length} min={2} max={500} onChange={(v) => set("ema1", { ...s.ema1, length: clampInt(v, 2, 500) })} />
          <ColorInput value={s.ema1.color} onChange={(c) => set("ema1", { ...s.ema1, color: c })} />
        </Row>
        <Row label="Second EMA">
          <NumberInput value={s.ema2.length} min={2} max={500} onChange={(v) => set("ema2", { ...s.ema2, length: clampInt(v, 2, 500) })} />
          <ColorInput value={s.ema2.color} onChange={(c) => set("ema2", { ...s.ema2, color: c })} />
        </Row>
        <Row label="VWAP colour">
          <ColorInput value={s.vwapColor} onChange={(c) => set("vwapColor", c)} />
        </Row>
      </Section>
      <Section title="Bollinger Bands">
        <Row label="Length and width" hint="Standard deviations from the average">
          <NumberInput value={s.bb.length} min={2} max={500} onChange={(v) => set("bb", { ...s.bb, length: clampInt(v, 2, 500) })} />
          <NumberInput value={s.bb.mult} min={0.5} max={5} step={0.1} width={56} onChange={(v) => set("bb", { ...s.bb, mult: Math.max(0.5, Math.min(5, v)) })} />
          <ColorInput value={s.bb.color} onChange={(c) => set("bb", { ...s.bb, color: c })} />
        </Row>
      </Section>
      <Section title="Oscillators">
        <Row label="RSI length">
          <NumberInput value={s.rsi.length} min={2} max={100} onChange={(v) => set("rsi", { length: clampInt(v, 2, 100) })} />
        </Row>
        <Row label="MACD" hint="Fast, slow, signal">
          <NumberInput value={s.macd.fast} min={2} max={100} width={52} onChange={(v) => set("macd", { ...s.macd, fast: clampInt(v, 2, 100) })} />
          <NumberInput value={s.macd.slow} min={3} max={200} width={52} onChange={(v) => set("macd", { ...s.macd, slow: clampInt(v, 3, 200) })} />
          <NumberInput value={s.macd.signal} min={2} max={100} width={52} onChange={(v) => set("macd", { ...s.macd, signal: clampInt(v, 2, 100) })} />
        </Row>
        <Row label="Stoch RSI" hint="RSI, stochastic, %K, %D">
          <NumberInput value={s.stochRsi.rsiLength} min={2} max={100} width={46} onChange={(v) => set("stochRsi", { ...s.stochRsi, rsiLength: clampInt(v, 2, 100) })} />
          <NumberInput value={s.stochRsi.stochLength} min={2} max={100} width={46} onChange={(v) => set("stochRsi", { ...s.stochRsi, stochLength: clampInt(v, 2, 100) })} />
          <NumberInput value={s.stochRsi.k} min={1} max={20} width={40} onChange={(v) => set("stochRsi", { ...s.stochRsi, k: clampInt(v, 1, 20) })} />
          <NumberInput value={s.stochRsi.d} min={1} max={20} width={40} onChange={(v) => set("stochRsi", { ...s.stochRsi, d: clampInt(v, 1, 20) })} />
        </Row>
        <Row label="ATR length">
          <NumberInput value={s.atr.length} min={2} max={100} onChange={(v) => set("atr", { length: clampInt(v, 2, 100) })} />
        </Row>
      </Section>
      <div className="flex justify-end pt-2">
        <button type="button" className="btn-ghost h-7 border border-line px-2 text-[12px]" onClick={() => onChange(DEFAULT_INDICATOR_SETTINGS)}>
          Reset to defaults
        </button>
      </div>
    </Dialog>
  );
}
