from fastapi.testclient import TestClient

from app.agent import Recipe
from app.main import app
from app.nutrition import Ingredient, Macros
from app.services import Services

WITHIN = Macros(100, 20, 0, 0)


class FakeAgent:
    def __init__(self, recipes, replacement):
        self._recipes = recipes
        self._replacement = replacement

    def plan_week(self, ctx):
        return list(self._recipes)

    def replace(self, declined, ctx):
        return self._replacement


class FakeOffer:
    def __init__(self, slug, name, price):
        from decimal import Decimal
        self.chain_slug = slug
        self.name = name
        self.price = Decimal(price)


class FakeOffers:
    def __init__(self, offers):
        self._offers = offers

    def offers(self, selected):
        return list(self._offers)


class FakeNutrition:
    def lookup(self, name):
        return (1, WITHIN)


class FakeMatcher:
    def __init__(self, mapping=None):
        self.mapping = mapping or {}

    def match(self, ingredients, offers):
        return {n: self.mapping.get(n) for n in ingredients}


def _recipe(title, *ings):
    return Recipe(title, 4, [Ingredient(n, 400) for n in ings], ["Cook it."])


def _install_fakes(client, recipes, replacement, offers, matches=None):
    def build(config, conn):
        return Services(FakeAgent(recipes, replacement), FakeOffers(offers), FakeNutrition(),
                        FakeMatcher(matches))

    client.app.state.build_services = build
    client.app.state.plan_sync = True  # run planning inline so tests are deterministic


def _set_profile(client, language=""):
    data = {"max_kcal": "600", "min_protein_g": "5", "servings": "2", "stores": ["rema"]}
    if language:
        data["language"] = language
    client.post("/profile", data=data, follow_redirects=False)


def test_home_shows_plan_button():
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Plan this week" in resp.text


def test_more_page_links_to_profile_cost_and_my_data():
    with TestClient(app) as client:
        resp = client.get("/more")
    assert resp.status_code == 200
    assert 'href="/profile"' in resp.text
    assert 'href="/cost"' in resp.text
    assert 'href="/me"' in resp.text


def test_plan_this_week_generates_and_renders():
    with TestClient(app) as client:
        _install_fakes(client, [_recipe("Chicken bowl", "chicken breast"),
                                _recipe("Trout plate", "trout fillet")],
                       _recipe("Backup", "eggs"),
                       [FakeOffer("rema", "Kylling", 20)])
        _set_profile(client)
        posted = client.post("/plan", follow_redirects=False)
        assert posted.status_code in (302, 303)
        home = client.get("/")
    assert "Chicken bowl" in home.text
    assert "Trout plate" in home.text
    assert "kcal" in home.text  # macros rendered


def test_rating_from_plan_page_persists():
    from app.db import connect
    from app.profile import rating_history

    with TestClient(app) as client:
        _install_fakes(client, [_recipe("Chicken bowl", "chicken breast")],
                       _recipe("B", "eggs"), [FakeOffer("rema", "Kylling", 20)])
        _set_profile(client)
        client.post("/plan", follow_redirects=False)
        client.post("/plan/0/rate", data={"score": "1"}, follow_redirects=False)
        hist = rating_history(connect(client.app.state.config.db_path))
    assert len(hist) == 1
    assert hist[0].title == "Chicken bowl"
    assert hist[0].score == 1


def test_use_up_prefills_on_home_and_shows_already_have_in_shopping():
    with TestClient(app) as client:
        _install_fakes(client, [_recipe("Chicken bowl", "chicken breast")],
                       _recipe("B", "eggs"), [FakeOffer("rema", "Kylling", 20)])
        _set_profile(client)
        client.post("/plan", data={"use_up": "3 kg chicken breast"}, follow_redirects=False)
        home = client.get("/").text
        shop = client.get("/shopping").text
    assert 'value="3 kg chicken breast"' in home     # field pre-filled from the stored plan
    assert "Already have" in shop                      # use-up excluded from the buy list
    assert "chicken breast" in shop.lower()


def test_shopping_view_has_offer_and_non_offer_sections():
    with TestClient(app) as client:
        _install_fakes(
            client,
            [_recipe("Chicken dinner", "chicken breast", "rice")],
            _recipe("B", "eggs"),
            [FakeOffer("rema", "Kyllingebryst", 39)],
            matches={"chicken breast": 0},  # chicken is on offer; rice is not
        )
        _set_profile(client)
        client.post("/plan", follow_redirects=False)
        shop = client.get("/shopping").text
    assert "On offer" in shop and "Kyllingebryst" in shop   # matched offer section
    assert "Not on offer" in shop and "rice" in shop         # unmatched section
    assert 'class="tick"' in shop and "shopping-" in shop    # checkboxes + per-plan localStorage


