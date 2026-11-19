"""Curated conditions -> cooking pointers for the AI.

A condition is an explicit chip, never detected in free text. The AI only ever receives the
pointers; the condition's name stays on the server. Add conditions one at a time, each
researched against its guideline; note the source and the review date above its entry.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Condition:
    id: str
    label: str
    pointers: tuple[str, ...]


CONDITIONS: tuple[Condition, ...] = (
    # Source: International evidence-based guideline for the assessment and management of
    # polycystic ovary syndrome, 2023: no diet composition beats another; lower-GI carbs,
    # adequate protein, fibre and unsaturated fats modestly help. Reviewed 2026-10-18.
    Condition("pmos", "PMOS (formerly PCOS)",
              ("lower-GI carbohydrates", "good protein and fibre", "unsaturated fats")),
)
_BY_ID = {c.id: c for c in CONDITIONS}


def known(ids) -> list[str]:
    return [i for i in dict.fromkeys(ids) if i in _BY_ID]


def pointers_for(ids) -> list[str]:
    return [p for i in known(ids) for p in _BY_ID[i].pointers]
