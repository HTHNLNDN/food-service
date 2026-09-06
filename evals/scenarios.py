"""Golden scenarios for the offline eval (evals/run.py). Versioned fixtures with PINNED offers
so runs are comparable. Keep it small — 10-30 cases is a legitimate PR gate at this scale."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    name: str
    max_kcal: int | None = 600
    min_protein_g: int | None = 40
    restrictions: str = "PCOS"
    servings: int = 2
    stores: tuple[str, ...] = ("rema",)
    bans: tuple[str, ...] = ()
    offers: tuple[str, ...] = ()        # pinned offer names (fed as a fixed OffersProvider)
    use_up: str = ""
    seed_history: tuple[str, ...] = ()  # recent dish titles to pre-seed (exercises the cooldown)
    num_dinners: int = 5


_OFFERS = ("Kyllingebryst", "Broccoli", "Laks", "Æg", "Svinemørbrad", "Squash", "Løg", "Gulerødder")

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("baseline"),
    Scenario("tight_macros", max_kcal=500, min_protein_g=45),
    Scenario("no_fish", bans=("fish", "shellfish")),
    Scenario("vegan", bans=("vegan",), restrictions="vegan, high-protein"),
    Scenario("gluten_free", bans=("gluten",)),
    Scenario("dairy_free", bans=("dairy",)),
    Scenario("use_up_chicken", use_up="3 kg chicken breast, freezer broccoli"),
    Scenario("with_offers", offers=_OFFERS),
    Scenario("crowded_cooldown", seed_history=(
        "Chicken and Broccoli Stir-Fry", "Thai Basil Chicken", "Creamy Tuscan Chicken",
        "Pork Tenderloin Tray Bake", "Beef and Bean Chili", "Turkey Meatballs",
        "Egg Fried Rice", "Lentil Dahl", "Chicken Fajita Bowl", "Pork and Cabbage Stir-Fry")),
    Scenario("three_dinners", num_dinners=3),
)