def test_shopping_view_groups_items_by_section():
    with TestClient(app) as client:
        _install_fakes(
            client,
            [_recipe("Chicken dinner", "chicken breast", "broccoli")],
            _recipe("B", "eggs"),
            [FakeOffer("rema", "Kyllingebryst", 39), FakeOffer("rema", "Broccoli", 12)],
            matches={"chicken breast": 0, "broccoli": 1},  # both on offer, different sections
        )
        _set_profile(client)
        client.post("/plan", follow_redirects=False)
        shop = client.get("/shopping").text
    assert "Fruit &amp; vegetables" in shop or "Fruit & vegetables" in shop
    assert "Meat, poultry &amp; fish" in shop or "Meat, poultry & fish" in shop


def test_print_page_groups_shopping_items_by_section():
    with TestClient(app) as client:
        _install_fakes(
            client,
            [_recipe("Chicken dinner", "chicken breast", "rice")],
            _recipe("B", "eggs"),
            [FakeOffer("rema", "Kyllingebryst", 39)],
            matches={"chicken breast": 0},  # chicken on offer (meat_fish); rice unmatched (pantry)
        )
        _set_profile(client)
        client.post("/plan", follow_redirects=False)
        printed = client.get("/print").text
    assert "Meat, poultry &amp; fish" in printed or "Meat, poultry & fish" in printed
    assert "Pantry &amp; dry goods" in printed or "Pantry & dry goods" in printed


def test_print_page_includes_shopping_list_when_stores_selected():
    with TestClient(app) as client:
        _install_fakes(
            client,
            [_recipe("Chicken dinner", "chicken breast", "rice")],
            _recipe("B", "eggs"),
            [FakeOffer("rema", "Kyllingebryst", 39)],
            matches={"chicken breast": 0},  # chicken is on offer; rice is not
        )
        _set_profile(client)
        client.post("/plan", follow_redirects=False)
        printed = client.get("/print").text
    assert 'class="print-shopping"' in printed
    assert "On offer" in printed and "Kyllingebryst" in printed and "DKK" in printed
    assert "Not on offer" in printed and "rice" in printed


def test_print_page_omits_shopping_section_without_stores():
    with TestClient(app) as client:
        _install_fakes(client, [_recipe("Chicken dinner", "chicken breast")],
                       _recipe("B", "eggs"), [])
        client.post("/profile", data={"max_kcal": "600", "min_protein_g": "5", "servings": "2"},
                    follow_redirects=False)  # no "stores" key -> none selected
        client.post("/plan", follow_redirects=False)
        printed = client.get("/print").text
    assert 'class="print-shopping"' not in printed


def test_planning_runs_in_background_then_shows_plan():
    import time

    with TestClient(app) as client:
        _install_fakes(client, [_recipe("Async Dish", "chicken breast")], _recipe("B", "eggs"), [])
        client.app.state.plan_sync = False  # exercise the real background thread
        _set_profile(client)
        posted = client.post("/plan", follow_redirects=False)
        assert posted.status_code in (302, 303)  # returns immediately, doesn't block
        home = ""
        for _ in range(50):  # poll while the background job runs (fast with fakes)
            home = client.get("/").text
            if "Async Dish" in home:
                break
            time.sleep(0.1)
    assert "Async Dish" in home
    assert "location.reload" not in home  # auto-refresh (progress state) gone once the plan is ready


def test_cost_view_renders_totals_from_seeded_prices():
    from app.db import connect

    with TestClient(app) as client:
        empty = client.get("/cost")
        assert empty.status_code == 200
        assert "No LLM calls logged yet" in empty.text
        c = connect(client.app.state.config.db_path)
        c.execute("INSERT INTO llm_calls (ts, call_type, provider, model, grounded, input_tokens, "
                  "output_tokens, search_queries, ok) VALUES "
                  "('2026-09-05', 'plan_week', 'gemini', 'gemini-3.6-flash', 1, 1000000, 1000000, 1000, 1)")
        c.commit()
        page = client.get("/cost").text
    assert "plan_week" in page
    assert "16.8" in page  # 1M*0.30 + 1M*2.50 + 1000/1000*14 from the seeded gemini-3.6-flash price


def test_cost_view_shows_weekly_trends():
    from app.db import connect

    with TestClient(app) as client:
        c = connect(client.app.state.config.db_path)
        c.execute("INSERT INTO plans (id, created_at, week, use_up) VALUES (1, '2026-09-01', '2026-W36', '')")
        c.execute("INSERT INTO recipes (plan_id, slot_index, title, servings, steps, flagged) "
                  "VALUES (1, 0, 'A', 2, '[]', 0), (1, 1, 'B', 2, '[]', 1)")  # 1 of 2 off-target
        c.commit()
        page = client.get("/cost").text
    assert "Trends by week" in page
    assert "2026-W36" in page
    assert "50%" in page  # 1 of 2 dinners flagged


