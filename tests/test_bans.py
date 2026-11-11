import pytest

from app.bans import (
    ALLERGY_CHIPS,
    BAN_CATEGORIES,
    DIET_CHIPS,
    DISLIKES,
    KNOWN_CATEGORY_IDS,
    ban_display,
    banned_hits,
    violates,
)
from app.nutrition import Ingredient


def test_fish_ban_hits_fish_ingredients_only():
    assert "salmon" in banned_hits(["salmon fillet", "broccoli"], ["fish"])
    assert banned_hits(["broccoli", "rice"], ["fish"]) == set()


def test_word_boundary_avoids_coconut_and_butternut_under_nuts():
    # 'coconut' / 'butternut squash' contain 'nut' but are not nuts — must not trip a nuts ban
    assert banned_hits(["coconut", "butternut squash"], ["nuts"]) == set()
    assert banned_hits(["peanut butter", "chopped almonds"], ["nuts"])  # real nuts caught


def test_plant_based_milk_and_butter_are_not_dairy():
    # regression: the eval gate caught coconut milk / peanut butter tripping the dairy/vegan ban
    assert banned_hits(["coconut milk"], ["dairy"]) == set()
    assert banned_hits(["almond milk", "peanut butter", "vegan cheese"], ["vegan"]) == set()
    assert banned_hits(["coconut cream", "oat milk"], ["dairy"]) == set()


def test_real_dairy_still_caught():
    assert "cheese" in banned_hits(["cheddar cheese"], ["dairy"])
    assert banned_hits(["whole milk", "butter"], ["vegan"])  # true dairy still trips vegan


def test_plant_qualifier_does_not_disable_nut_or_soy_bans():
    assert banned_hits(["peanut butter"], ["nuts"])  # peanut still trips the nuts ban
    assert banned_hits(["soy milk"], ["soy"])         # soy still trips the soy ban


def test_all_meat_category_catches_common_meats():
    assert banned_hits(["chicken breast"], ["meat"])
    assert banned_hits(["ground pork"], ["meat"])
    assert banned_hits(["beef mince"], ["meat"])


def test_custom_item_ban():
    assert banned_hits(["sliced mushroom", "onion"], ["mushroom"]) == {"mushroom"}
    assert banned_hits(["onion", "carrot"], ["mushroom"]) == set()


def test_no_bans_means_no_hits():
    assert banned_hits(["salmon", "chicken", "milk"], []) == set()


def test_ban_display_expands_categories_and_keeps_custom():
    assert ban_display(["fish", "dairy", "mushroom"]) == ["Fish", "Dairy & lactose", "mushroom"]


def test_violates_over_ingredient_objects():
    assert violates([Ingredient("salmon", 200), Ingredient("rice", 100)], ["fish"])
    assert not violates([Ingredient("chicken breast", 200)], ["fish"])


def test_known_category_ids_cover_the_core_set():
    assert {"fish", "shellfish", "pork", "beef", "poultry", "meat", "dairy", "egg",
            "gluten", "nuts", "soy"} <= KNOWN_CATEGORY_IDS


def test_onboarding_chip_groups_cover_exactly_the_13_categories():
    assert sorted(DIET_CHIPS + ALLERGY_CHIPS) == sorted(cid for cid, _, _ in BAN_CATEGORIES)


@pytest.mark.parametrize(("dislike", "hit", "miss"), [
    ("anchovies", "anchovy fillets", "sardines"),
    ("olives", "pitted kalamata olives", "olive oil"),
    ("blue cheese", "crumbled gorgonzola", "cheddar cheese"),
    ("coriander", "fresh coriander", "ground coriander"),
    ("mushrooms", "sliced mushroom", "button squash"),
    ("oysters", "fresh oysters", "oyster sauce"),
    ("squid", "calamari rings", "shrimp"),
])
def test_dislikes_match_their_variants_but_not_lookalikes(dislike, hit, miss):
    assert banned_hits([hit], [dislike])
    assert not banned_hits([miss], [dislike])


def test_dislikes_display_by_name():
    assert ban_display(["blue cheese", "fish", "kale"]) == ["Blue cheese", "Fish", "kale"]
    assert len(DISLIKES) == 9
