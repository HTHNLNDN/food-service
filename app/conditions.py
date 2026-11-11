"""Curated conditions -> cooking pointers for the AI.

A condition is an explicit chip, never detected in free text. The AI only ever receives the
pointers; the condition's name stays on the server. Add conditions one at a time, each
researched against its guideline, with the source and the date it was reviewed.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Condition:
    id: str
    label: str
    pointers: tuple[str, ...]
    source: str
    reviewed: date


CONDITIONS: tuple[Condition, ...] = (
    Condition(
        "pmos", "PMOS (formerly PCOS)",
        ("lower-GI carbohydrates", "good protein and fibre", "unsaturated fats"),
        "International evidence-based guideline for the assessment and management of "
        "polycystic ovary syndrome, 2023 (no diet composition beats another; lower-GI carbs, "
        "adequate protein, fibre and unsaturated fats modestly help)",
        date(2026, 10, 18),
    ),
)
_BY_ID = {c.id: c for c in CONDITIONS}


def known(ids) -> list[str]:
    return [i for i in dict.fromkeys(ids) if i in _BY_ID]


def pointers_for(ids) -> list[str]:
    return [p for i in known(ids) for p in _BY_ID[i].pointers]