def test_decline_swaps_the_dish():
    with TestClient(app) as client:
        _install_fakes(client, [_recipe("Chicken bowl", "chicken breast"),
                                _recipe("Trout plate", "trout fillet")],
                       _recipe("Backup dinner", "eggs"),
                       [FakeOffer("rema", "Kylling", 20)])
        _set_profile(client)
        client.post("/plan", follow_redirects=False)
        client.post("/plan/0/decline", follow_redirects=False)
        home = client.get("/")
    assert "Backup dinner" in home.text
    assert "Trout plate" in home.text


def test_declining_one_dish_only_greys_out_that_card():
    """A single-dish decline must not take over the whole page as if replanning the week."""
    import threading
    import time

    class SlowReplaceAgent:
        """Blocks in replace() until told to proceed, so the test can observe the in-flight state."""

        def __init__(self, recipes, replacement, ready):
            self._recipes = recipes
            self._replacement = replacement
            self._ready = ready

        def plan_week(self, ctx):
            return list(self._recipes)

        def replace(self, declined, ctx):
            self._ready.wait(timeout=5)
            return self._replacement

    ready = threading.Event()
    with TestClient(app) as client:
        _install_fakes(client, [_recipe("Chicken bowl", "chicken breast"),
                                _recipe("Trout plate", "trout fillet")],
                       _recipe("Backup dinner", "eggs"),
                       [FakeOffer("rema", "Kylling", 20)])
        _set_profile(client)
        client.post("/plan", follow_redirects=False)  # fast, synchronous initial plan

        def build(config, conn):
            slow = SlowReplaceAgent(
                [_recipe("Chicken bowl", "chicken breast"), _recipe("Trout plate", "trout fillet")],
                _recipe("Backup dinner", "eggs"), ready,
            )
            return Services(slow, FakeOffers([FakeOffer("rema", "Kylling", 20)]),
                            FakeNutrition(), FakeMatcher())

        client.app.state.build_services = build
        client.app.state.plan_sync = False  # exercise the real background thread

        posted = client.post("/plan/0/decline", follow_redirects=False)
        assert posted.status_code in (302, 303)

        # The background job is guaranteed still blocked in replace() here (ready isn't set yet).
        mid = client.get("/").text
        assert 'class="planning"' not in mid     # no whole-page takeover for a single dish
        assert "This week&#39;s dinners" in mid   # heading renders (t() escapes the apostrophe)
        assert "Trout plate" in mid              # the other dish renders normally
        assert 'class="card declining"' in mid   # the declined card is marked for greying out
        assert "Chicken bowl" in mid             # its old content stays visible while it waits

        ready.set()  # let the replacement complete
        home = ""
        for _ in range(50):
            home = client.get("/").text
            if "Backup dinner" in home:
                break
            time.sleep(0.1)
    assert "Backup dinner" in home
    assert "declining" not in home


def test_translated_content_renders_end_to_end():
    from app.translate import Translated

    class FakeTranslator:
        def translate(self, title, steps, ingredient_names, language):
            return Translated("Kyllingeskål", ["Steg det."], ["kyllingebryst"])

        def translate_batch(self, texts, language):
            return [f"[{t}]" for t in texts]  # stand-in "translation": marks each string

    with TestClient(app) as client:
        def build(config, conn):
            return Services(FakeAgent([_recipe("Chicken bowl", "chicken breast")], _recipe("B", "eggs")),
                            FakeOffers([FakeOffer("rema", "Kylling", 20)]), FakeNutrition(),
                            FakeMatcher(), FakeTranslator())

        client.app.state.build_services = build
        client.app.state.plan_sync = True
        _set_profile(client, language="Danish")  # profile_save warms the ui_translations cache
        client.post("/plan", follow_redirects=False)

        home = client.get("/").text
        assert "Kyllingeskål" in home and "Chicken bowl" not in home
        assert "Steg det." in home
        assert "kyllingebryst" in home
        assert "[This week&#39;s dinners]" in home  # static chrome translated too, not just recipes
        assert "[Shopping]" in home                # nav link

        shop = client.get("/shopping").text
        assert "kyllingebryst" in shop           # translated label shown
        assert 'value="chicken breast"' in shop  # English stays the checklist/lookup key
        assert "[Shopping list]" in shop

        printed = client.get("/print").text
        assert "Kyllingeskål" in printed and "Steg det." in printed


def test_static_chrome_stays_english_without_a_language_configured():
    with TestClient(app) as client:
        home = client.get("/").text
    assert "This week's dinners" not in home  # no plan yet, but nav/title chrome renders
    assert "Plan this week" in home
    assert "Shopping" in home and "[Shopping]" not in home
