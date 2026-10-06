"""Prompt → intent evaluation.

    python -m evals.run            # the rule parser (what CI checks)
    python -m evals.run --llm      # the configured LLM_PROVIDER, including llm_only cases

Each line of intents.jsonl is a prompt plus the fields its plan must have. A list expects those items to be
present (`"features": []` means none), `<field>_none` lists items that must be absent, `custom_levels_count`
checks how many explicit levels were parsed, and any other value must match exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.llm import ChartContext, LLMClient, rule_intent
from app.schemas import AnalysisIntent

CASES = Path(__file__).with_name("intents.jsonl")
CHART = ChartContext(symbol="INJUSDT", interval="4h", watchlist=["BTCUSDT", "ETHUSDT", "SOLUSDT", "INJUSDT"])


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


def check(intent: AnalysisIntent, expect: dict) -> list[str]:
    """Mismatches between an intent and a case's expectations (empty = pass)."""
    got = intent.model_dump()
    errors = []
    for key, want in expect.items():
        if key == "custom_levels_count":
            if len(intent.custom_levels) != want:
                errors.append(f"custom_levels: {len(intent.custom_levels)} != {want}")
        elif key.endswith("_none"):
            bad = set(want) & set(got[key[:-5]])
            if bad:
                errors.append(f"{key[:-5]} should not have {sorted(bad)}")
        elif isinstance(want, list):
            if want == [] and got[key]:
                errors.append(f"{key}: expected empty, got {got[key]}")
            missing = [w for w in want if w not in got[key]]
            if missing:
                errors.append(f"{key}: missing {missing} in {got[key]}")
        elif got[key] != want:
            errors.append(f"{key}: {got[key]!r} != {want!r}")
    return errors


def previous(case: dict) -> AnalysisIntent | None:
    return AnalysisIntent.model_validate(case["previous"]) if case.get("previous") else None


def run_rules() -> list[tuple[dict, list[str]]]:
    known = {s.removesuffix("USDT") for s in CHART.watchlist}
    return [(c, check(rule_intent(c["prompt"], previous(c), known, CHART.symbol), c["expect"]))
            for c in load_cases() if not c.get("llm_only")]


async def run_llm() -> list[tuple[dict, list[str]]]:
    llm = LLMClient()
    out = []
    try:
        for c in load_cases():
            intent, engine = await llm.parse_intent(c["prompt"], [], [], previous(c), CHART)
            errs = check(intent, c["expect"])
            if engine == "rules":
                errs.insert(0, "LLM unavailable: answered by the rule parser")
            out.append((c, errs))
    finally:
        await llm.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="evaluate the configured LLM provider")
    args = ap.parse_args()
    results = asyncio.run(run_llm()) if args.llm else run_rules()
    passed = sum(1 for _, e in results if not e)
    for case, errs in results:
        print(("PASS " if not errs else "FAIL ") + case["prompt"] + ("" if not errs else "\n     " + "; ".join(errs)))
    print(f"\n{passed}/{len(results)} passed")
    raise SystemExit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
