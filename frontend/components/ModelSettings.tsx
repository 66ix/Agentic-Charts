"use client";

import clsx from "clsx";
import { Check, FlaskConical, Loader2, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { apiRequest } from "@/lib/api";

import { Section } from "./Dialog";

type Provider = "ollama" | "openai" | "anthropic" | "google";

interface OllamaModel {
  model: string;
  size_gb: number;
  params?: string | null;
  quant?: string | null;
  gpu_share?: number | null;
}

interface ModelsInfo {
  provider: Provider | "none";
  model: string;
  custom: boolean;
  default: { provider: string; model: string };
  providers: Record<Provider, boolean>;
  /** Who plans requests: the rule parser when it is sure, or the model every time. */
  router?: "rules_first" | "llm_first";
  router_default?: string;
  ollama: {
    installed: OllamaModel[];
    error: string | null;
    suggested: { model: string; size_gb: number; note: string; installed: boolean }[];
  };
}

interface EvalRun {
  id: string;
  provider: Provider;
  model: string;
  status: "running" | "done" | "failed";
  total: number;
  done: number;
  passed: number;
  avg_seconds: number | null;
  gpu_share: number | null;
  error: string | null;
}

const PROVIDER_LABEL: Record<Provider, string> = {
  ollama: "Ollama (local)",
  openai: "OpenAI-compatible",
  anthropic: "Anthropic",
  google: "Google AI Studio (Gemini)",
};

function gpuText(share: number | null | undefined) {
  if (share == null) return null;
  if (share >= 0.99) return { text: "all on GPU", good: true };
  return { text: `${Math.round(share * 100)}% on GPU, rest on CPU (slow)`, good: false };
}

/** Settings → AI model: which model answers the chart agent, and how models score on the prompt eval set. */
export default function ModelSettings() {
  const [info, setInfo] = useState<ModelsInfo | null>(null);
  const [runs, setRuns] = useState<EvalRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [provider, setProvider] = useState<Provider>("ollama");
  const [model, setModel] = useState("");

  const load = useCallback(async () => {
    try {
      const [m, r] = await Promise.all([
        apiRequest<ModelsInfo>("/api/llm/models"),
        apiRequest<{ runs: EvalRun[] }>("/api/llm/evals"),
      ]);
      setInfo(m);
      setRuns(r.runs);
      setError(null);
      return m;
    } catch (err) {
      setError((err as Error).message);
      return null;
    }
  }, []);

  useEffect(() => {
    void load().then((m) => {
      if (!m) return;
      if (m.provider !== "none") setProvider(m.provider);
      setModel(m.model);
    });
  }, [load]);

  // While an eval runs, follow its progress.
  const running = runs.find((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => void load(), 1500);
    return () => clearInterval(id);
  }, [running, load]);

  const setRouter = async (router: "rules_first" | "llm_first") => {
    try {
      await apiRequest("/api/llm/model", { method: "PUT", body: JSON.stringify({ router }) });
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const choose = async (body: { provider: Provider | null; model?: string }) => {
    try {
      await apiRequest("/api/llm/model", { method: "PUT", body: JSON.stringify(body) });
      const m = await load();
      if (m && m.provider !== "none") {
        setProvider(m.provider);
        setModel(m.model);
      }
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const runEval = async () => {
    try {
      await apiRequest("/api/llm/evals", { method: "POST", body: JSON.stringify({ provider, model }) });
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const isCurrent = info?.provider === provider && info?.model === model;
  const installed = info?.ollama.installed ?? [];
  const best = runs.filter((r) => r.status === "done").reduce<EvalRun | null>((b, r) => (!b || r.passed / r.total > b.passed / b.total ? r : b), null);

  return (
    <Section title="AI model">
      <p className="pb-2 text-[11px] text-mute">
        The model that reads your questions and writes the answers. Prices always come from the detectors, so a model
        only changes how well requests are understood and explained.
      </p>
      {info && (
        <p className="pb-2 text-[12px] text-ink">
          Now: <span className="font-mono">{info.provider === "none" ? "none (rule parser)" : `${info.provider} · ${info.model}`}</span>
          {info.custom && (
            <button type="button" className="btn-ghost ml-2 h-6 gap-1 px-1.5 text-[11px]" onClick={() => void choose({ provider: null })} title={`Back to ${info.default.provider} · ${info.default.model} from the backend's .env`}>
              <RotateCcw className="h-3 w-3" /> Use the default
            </button>
          )}
        </p>
      )}

      {info?.router && (
        <label className="flex items-center gap-2 pb-2 text-[12px] text-ink">
          Planning
          <select
            value={info.router}
            onChange={(e) => void setRouter(e.target.value as "rules_first" | "llm_first")}
            className="h-7 rounded border border-line bg-base px-1.5 text-[12px] text-ink outline-none focus:border-accent"
            aria-label="Planning"
            title="Rules first: clear requests ('give me a long setup', 'best spot buys') are planned instantly without the model; the model plans only what the rules are unsure of"
          >
            <option value="rules_first">Rules first, model when unsure (fastest)</option>
            <option value="llm_first">Model plans every request</option>
          </select>
        </label>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <select
          value={provider}
          onChange={(e) => {
            const p = e.target.value as Provider;
            setProvider(p);
            setModel(p === "ollama" ? (installed[0]?.model ?? "") : p === info?.provider ? info.model : p === "google" ? "gemini-2.5-flash" : "");
          }}
          className="h-7 rounded border border-line bg-base px-1.5 text-[12px] text-ink outline-none focus:border-accent"
          aria-label="Provider"
        >
          {(Object.keys(PROVIDER_LABEL) as Provider[]).map((p) => (
            <option key={p} value={p} disabled={info ? !info.providers[p] : false}>
              {PROVIDER_LABEL[p]}
              {info && !info.providers[p] ? (p === "google" ? " (needs GOOGLE_API_KEY in .env)" : " (needs a key in .env)") : ""}
            </option>
          ))}
        </select>
        {provider === "ollama" && installed.length > 0 ? (
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="h-7 min-w-0 flex-1 rounded border border-line bg-base px-1.5 text-[12px] text-ink outline-none focus:border-accent"
            aria-label="Model"
          >
            {!installed.some((m) => m.model === model) && model && <option value={model}>{model}</option>}
            {installed.map((m) => (
              <option key={m.model} value={m.model}>
                {m.model} · {m.size_gb} GB{m.quant ? ` · ${m.quant}` : ""}
              </option>
            ))}
          </select>
        ) : (
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="model name"
            className="h-7 min-w-0 flex-1 rounded border border-line bg-base px-2 text-[12px] text-ink outline-none focus:border-accent"
            aria-label="Model"
          />
        )}
        <button
          type="button"
          disabled={!model.trim() || isCurrent}
          className="btn-ghost h-7 gap-1 border border-line px-2 text-[12px] disabled:opacity-50"
          onClick={() => void choose({ provider, model: model.trim() })}
        >
          <Check className="h-3.5 w-3.5" /> {isCurrent ? "In use" : "Use"}
        </button>
        <button
          type="button"
          disabled={!model.trim() || !!running}
          className="btn-ghost h-7 gap-1 border border-line px-2 text-[12px] disabled:opacity-50"
          title="Run the app's prompt test set against this model (doesn't change the model in use)"
          onClick={() => void runEval()}
        >
          <FlaskConical className="h-3.5 w-3.5" /> Test it
        </button>
      </div>

      {provider === "ollama" && info?.ollama.error && <p className="pt-1.5 text-[11px] text-yellow-300">{info.ollama.error}</p>}
      {error && <p className="pt-1.5 text-[11px] text-down">{error}</p>}

      {runs.length > 0 && (
        <div className="mt-3 overflow-hidden rounded-md border border-line text-[11px]">
          <div className="grid grid-cols-[1fr_auto_auto] gap-x-3 border-b border-line bg-panel2 px-2 py-1 text-mute">
            <span>Model</span>
            <span>Right</span>
            <span>Per answer</span>
          </div>
          {runs.slice(0, 8).map((r) => {
            const gpu = gpuText(r.gpu_share);
            return (
              <div key={r.id} className="grid grid-cols-[1fr_auto_auto] items-center gap-x-3 border-b border-line px-2 py-1 last:border-b-0">
                <span className="min-w-0">
                  <span className={clsx("font-mono text-ink", r === best && "text-accent")}>{r.model}</span>
                  {r === best && runs.filter((x) => x.status === "done").length > 1 && <span className="ml-1 text-accent">best</span>}
                  {gpu && <span className={clsx("block text-[10px]", gpu.good ? "text-mute" : "text-yellow-300")}>{gpu.text}</span>}
                  {r.error && <span className="block text-[10px] text-down">{r.error}</span>}
                </span>
                <span className="font-mono">
                  {r.status === "running" ? (
                    <span className="inline-flex items-center gap-1 text-mute">
                      <Loader2 className="h-3 w-3 animate-spin" /> {r.done}/{r.total}
                    </span>
                  ) : (
                    <span className={r.passed === r.total ? "text-up" : "text-ink"}>
                      {r.passed}/{r.total}
                    </span>
                  )}
                </span>
                <span className="font-mono text-mute">{r.avg_seconds != null ? `${r.avg_seconds.toFixed(1)}s` : ""}</span>
              </div>
            );
          })}
        </div>
      )}

      {provider === "ollama" && info && (
        <div className="mt-3 text-[11px] text-mute">
          <p className="pb-1">Worth trying on an 8 GB card (install with <span className="font-mono text-ink">ollama pull &lt;name&gt;</span>, then test it here):</p>
          <ul className="space-y-0.5">
            {info.ollama.suggested.map((m) => (
              <li key={m.model}>
                <span className="font-mono text-ink">{m.model}</span> · {m.size_gb} GB · {m.note}
                {m.installed && <span className="text-up"> · installed</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Section>
  );
}
