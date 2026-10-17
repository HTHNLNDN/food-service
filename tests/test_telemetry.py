import json

import httpx
import pytest

from app import telemetry
from app.agent import PlanContext
from app.db import make_pool

CTX = PlanContext(max_kcal=600, min_protein_g=40, restrictions="", servings=2,
                  num_dinners=2, offers=[], preference_summary="")

_RECIPE = {"title": "A", "servings": 2, "ingredients": [{"name": "chicken", "grams": 100}],
           "steps": ["cook"]}


@pytest.fixture
def sink(conn, db):
    """Telemetry writing to the clean test DB; yields a connection to read the rows back."""
    pool = make_pool(db)
    pool.open(wait=True)
    telemetry.configure(pool)
    try:
        yield conn
    finally:
        telemetry.configure(None)  # don't leak the sink into other tests
        pool.close()


def test_record_writes_a_row_with_usage(sink):
    with telemetry.session() as trace, telemetry.record("plan_week", "gemini", "m", grounded=True):
        telemetry.set_usage(input_tokens=100, output_tokens=50, total_tokens=150,
                            search_queries=2, finish_reason="STOP")
    r = sink.execute("SELECT * FROM llm_calls").fetchone()
    assert r["call_type"] == "plan_week" and r["grounded"] == 1 and r["provider"] == "gemini"
    assert r["input_tokens"] == 100 and r["output_tokens"] == 50 and r["search_queries"] == 2
    assert r["trace_id"] == trace and r["ok"] == 1 and r["latency_ms"] is not None


def test_usage_accumulates_and_retry_counts(sink):
    with telemetry.session(), telemetry.record("nutrition_pick", "openai-compat", "m"):
        telemetry.set_usage(input_tokens=10, output_tokens=5)
        telemetry.set_usage(input_tokens=10, output_tokens=5)  # a re-prompt: sum, don't overwrite
        telemetry.bump_retry()
    r = sink.execute("SELECT * FROM llm_calls").fetchone()
    assert r["input_tokens"] == 20 and r["output_tokens"] == 10 and r["retries"] == 1


def test_link_plan_attributes_calls(sink):
    with telemetry.session() as trace, telemetry.record("plan_week", "gemini", "m"):
        telemetry.set_usage(input_tokens=1)
    telemetry.link_plan(sink, trace, 42)
    assert sink.execute("SELECT plan_id FROM llm_calls").fetchone()["plan_id"] == 42


def test_failure_is_recorded_then_reraised(sink):
    with pytest.raises(ValueError), telemetry.record("replace", "gemini", "m"):
        raise ValueError("boom")
    r = sink.execute("SELECT ok, error FROM llm_calls").fetchone()
    assert r["ok"] == 0 and "boom" in r["error"]


def test_v_llm_cost_computes_from_model_prices(sink):
    sink.execute("INSERT INTO model_prices (model, input_per_mtok, output_per_mtok, "
                 "search_per_ktok) VALUES ('m2', 1.0, 2.0, 10.0)")
    sink.execute("INSERT INTO llm_calls (ts, call_type, provider, model, input_tokens, output_tokens, "
                 "search_queries, ok) VALUES (now(), 'plan_week', 'p', 'm2', 1000000, 500000, 100, TRUE)")
    usd = sink.execute("SELECT usd FROM v_llm_cost").fetchone()["usd"]
    assert usd == pytest.approx(1.0 + 1.0 + 1.0)  # 1M*1 + 0.5M*2 + 100/1000*10


def test_default_price_is_seeded(sink):
    row = sink.execute("SELECT * FROM model_prices WHERE model = 'gemini-3.6-flash'").fetchone()
    assert row is not None and row["output_per_mtok"] > 0


def test_disabled_when_not_configured():
    telemetry.configure(None)
    with telemetry.record("x", "p", "m"):  # must not raise even with no sink
        telemetry.set_usage(input_tokens=1)


def test_real_agent_call_records_tokens(sink):
    from app.agent import OpenAICompatibleAgent

    def handler(_req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"recipes": [_RECIPE, _RECIPE]})},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
        })

    client = httpx.Client(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler))
    agent = OpenAICompatibleAgent("https://llm.test/v1", "test-model", "k", client=client)
    with telemetry.session():
        agent.plan_week(CTX)
    r = sink.execute("SELECT * FROM llm_calls WHERE call_type = 'plan_week'").fetchone()
    assert r["input_tokens"] == 120 and r["output_tokens"] == 80
    assert r["provider"] == "openai-compat" and r["model"] == "test-model" and r["ok"] == 1
