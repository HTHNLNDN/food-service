"""Goals -> targets and prompt preferences; the curated conditions table."""

import re
from itertools import combinations

import pytest

from app import conditions, goals
from app.feasibility import FINE, check_targets


@pytest.mark.parametrize(("ids", "expected"), [
    ([], (700, 25)),
    (["healthier"], (700, 25)),
    (["lose_gently"], (550, 30)),
    (["more_protein"], (750, 40)),
    (["save_money"], (700, 25)),
    (["more_plants", "save_money"], (700, 25)),
    (["lose_gently", "more_protein"], (550, 40)),   # lowest ceiling, highest floor
    (["healthier", "more_protein"], (700, 40)),
    (["healthier", "lose_gently", "save_money"], (550, 30)),
])
def test_goals_produce_the_r2_targets(ids, expected):
    assert goals.targets_for(ids) == expected


def test_every_combination_of_up_to_3_goals_is_fine_under_the_safe_stops():
    ids = [g.id for g in goals.GOALS]
    for n in range(goals.MAX_GOALS + 1):
        for combo in combinations(ids, n):
            assert check_targets(*goals.targets_for(combo)).level == FINE, combo


def test_only_save_money_and_more_plants_add_prompt_preferences():
    assert goals.prompt_notes(["healthier", "lose_gently", "more_protein"]) == []
    money, plants = goals.prompt_notes(["save_money", "more_plants"])
    assert "offer" in money
    assert "vegetarian" in plants and "without banning meat" in plants


def test_weight_loss_copy_has_no_numbers():
    lose = next(g for g in goals.GOALS if g.id == "lose_gently")
    assert lose.label == "Lose weight, gently"
    assert not re.search(r"\d", lose.label + lose.line)


def test_known_filters_unknown_ids_and_duplicates():
    assert goals.known(["more_protein", "nope", "more_protein"]) == ["more_protein"]
    assert conditions.known(["pmos", "pcos", "pmos"]) == ["pmos"]


def test_pmos_entry_has_its_label_and_pointers():
    (pmos,) = conditions.CONDITIONS
    assert pmos.id == "pmos" and pmos.label == "PMOS (formerly PCOS)"
    assert conditions.pointers_for(["pmos"]) == [
        "lower-GI carbohydrates", "good protein and fibre", "unsaturated fats"]
    for pointer in pmos.pointers:  # pointers never name the condition
        assert "pmos" not in pointer.lower() and "pcos" not in pointer.lower()
