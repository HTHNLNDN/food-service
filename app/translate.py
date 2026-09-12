"""Display-only translation: recipe titles, steps, and ingredient names, translated once at
persist time into the user's configured display language.

English stays the lookup key for USDA resolution and offer matching everywhere else in the
app — this module only produces text to print/render. A cheap model is fine here; a bad or
missing translation just falls back to English, it never blocks a plan.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx

from app import telemetry
from app.llm import chat_completion, extract_json_object


@dataclass(frozen=True)
class Translated:
    title: str
    steps: list[str]              # same length/order as the input steps
    ingredient_names: list[str]   # same length/order as the input ingredient_names


class Translator(Protocol):
    def translate(
        self, title: str, steps: list[str], ingredient_names: list[str], language: str
    ) -> Translated | None:
        """Return the translation, or None if it couldn't be produced (caller keeps English)."""
        ...


class LLMTranslator:
    """Translates via any OpenAI-compatible chat model, in one batched call per recipe."""

    _SYSTEM = (
        "You translate a recipe for display in a home-cooking app. Translate the title, each "
        "step, and each ingredient name into the target language the way a native speaker would "
        "naturally write it — not a literal word-for-word translation. Keep exactly the same "
        "number of steps and ingredient names, in the same order, matching the input 1:1. Reply "
        'with ONLY a JSON object: {"title": str, "steps": [str, ...], "ingredients": [str, ...]}.'
    )

    def __init__(self, base_url: str, model: str, api_key: str, client: httpx.Client | None = None):
        self.model = model
        self._client = client or httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    def translate(self, title, steps, ingredient_names, language) -> Translated | None:
        prompt = (
            f"Target language: {language}\n"
            f"Title: {title}\n"
            "Ingredients:\n" + "\n".join(f"- {n}" for n in ingredient_names) + "\n"
            "Steps:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps)) + "\n\nJSON:"
        )
        messages = [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": prompt},
        ]
        with telemetry.record("translate", "openai-compat", self.model):
            content = chat_completion(self._client, self.model, messages)
        return _parse(content, steps, ingredient_names)

    _UI_SYSTEM = (
        "You translate short UI copy (navigation links, headings, button labels) for a home-"
        "cooking app, the way a native speaker would naturally write it. Keep exactly the same "
        "number of lines, in the same order, matching the input 1:1. Reply with ONLY a JSON "
        'object: {"strings": [str, ...]}.'
    )

    def translate_batch(self, texts: list[str], language: str) -> list[str] | None:
        """Translate a flat list of short, standalone UI strings in one call. Used to warm the
        ui_translations cache (app/i18n.py) once per configured display language."""
        prompt = (
            f"Target language: {language}\n"
            + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
            + "\n\nJSON:"
        )
        messages = [
            {"role": "system", "content": self._UI_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        with telemetry.record("translate_ui", "openai-compat", self.model):
            content = chat_completion(self._client, self.model, messages)
        obj = extract_json_object(content)
        items = obj.get("strings")
        if not isinstance(items, list) or len(items) != len(texts):
            return None
        return [str(s) for s in items]


def _parse(content: str, steps: list[str], ingredient_names: list[str]) -> Translated | None:
    obj = extract_json_object(content)
    title, t_steps, t_ings = obj.get("title"), obj.get("steps"), obj.get("ingredients")
    if (
        not isinstance(title, str)
        or not isinstance(t_steps, list) or len(t_steps) != len(steps)
        or not isinstance(t_ings, list) or len(t_ings) != len(ingredient_names)
    ):
        return None  # malformed or a count mismatch — caller falls back to English, no retry
    return Translated(title, [str(s) for s in t_steps], [str(n) for n in t_ings])
