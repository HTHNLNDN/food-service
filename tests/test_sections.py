from app.sections import section_for


def test_fresh_produce():
    assert section_for("broccoli") == "produce"
    assert section_for("bell pepper") == "produce"
    assert section_for("fresh basil") == "produce"
    assert section_for("cherry tomatoes") == "produce"  # plural


def test_meat_and_fish():
    assert section_for("chicken breast") == "meat_fish"
    assert section_for("lean ground beef 5% fat") == "meat_fish"
    assert section_for("salmon fillet") == "meat_fish"
    assert section_for("shrimp") == "meat_fish"


def test_dairy_and_eggs():
    assert section_for("butter") == "dairy_eggs"
    assert section_for("greek yogurt") == "dairy_eggs"
    assert section_for("eggs") == "dairy_eggs"  # plural


def test_bakery():
    assert section_for("whole wheat pita bread") == "bakery"


def test_pantry_dry_goods():
    assert section_for("brown rice") == "pantry"
    assert section_for("black pepper") == "pantry"
    assert section_for("soy sauce") == "pantry"
    assert section_for("canned black beans") == "pantry"


def test_frozen_qualifier_overrides_base_ingredient():
    # "peas" alone would suggest produce, but "frozen" must win
    assert section_for("frozen green peas") == "frozen"


def test_canned_qualifier_overrides_base_ingredient():
    # "tomatoes" alone would suggest produce, but "canned" must win
    assert section_for("canned diced tomatoes") == "pantry"
    assert section_for("canned chickpeas") == "pantry"


def test_processed_product_qualifiers_override_base_ingredient():
    # "garlic"/"onion"/"tomato"/"avocado"/"apple" would suggest produce; "chicken" meat_fish —
    # but powder/paste/oil/vinegar/broth/stock qualifiers mean it's a shelf-stable pantry item.
    assert section_for("garlic powder") == "pantry"
    assert section_for("onion powder") == "pantry"
    assert section_for("tomato paste") == "pantry"
    assert section_for("tomato passata") == "pantry"
    assert section_for("avocado oil") == "pantry"
    assert section_for("apple cider vinegar") == "pantry"
    assert section_for("chicken broth") == "pantry"
    assert section_for("chicken stock") == "pantry"
    assert section_for("fish sauce") == "pantry"


def test_plant_based_milk_is_not_dairy():
    # mirrors app/bans.py's identical plant-qualifier problem for the same reason
    assert section_for("light coconut milk") == "pantry"
    assert section_for("almond milk") == "pantry"


def test_dried_herb_qualifier_overrides_to_pantry():
    assert section_for("dried oregano") == "pantry"
    assert section_for("dried thyme") == "pantry"
    assert section_for("dried basil") == "pantry"  # not literally in real data, but must still work


def test_beverages():
    assert section_for("water") == "beverages"


def test_unknown_ingredient_falls_back_to_other():
    assert section_for("gefilte fish croquette surprise") == "other"


def test_real_ingredient_sample_has_no_other_fallback():
    # a representative sample pulled from this app's actual generated-recipe history —
    # every one of these must land in a real section, not "other"
    sample = [
        "Dijon mustard", "apple", "apple cider vinegar", "asparagus", "avocado",
        "avocado oil", "beef broth", "beef sirloin", "bell pepper", "black pepper",
        "brown rice", "brussels sprouts", "butter", "button mushrooms", "canned black beans",
        "canned chopped tomatoes", "capers", "caraway seeds", "carrots", "cauliflower rice",
        "cherry tomatoes", "chicken breast", "chicken stock", "chili powder", "cornstarch",
        "cottage cheese", "cremini mushrooms", "cucumber", "dried oregano", "edamame",
        "egg", "eggplant", "feta cheese", "fish sauce", "fresh basil", "fresh ginger",
        "frozen green peas", "garam masala", "garlic", "garlic powder", "ginger",
        "grana padano cheese", "greek yogurt", "green bell pepper", "green onions",
        "ground beef", "haddock fillet", "honey", "hummus", "iceberg lettuce",
        "jalapeño pepper", "jasmine rice", "kalamata olives", "kimchi", "lemon juice",
        "lemongrass", "light coconut milk", "lime juice", "mirin", "mozzarella cheese",
        "olive oil", "onion powder", "oyster sauce", "paprika", "parmesan cheese", "pesto",
        "potatoes", "quinoa", "red chili pepper", "red pepper flakes", "red wine vinegar",
        "romaine lettuce", "salmon fillet", "salt", "salsa", "sesame oil", "sesame seeds",
        "shallot", "shrimp", "skyr", "smoked paprika", "snow peas", "soy sauce", "spinach",
        "sriracha sauce", "sugar snap peas", "sun-dried tomatoes", "sweet corn",
        "sweet potato", "taco seasoning", "thai basil", "tomato passata", "tomato paste",
        "trout fillet", "turkey breast", "turmeric powder", "unsalted butter", "veal steak",
        "vegetable broth", "vegetable oil", "water", "whole wheat pasta",
        "worcestershire sauce", "zucchini",
    ]
    assert all(section_for(n) != "other" for n in sample), (
        [n for n in sample if section_for(n) == "other"]
    )
