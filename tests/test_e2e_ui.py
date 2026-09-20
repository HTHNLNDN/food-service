"""End-to-end UI test: drive the real app in a headless browser with fake services.

Runs uvicorn in a background thread and injects fake collaborators (no real LLM/network),
then Playwright clicks through the actual flow: set preferences -> plan the week -> decline
a dish. This is the pattern to extend when adding UI features.

Requires the Playwright browser: `uv run playwright install chromium`.
"""

import socket
import threading
import time
from decimal import Decimal

import pytest
import uvicorn

from app.agent import Recipe
from app.main import app
from app.nutrition import Ingredient, Macros
from app.services import Services


class _FakeAgent:
    def plan_week(self, ctx):
        return [
            Recipe("Chicken Bowl", 4, [Ingredient("chicken breast", 400)], ["Cook it."]),
            Recipe("Trout Plate", 4, [Ingredient("trout fillet", 400)], ["Bake it."]),
        ]

    def replace(self, declined, ctx):
        return Recipe("Backup Dinner", 4, [Ingredient("eggs", 400)], ["Scramble."])


class _FakeOffer:
    def __init__(self):
        self.chain_slug = "rema"
        self.name = "Kylling"
        self.price = Decimal(20)


class _FakeOffers:
    def offers(self, selected):
        return [_FakeOffer()]


class _FakeNutrition:
    def lookup(self, name):
        return (1, Macros(100, 20, 0, 0, 2))  # per 100g -> on-target at 400g/4 servings


class _FakeMatcher:
    def match(self, ingredients, offers):
        return {n: (0 if "chicken" in n else None) for n in ingredients}  # chicken -> offer, rest not


class _FakeTranslator:
    def translate(self, title, steps, ingredient_names, language):
        return None  # not exercised here — only the UI-chrome batch path matters for this test

    def translate_batch(self, texts, language):
        # Deliberately long stand-in "translations" — longer than any real Danish string in
        # app/i18n.py's catalog, to prove the layout survives worse than the real case.
        return ["Afvis dette forslag til middagsret og find en ny erstatning i stedet" for _ in texts]


def _fake_build(config, conn):
    return Services(_FakeAgent(), _FakeOffers(), _FakeNutrition(), _FakeMatcher())


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("FOOD_SERVICE_DATA_DIR", str(tmp_path))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    app.state.build_services = _fake_build  # override the real builder set during lifespan
    app.state.plan_sync = True  # run planning inline (no background thread) for deterministic e2e
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_plan_flow_clickthrough(live_server, page):
    # 1. set preferences
    page.goto(f"{live_server}/profile")
    page.fill("input[name=max_kcal]", "600")
    page.fill("input[name=min_protein_g]", "5")
    page.fill("input[name=servings]", "2")
    page.check("input[value='rema']")
    page.click("button[type=submit]")

    # 2. plan the week
    page.goto(live_server)
    page.click("text=Plan this week")
    page.wait_for_selector(".card")
    assert page.locator(".card").count() >= 2
    assert "Chicken Bowl" in page.content()
    assert "g protein" in page.content()

    # 3. decline the first dish -> replaced
    page.locator("form[action$='/decline'] button").first.click()
    page.wait_for_selector(".card")
    assert "Backup Dinner" in page.content()


def test_shopping_view_check_off_persists(live_server, page):
    # set up a plan
    page.goto(f"{live_server}/profile")
    page.fill("input[name=min_protein_g]", "5")
    page.fill("input[name=servings]", "2")
    page.check("input[value='rema']")
    page.click("button[type=submit]")
    page.goto(live_server)
    page.click("text=Plan this week")
    page.wait_for_selector(".card")

    # dedicated shopping view: both sections + checkable items
    page.goto(f"{live_server}/shopping")
    page.wait_for_selector("input.tick")
    assert "On offer" in page.content()       # chicken breast matched an offer
    assert "Not on offer" in page.content()   # trout fillet did not

    # check off the first item -> row marked done
    row = page.locator("li:has(input.tick)").first
    row.locator("input.tick").check()
    assert "done" in (row.get_attribute("class") or "")

    # persists across a reload (localStorage)
    page.reload()
    page.wait_for_selector("input.tick")
    assert page.locator("li.done").count() >= 1


def test_use_up_field_persists_after_planning(live_server, page):
    # set minimal profile
    page.goto(f"{live_server}/profile")
    page.fill("input[name=min_protein_g]", "5")
    page.fill("input[name=servings]", "2")
    page.check("input[value='rema']")
    page.click("button[type=submit]")

    # enter leftovers, plan, and confirm the field is retained (persisted on the plan)
    page.goto(live_server)
    page.fill("input[name=use_up]", "3 kg chicken breast")
    page.click("text=Plan this week")
    page.wait_for_selector(".card")
    assert page.input_value("input[name=use_up]") == "3 kg chicken breast"


