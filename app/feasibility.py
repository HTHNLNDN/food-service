"""Safe stops for calorie/protein targets.

Deterministic and LLM-free: given per-portion targets, say whether real dinners can meet
them, explain why not in the mascot's voice, and suggest gentler targets. Protein is ~4 kcal
per gram, so the protein share of a portion is 4 x protein / kcal.
"""

from dataclasses import dataclass

FINE, AMBITIOUS, UNREALISTIC, IMPOSSIBLE = "fine", "ambitious", "unrealistic", "impossible"

KCAL_FLOOR = 300              # hard: no dinner target below this, ever
KCAL_LIGHT = 360              # below: a very light dinner (unrealistic)
FLOOR_SUGGESTION = 400        # what the floor message offers instead
PROTEIN_REALISTIC_MAX = 70    # g per portion (~300 g chicken breast)
PROTEIN_HARD_MAX = 120
KCAL_PER_G_PROTEIN = 4
# protein-share edges, in whole percent (see PCT_* below): 75 = leaner than plain chicken
# breast; 35 = top of the AMDR range (10-35%); alternatives aim at a comfortable 30


@dataclass(frozen=True)
class Targets:
    max_kcal: int | None
    min_protein_g: int | None


@dataclass(frozen=True)
class Verdict:
    level: str
    reason: str = ""
    alternatives: tuple[Targets, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.level == IMPOSSIBLE

    @property
    def can_keep(self) -> bool:
        return self.level == UNREALISTIC


# Band edges as whole percentages: integer maths, so 35% / 40% / 75% compare exactly.
PCT_IMPOSSIBLE, PCT_UNREALISTIC, PCT_AMBITIOUS, PCT_SUGGEST = 75, 40, 35, 30


def _share_over(kcal: int, protein: int, pct: int) -> bool:
    """Is protein's share of the portion's energy strictly above `pct` percent?"""
    return KCAL_PER_G_PROTEIN * protein * 100 > pct * kcal


def _share_pct(kcal: int, protein: int) -> int:
    return round(KCAL_PER_G_PROTEIN * protein * 100 / kcal)


def _kcal_for(protein: int) -> int:
    """Calories that put `protein` at the comfortable share, rounded up to 50."""
    exact = KCAL_PER_G_PROTEIN * protein * 100  # kcal x PCT_SUGGEST
    return max(KCAL_LIGHT, -(-exact // (PCT_SUGGEST * 50)) * 50)  # ceil to 50


def _protein_for(kcal: int) -> int:
    """Protein at the comfortable share of `kcal`, rounded down to 5."""
    grams = PCT_SUGGEST * kcal // (KCAL_PER_G_PROTEIN * 100)
    return min(PROTEIN_REALISTIC_MAX, grams // 5 * 5)


def _alternatives(kcal: int | None, protein: int | None) -> tuple[Targets, ...]:
    """Keep-protein and keep-calories options, clamped and de-duplicated."""
    options = []
    if protein is not None:
        p = min(protein, PROTEIN_REALISTIC_MAX)
        options.append(Targets(_kcal_for(p), p))
    if kcal is not None:
        k = max(kcal, KCAL_LIGHT)
        options.append(Targets(k, _protein_for(k) if protein is not None else None))
    return tuple(dict.fromkeys(options))


def _floor_alternative(protein: int | None) -> Targets:
    if protein is not None and _share_over(FLOOR_SUGGESTION, protein, PCT_AMBITIOUS):
        protein = _protein_for(FLOOR_SUGGESTION)
    return Targets(FLOOR_SUGGESTION, protein)


def _chicken_grams(protein: int) -> int:
    return round(protein / 0.31 / 10) * 10  # chicken breast is ~31 g protein per 100 g


def check_targets(max_kcal: int | None, min_protein_g: int | None) -> Verdict:
    kcal, protein = max_kcal, min_protein_g

    # --- impossible: never saved, never planned ---
    # Protein alone exceeding the calories is the most telling reason, so it goes first.
    if kcal is not None and protein is not None and KCAL_PER_G_PROTEIN * protein > kcal:
        return Verdict(IMPOSSIBLE, (
            f"At {kcal} kcal, {protein} g of protein would be {KCAL_PER_G_PROTEIN * protein} kcal "
            "on its own, so no recipe can hit that."), _alternatives(kcal, protein))
    if kcal is not None and kcal < KCAL_FLOOR:
        return Verdict(IMPOSSIBLE, (
            f"I can't plan dinners under {KCAL_FLOOR} kcal. That's less than a main meal needs "
            f"to keep you going through the evening. How about starting at {FLOOR_SUGGESTION} "
            "kcal? We can always adjust later."), (_floor_alternative(protein),))
    if protein is not None and protein > PROTEIN_HARD_MAX:
        return Verdict(IMPOSSIBLE, (
            f"{protein} g of protein in one portion is more than any dinner can hold (about "
            f"{_chicken_grams(protein)} g of chicken breast each)."),
            _alternatives(kcal, protein))
    if kcal is not None and protein is not None:
        if _share_over(kcal, protein, PCT_IMPOSSIBLE):
            return Verdict(IMPOSSIBLE, (
                f"At {kcal} kcal, {protein} g of protein means about {_share_pct(kcal, protein)}% "
                "of the energy coming from protein. That's leaner than plain chicken breast, so "
                "no real recipe can hit it."), _alternatives(kcal, protein))
        # --- unrealistic: allowed, but only after an explicit choice ---
        if _share_over(kcal, protein, PCT_UNREALISTIC):
            return Verdict(UNREALISTIC, (
                f"That's a strong goal! At {kcal} kcal with {protein} g protein, though, most "
                "dinners would be plain lean meat and steamed greens: about "
                f"{_share_pct(kcal, protein)}% of the energy would have to come from protein."),
                _alternatives(kcal, protein))
    if kcal is not None and kcal < KCAL_LIGHT:
        return Verdict(UNREALISTIC, (
            f"{kcal} kcal is a very light dinner, and most real recipes need a bit more to be "
            "filling."), (_floor_alternative(protein),))
    if protein is not None and protein > PROTEIN_REALISTIC_MAX:
        return Verdict(UNREALISTIC, (
            f"{protein} g of protein per portion means very large servings (about "
            f"{_chicken_grams(protein)} g of chicken breast each)."), _alternatives(kcal, protein))
    # --- ambitious: saved, with a note ---
    if kcal is not None and protein is not None and _share_over(kcal, protein, PCT_AMBITIOUS):
        return Verdict(AMBITIOUS, (
            "Protein-heavy: recipes will lean on lean meat, fish and pulses."))
    return Verdict(FINE)
