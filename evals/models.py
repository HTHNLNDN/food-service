"""Model candidates for the cost x quality A/B (`evals/run.py --compare`).

Edit this list; API keys come from env. Supersedes the old (broken) scripts/benchmark_agents.py.
Prices are ESTIMATES ($/1M tokens, $/1k grounding queries) — correct them for a real $ compare.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    label: str
    base_url: str          # OpenAI-compatible base; a grounded Gemini derives its native base from this
    model: str
    api_key: str
    grounded: bool = False
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    search_per_ktok: float = 0.0


def candidates() -> list[Candidate]:
    """Configured candidates, skipping any whose key is missing."""
    out: list[Candidate] = []
    gemini_base, gemini_key = os.environ.get("LLM_BASE_URL", ""), os.environ.get("LLM_API_KEY", "")
    if gemini_base and gemini_key:
        out.append(Candidate(
            "gemini-3.6-flash (grounded)", gemini_base, "gemini-3.6-flash", gemini_key,
            grounded=True, input_per_mtok=0.30, output_per_mtok=2.50, search_per_ktok=14.0))
        # Grounding ablation: same model, Google Search off — does grounding earn its latency/$?
        out.append(Candidate(
            "gemini-3.6-flash (no grounding)", gemini_base, "gemini-3.6-flash", gemini_key,
            grounded=False, input_per_mtok=0.30, output_per_mtok=2.50))

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        out.append(Candidate(
            "claude-haiku-4-5", "https://api.anthropic.com/v1", "claude-haiku-4-5", anthropic_key,
            grounded=False, input_per_mtok=1.0, output_per_mtok=5.0))

    # Add e.g. a Chinese model or local endpoint here:
    # if os.environ.get("DEEPSEEK_API_KEY"):
    #     out.append(Candidate("deepseek-chat", "https://api.deepseek.com/v1", "deepseek-chat",
    #                          os.environ["DEEPSEEK_API_KEY"], input_per_mtok=0.28, output_per_mtok=0.42))
    return out
