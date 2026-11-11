"""Onboarding goals and the targets and prompt preferences they produce.

Numbers are per standard dinner portion. A combination takes the lowest ceiling and the
highest floor; with no numeric goal, dinners default to "eat a bit healthier" (700/25).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Goal:
    id: str
    label: str
    line: str             # what I'll do differently; no numbers, so it isn't numbers-first
    kcal: int | None      # per-portion target, or None: this goal doesn't change the numbers
    protein: int | None
    prompt: str = ""      # a preference for the AI, or "" when the numbers say it all


GOALS: tuple[Goal, ...] = (
    Goal("healthier", "Eat a bit healthier", "Balanced plates, more veg and fibre.", 700, 25),
    Goal("lose_gently", "Lose weight, gently", "Lighter dinners that still fill you up.",
         550, 30),
    Goal("more_protein", "More protein", "Protein-forward dinners for training or staying full.",
         750, 40),
    Goal("save_money", "Save money", "I'll build the week around what's on offer at your stores.",
         None, None, "Keep costs down: build the dinners around this week's offers wherever "
         "they fit, and favour budget staples."),
    Goal("more_plants", "Eat more plants", "More vegetarian dinners, without banning meat.",
         None, None, "Make about half of the dinners vegetarian (beans, lentils, eggs, dairy "
         "or tofu), without banning meat."),
)
MAX_GOALS = 3
DEFAULT_TARGETS = (700, 25)  # "eat a bit healthier": the default, and what skipping gives
_BY_ID = {g.id: g for g in GOALS}


def known(ids) -> list[str]:
    return [i for i in dict.fromkeys(ids) if i in _BY_ID]


def targets_for(ids) -> tuple[int, int]:
    numeric = [_BY_ID[i] for i in known(ids) if _BY_ID[i].kcal is not None]
    if not numeric:
        return DEFAULT_TARGETS
    return min(g.kcal for g in numeric), max(g.protein for g in numeric)


def prompt_notes(ids) -> list[str]:
    return [_BY_ID[i].prompt for i in known(ids) if _BY_ID[i].prompt]
