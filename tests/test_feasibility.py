"""Safe stops for targets: every band edge, the alternatives, and the floor wording."""

import pytest

from app.feasibility import (
    AMBITIOUS,
    FINE,
    IMPOSSIBLE,
    UNREALISTIC,
    Targets,
    check_targets,
)


@pytest.mark.parametrize(("kcal", "protein", "level"), [
    # unset targets behave as today
    (None, None, FINE), (700, None, FINE), (None, 40, FINE),
    # the hard calorie floor: 300 is allowed, 299 is not
    (299, None, IMPOSSIBLE), (300, None, UNREALISTIC), (359, None, UNREALISTIC),
    (360, None, FINE), (250, 10, IMPOSSIBLE),
    # protein share bands: 35% / 40% / 75%
    (400, 35, FINE),          # 35% exactly: fine
    (400, 36, AMBITIOUS),     # 36%
    (400, 40, AMBITIOUS),     # 40% exactly: still ambitious
    (400, 41, UNREALISTIC),   # 41%
    (400, 50, UNREALISTIC),   # the worked example, 50%
    (400, 75, UNREALISTIC),   # 75% exactly: unrealistic, not impossible
    (400, 76, IMPOSSIBLE),    # 76%
    (100, 40, IMPOSSIBLE),    # protein alone exceeds the calories
    # protein ceilings: 70 (realistic) / 120 (hard)
    (2000, 70, FINE), (2000, 71, UNREALISTIC), (2000, 120, UNREALISTIC),
    (2000, 121, IMPOSSIBLE), (None, 121, IMPOSSIBLE), (None, 71, UNREALISTIC),
    # R2 goal presets and their combinations are all fine
    (700, 25, FINE), (550, 30, FINE), (750, 40, FINE), (550, 40, FINE), (700, 40, FINE),
])
def test_levels(kcal, protein, level):
    assert check_targets(kcal, protein).level == level


def test_worked_example_offers_keep_protein_and_keep_calories():
    v = check_targets(400, 50)
    assert set(v.alternatives) == {Targets(700, 50), Targets(400, 30)}
    assert v.can_keep and not v.blocked
    assert "400 kcal" in v.reason and "50 g" in v.reason


def test_impossible_explains_and_cannot_be_kept():
    v = check_targets(100, 40)
    assert v.blocked and not v.can_keep
    assert "160 kcal" in v.reason  # 40 g protein = 160 kcal on its own


def test_floor_suggests_400_and_never_mentions_disorders():
    v = check_targets(299, None)
    assert v.blocked
    assert "300 kcal" in v.reason and "400 kcal" in v.reason
    assert v.alternatives == (Targets(400, None),)


def test_floor_suggestion_keeps_a_sensible_protein():
    assert check_targets(250, 50).alternatives[0] == Targets(400, 30)  # 50 g would be 50%
    assert check_targets(250, 20).alternatives[0] == Targets(400, 20)  # 20%: keep it


def test_no_reason_ever_mentions_eating_disorders():
    for kcal in (None, 100, 250, 299, 300, 350, 400, 700, 2000):
        for protein in (None, 10, 40, 50, 71, 121):
            reason = check_targets(kcal, protein).reason.lower()
            assert not any(w in reason for w in ("anorex", "disorder", "eating disorder"))


def test_every_alternative_is_itself_acceptable():
    for kcal in range(100, 1600, 25):
        for protein in range(0, 135, 5):
            for alt in check_targets(kcal, protein).alternatives:
                assert check_targets(alt.max_kcal, alt.min_protein_g).level in (FINE, AMBITIOUS), (
                    kcal, protein, alt)


def test_fine_and_ambitious_have_no_alternatives():
    assert check_targets(700, 25).alternatives == ()
    v = check_targets(400, 38)
    assert v.level == AMBITIOUS and v.alternatives == () and v.reason
