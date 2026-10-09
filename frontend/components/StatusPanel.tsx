"use client";

import clsx from "clsx";
import { Loader2, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { useAppStatus } from "@/hooks/useAppStatus";
import type { AppStatus, JobState } from "@/lib/status";

const REFRESH_MS = 30_000;

function ago(ts: number | null | undefined, now: number): string {
  if (!ts) return "never";
  const s = Math.max(0, now - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86_400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86_400)}d ago`;
}

function every(s: number): string {
  return s >= 86_400 ? `${Math.round(s / 86_400)}d` : s >= 3600 ? `${Math.round(s / 3600)}h` : s >= 60 ? `${Math.round(s / 60)}m` : `${s}s`;
}

const DOT: Record<JobState | "warn", string> = {
  ok: "bg-up",
  waiting: "bg-mute",
  off: "bg-line",
  error: "bg-down",
  overdue: "bg-yellow-400",
  warn: "bg-yellow-400",
};

function Dot({ state }: { state: JobState | "warn" }) {
  return <span className={clsx("mt-1 h-2 w-2 shrink-0 rounded-full", DOT[state])} />;
}

function Row({ state, title, children }: { state: JobState | "warn"; title: string; children?: ReactNode }) {
  return (
    <div className="flex gap-2 py-1">
      <Dot state={state} />
      <div className="min-w-0 flex-1">
        <div className="text-[12px] text-ink">{title}</div>
        {children && <div className="text-[11px] leading-snug text-mute">{children}</div>}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-0.5">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-mute">{title}</div>
      {children}
    </div>
  );
}

function Body({ s }: { s: AppStatus }) {
  const now = s.time;
  const llm = s.llm;
  const llmState: JobState | "warn" = !llm.configured ? "off" : llm.problem || llm.paused ? "error" : llm.last_error_at && (!llm.last_ok_at || llm.last_error_at > llm.last_ok_at) ? "warn" : "ok";
  return (
    <div className="space-y-4 px-3 py-2">
      <Section title="AI model">
        <Row state={llmState} title={llm.configured ? `${llm.provider} · ${llm.model}` : "None: the built-in writer answers"}>
          {llm.problem && <div className="text-down">{llm.problem}</div>}
          {llm.paused && <div className="text-down">Not answering; retried 30 s after the last failure.</div>}
          <div>
            Last answer {ago(llm.last_ok_at, now)}
            {llm.last_error && ` · last failure ${ago(llm.last_error_at, now)}: ${llm.last_error}`}
          </div>
        </Row>
      </Section>
      <Section title="Market data">
        <Row
          state={s.market.data_source === "synthetic" ? "warn" : s.market.binance_reachable ? "ok" : "error"}
          title={s.market.data_source === "synthetic" ? "Demo data (DATA_SOURCE=synthetic)" : s.market.binance_reachable ? "Binance answering" : "Binance unreachable"}
        >
          {!s.market.binance_reachable && s.market.data_source !== "synthetic" && "Charts fall back to demo candles until it answers again."}
        </Row>
        <Row state={s.market.liquidation_stream ? "ok" : "warn"} title={s.market.liquidation_stream ? "Liquidation stream connected" : "Liquidation stream not connected"} />
      </Section>
      <Section title="Binance key">
        <Row
          state={!s.binance_key.configured ? "off" : s.binance_key.ok === false ? "error" : s.binance_key.ok ? "ok" : "waiting"}
          title={s.binance_key.configured ? `Read-only key ${s.binance_key.masked ?? ""}` : "No key (Account tab)"}
        >
          {s.binance_key.problems.length > 0 && <div className="text-down">This key {s.binance_key.problems.join("; ")}.</div>}
          {s.binance_key.error && <div className="text-down">{s.binance_key.error}</div>}
          {s.binance_key.checked_at && <div>Permissions checked {ago(s.binance_key.checked_at, now)}</div>}
        </Row>
      </Section>
      <Section title="Notifications">
        <Row state={s.channels.discord ? "ok" : "off"} title={`Discord ${s.channels.discord ? "connected" : "not set (DISCORD_WEBHOOK_URL)"}`} />
        <Row state={s.channels.telegram ? "ok" : "off"} title={`Telegram ${s.channels.telegram ? "connected" : "not set (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)"}`} />
      </Section>
      <Section title="Background jobs">
        {s.jobs.length === 0 && <p className="text-[11px] text-mute">No jobs have started yet.</p>}
        {s.jobs.map((j) => (
          <Row key={j.name} state={j.state} title={j.label}>
            <div>
              {j.state === "off"
                ? "Off"
                : `Runs every ${every(j.every_seconds)} · last ran ${ago(j.last_ok, now)}${j.runs ? ` · ${j.runs} run${j.runs === 1 ? "" : "s"}` : ""}`}
              {j.state === "overdue" && " · overdue: it may have stopped"}
            </div>
            {j.detail && <div>{j.detail}</div>}
            {j.last_error && (
              <div className={j.state === "error" ? "text-down" : undefined}>
                Last error {ago(j.last_fail, now)}: {j.last_error}
              </div>
            )}
          </Row>
        ))}
      </Section>
      <p className="text-[10px] text-mute">Database: {s.database.memory ? "in memory (not kept)" : s.database.path}</p>
    </div>
  );
}

/** What is running and what is broken: the AI model, market data, the Binance key, notifications, background jobs. */
export default function StatusPanel() {
  const { data, error, refresh } = useAppStatus(REFRESH_MS);
  return (
    <div className="flex h-full min-h-0 flex-col text-xs">
      <div className="flex items-center gap-2 border-b border-line px-3 py-1.5 text-[11px] text-mute">
        <span>Refreshes every 30 s</span>
        <div className="flex-1" />
        <button type="button" onClick={() => void refresh()} className="btn-ghost h-6 w-6 p-0" title="Refresh now">
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {error && <div className="border-b border-line bg-down/10 px-3 py-2 text-[11px] text-down">Backend not answering: {error}</div>}
        {!data && !error && <Loader2 className="mx-auto mt-4 h-4 w-4 animate-spin text-mute" />}
        {data && <Body s={data} />}
      </div>
    </div>
  );
}
