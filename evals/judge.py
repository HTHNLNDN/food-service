"""LLM-as-judge scorer for the offline eval.

Scores the axes a deterministic check can't: appeal, realism, effort-fit. Pointwise 1-5,
structured, length-blind. Point it at a STRONGER / different-family model via JUDGE_* env for
less self-preference bias. Judge calls are logged (call_type 'judge') but not attributed to a
plan, so they don't pollute the app's $/dinner.
"""

import json
import re

import httpx

from app import telemetry
from app.llm import chat_completion

AXES = ("appeal", "realism", "effort")


class LLMJudge:
    _SYSTEM = (
        "You are a practical home-cooking critic scoring weeknight dinners. For EACH recipe, give "
        "integer scores 1-5 on three axes:\n"
        "- appeal: would a household actually enjoy eating this?\n"
        "- realism: are the steps coherent and would they truly produce the dish?\n"
        "- effort: does it fit a quick ~30-minute weeknight? (5 = fast & simple, 1 = an hour-long "
        "project)\n"
        "Judge on merit, NOT length — a short recipe is not worse. Reply with ONLY a JSON array, "
        'one object per recipe IN ORDER: [{"appeal":n,"realism":n,"effort":n}, ...].'
    )

    def __init__(self, base_url: str, model: str, api_key: str, client: httpx.Client | None = None):
        self.model = model
        self._client = client or httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    def score(self, recipes) -> list[dict]:
        """Return one {appeal, realism, effort} dict per recipe (values 1-5, or None if unparsable)."""
        if not recipes:
            return []
        listing = "\n\n".join(
            f"{i + 1}. {r.title}\n"
            "   ingredients: " + ", ".join(f"{ing.grams:.0f}g {ing.name}" for ing in r.ingredients) + "\n"
            "   steps: " + " ".join(r.steps)
            for i, r in enumerate(recipes)
        )
        messages = [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": f"Recipes:\n{listing}\n\nJSON array:"},
        ]
        with telemetry.record("judge", "openai-compat", self.model):
            content = chat_completion(self._client, self.model, messages)
        return _parse_scores(content, len(recipes))


def _parse_scores(content: str, n: int) -> list[dict]:
    arr = _extract_array(content)
    out = []
    for i in range(n):
        obj = arr[i] if i < len(arr) and isinstance(arr[i], dict) else {}
        out.append({axis: _clamp(obj.get(axis)) for axis in AXES})
    return out


def _extract_array(content: str) -> list:
    m = re.search(r"\[.*\]", content.strip(), re.DOTALL)
    if not m:
        return []
    try:
        val = json.loads(m.group())
    except json.JSONDecodeError:
        return []
    return val if isinstance(val, list) else []


def _clamp(v) -> int | None:
    try:
        return max(1, min(5, round(float(v))))
    except (TypeError, ValueError):
        return None
