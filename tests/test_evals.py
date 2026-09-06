import httpx
import pytest

from app.agent import Recipe
from app.nutrition import Ingredient, Macros
from app.planner import PlannedRecipe
from app.services import Services
from evals import run, scorers
from evals.judge import LLMJudge, _parse_scores
from evals.scenarios import Scenario


def _r(title, kcal, protein, ings, flagged=False):
    return PlannedRecipe(title, 2, [Ingredient(n, g) for n, g in ings], ["cook"],
                         Macros(kcal, protein, 0, 0, 0), flagged)


# --- deterministic scorers ---


def test_macro_pass_rate_counts_within_target():
    scn = Scenario("t", max_kcal=600, min_protein_g=40)
    recipes = [
        _r("ok", 500, 45, [("chicken breast", 300)]),   # within
        _r("hot", 700, 45, [("beef", 300)]),            # over kcal
        _r("weak", 500, 30, [("tofu", 300)]),           # under protein
    ]
    assert scorers.macro_pass_rate(recipes, scn) == pytest.approx(1 / 3)


def test_ban_violations_flags_banned_ingredient():
    scn = Scenario("t", bans=("fish",))
    recipes = [_r("a", 500, 45, [("salmon fillet", 300)]), _r("b", 500, 45, [("chicken", 300)])]
    assert scorers.ban_violations(recipes, scn) == 1


def test_distinct_proteins_groups_by_type():
    recipes = [_r("a", 1, 1, [("chicken breast", 300)]),
               _r("b", 1, 1, [("pork loin", 300)]),
               _r("c", 1, 1, [("chicken thigh", 300)])]
    assert scorers.distinct_proteins(recipes) == 2  # chicken, pork


def test_repetition_lower_for_distinct_than_duplicates():
    distinct = [_r("a", 1, 1, [("chicken", 200), ("broccoli", 100)]),
                _r("b", 1, 1, [("beef", 200), ("rice", 100)])]
    dupes = [_r("a", 1, 1, [("chicken", 200), ("broccoli", 100)]),
             _r("b", 1, 1, [("chicken", 200), ("broccoli", 100)])]
    assert scorers.repetition(distinct) < scorers.repetition(dupes)
    assert scorers.repetition(dupes) == pytest.approx(1.0)


# --- runner orchestration (fakes: no real LLM) ---


class _FakeAgent:
    def plan_week(self, ctx):
        return [Recipe("Chicken Bowl", 2, [Ingredient("chicken breast", 300)], ["cook"])
                for _ in range(ctx.num_dinners)]

    def replace(self, declined, ctx):
        return Recipe("Egg Bowl", 2, [Ingredient("eggs", 300)], ["cook"])


class _FakeNutrition:
    def lookup(self, name):
        return (1, Macros(150, 30, 0, 0, 0))  # 300 g / 2 servings -> 225 kcal, 45 g protein


class _FakeMatcher:
    def match(self, ingredients, offers):
        return {n: None for n in ingredients}


def test_run_scenario_produces_metrics_with_fakes(monkeypatch):
    from app import telemetry
    from app.config import load_config

    def fake_build(config, conn):
        return Services(_FakeAgent(), None, _FakeNutrition(), _FakeMatcher())

    try:
        rows = run.run_scenario(load_config(), Scenario("baseline"), samples=1, build=fake_build)
    finally:
        telemetry.configure(None)  # don't leak the scratch sink into other tests
    assert len(rows) == 1
    m = rows[0]
    assert set(m) == {"macro_pass", "ban_violations", "flagged", "proteins", "repetition",
                      "usd", "revises", "usd_per_dinner", "latency_s", "_total_usd"}
    assert m["ban_violations"] == 0
    assert 0.0 <= m["macro_pass"] <= 1.0
    assert m["usd"] == 0.0  # fakes make no billed calls


# --- LLM-as-judge (mocked) ---


def _judge(handler):
    client = httpx.Client(base_url="https://judge.test/v1", transport=httpx.MockTransport(handler))
    return LLMJudge("https://judge.test/v1", "judge-m", "k", client=client)


def _completion(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_judge_parses_and_clamps_scores():
    recipes = [_r("a", 1, 1, [("chicken", 100)]), _r("b", 1, 1, [("beef", 100)])]
    body = '[{"appeal":4,"realism":5,"effort":3},{"appeal":9,"realism":0,"effort":2}]'
    scores = _judge(lambda _r: _completion(body)).score(recipes)
    assert scores[0] == {"appeal": 4, "realism": 5, "effort": 3}
    assert scores[1] == {"appeal": 5, "realism": 1, "effort": 2}  # 9->5, 0->1


def test_judge_tolerates_prose_and_garbage():
    recipes = [_r("a", 1, 1, [("chicken", 100)])]
    good = _judge(lambda _r: _completion('Here you go: [{"appeal":3,"realism":4,"effort":5}] thanks'))
    assert good.score(recipes)[0] == {"appeal": 3, "realism": 4, "effort": 5}
    bad = _judge(lambda _r: _completion("sorry, no JSON"))
    assert bad.score(recipes)[0] == {"appeal": None, "realism": None, "effort": None}


def test_parse_scores_pads_count_mismatch():
    out = _parse_scores('[{"appeal":3,"realism":3,"effort":3}]', 2)
    assert out[0]["appeal"] == 3
    assert out[1] == {"appeal": None, "realism": None, "effort": None}


# --- CI/pre-merge gate ---


def test_gate_passes_on_healthy_metrics():
    from evals.gate import check_gates
    healthy = {"ban_violations": 0.0, "macro_pass": 0.91, "flagged": 0.03, "usd_per_dinner": 0.001}
    assert check_gates(healthy) == []


def test_gate_fails_on_ban_violation_macro_drop_and_cost_spike():
    from evals.gate import check_gates
    bad = {"ban_violations": 0.1, "macro_pass": 0.4, "flagged": 0.5, "usd_per_dinner": 0.05}
    breached = {f[0] for f in check_gates(bad)}
    assert breached == {"ban_violations", "macro_pass", "flagged", "usd_per_dinner"}