def test_ban_removes_fish_from_plan(live_server, page):
    # ban Fish -> the fake agent's Trout dish must be replaced (guard enforced through the UI)
    page.goto(f"{live_server}/profile")
    page.fill("input[name=min_protein_g]", "5")
    page.fill("input[name=servings]", "2")
    page.check("input[value='fish']")
    page.click("button[type=submit]")

    page.goto(live_server)
    page.click("text=Plan this week")
    page.wait_for_selector(".card")
    content = page.content()
    assert "Trout" not in content        # fish dish banned & replaced
    assert "Backup Dinner" in content    # the replacement


def test_no_horizontal_overflow_with_long_translated_text(live_server, page):
    def build(config, conn):
        return Services(_FakeAgent(), _FakeOffers(), _FakeNutrition(), _FakeMatcher(),
                         _FakeTranslator())

    # Install the fake translator BEFORE saving the profile: profile_save() calls
    # i18n.warm() synchronously, which no-ops if request.app.state.build_services doesn't
    # yet resolve to a translator — installing it after the save would leave the static
    # UI-chrome cache (nav/button labels) untranslated and this test would pass for the
    # wrong reason (only recipe content would be long, not the chrome that actually broke).
    app.state.build_services = build

    page.set_viewport_size({"width": 360, "height": 780})  # narrow phone width

    page.goto(f"{live_server}/profile")
    page.fill("input[name=min_protein_g]", "5")
    page.fill("input[name=servings]", "2")
    page.check("input[value='rema']")
    page.fill("input[name=language]", "Danish")
    page.click("button[type=submit]")

    page.goto(live_server)
    # Not `text=Plan this week` — that button's text is translated now too (the fake
    # translator applies to every cached UI string, chrome included), so select by
    # structure instead of by English copy.
    page.click(".plan-form button[type=submit]")
    page.wait_for_selector(".card")

    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )
    assert not overflow, "page overflows horizontally at mobile width with long translated text"


def test_bottom_tab_bar_navigates_between_pages(live_server, page):
    page.set_viewport_size({"width": 360, "height": 780})
    page.goto(live_server)
    page.wait_for_selector("nav.tabbar")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )

    page.click("nav.tabbar >> text=Shopping")
    page.wait_for_url("**/shopping")

    page.click("nav.tabbar >> text=More")
    page.wait_for_url("**/more")
    assert "Preferences" in page.content()
    assert "Cost" in page.content()
    assert "My data" in page.content()

    page.click("text=Preferences")
    page.wait_for_url("**/profile")


def test_tab_bar_renders_above_content_on_desktop(live_server, page):
    # At the desktop breakpoint (>=768px) the tab bar switches from `position: fixed`
    # (viewport-pinned, DOM order irrelevant) to `position: static` (normal document
    # flow, DOM order determines rendered position). It must appear above the main
    # content, as a top bar, not below it.
    page.set_viewport_size({"width": 1024, "height": 800})
    page.goto(live_server)
    page.wait_for_selector("nav.tabbar")
    page.wait_for_selector("main.container")

    nav_box = page.locator("nav.tabbar").bounding_box()
    main_box = page.locator("main.container").bounding_box()
    assert nav_box["y"] < main_box["y"], (
        "tab bar should render above the main content on desktop viewports"
    )


def test_tabbar_hidden_in_print_media(live_server, page):
    # Regression test: `.tabbar` is a class selector (specificity 0,1,0) with an
    # unscoped `display: flex` rule, which beats the print stylesheet's `nav { display:
    # none }` (element selector, specificity 0,0,1) regardless of source order. That let
    # the fixed-position tab bar print once, anchored over the first page, in every
    # browser's print preview/PDF output — caught during manual verification, not by any
    # existing test. The fix targets `.tabbar` explicitly in the print rule so the
    # specificities match and source order (print rule comes later) decides correctly.
    page.goto(f"{live_server}/profile")
    page.fill("input[name=min_protein_g]", "5")
    page.fill("input[name=servings]", "2")
    page.check("input[value='rema']")
    page.click("button[type=submit]")
    page.goto(live_server)
    page.click(".plan-form button[type=submit]")
    page.wait_for_selector(".card")

    page.goto(f"{live_server}/print")
    page.wait_for_selector("nav.tabbar")
    page.emulate_media(media="print")
    display = page.eval_on_selector("nav.tabbar", "el => getComputedStyle(el).display")
    assert display == "none", "tab bar must not render in print media"
