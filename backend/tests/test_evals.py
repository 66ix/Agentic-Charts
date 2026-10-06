"""Every prompt in evals/intents.jsonl must parse to the expected plan with the rule parser."""

import pytest

from evals.run import load_cases, run_rules


@pytest.mark.parametrize("case,errors", run_rules(), ids=lambda v: v["prompt"][:50] if isinstance(v, dict) else "")
def test_prompt(case, errors):
    assert not errors, f"{case['prompt']}: {'; '.join(errors)}"


def test_cases_cover_every_ability():
    keys = {k for c in load_cases() for k in c["expect"]}
    assert {"symbol", "switch_chart", "scan_watchlist", "trade_plan", "indicators_on", "remove",
            "alert_targets", "custom_levels_count"} <= keys
