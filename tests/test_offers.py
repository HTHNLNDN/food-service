from decimal import Decimal

import httpx

from app.db import bootstrap, connect
from app.offers import SHELFATLAS_BASE, Offer, ShelfAtlasOffersProvider


def _raw_offer(slug: str) -> dict:
    return {
        "id": f"id-{slug}",
        "chainId": f"uuid-{slug}",
        "storeId": None,
        "rawName": f"{slug.upper()} tilbud",
        "price": "15.00",
        "unitPrice": "100.0000",
        "currency": "DKK",
        "validFrom": "2026-08-22T22:00:00.000Z",
        "validTo": "2026-08-29T21:59:59.000Z",
        "volumeMl": 150,
        "unitCount": 1,
        "priceKind": "campaign",
    }


def _provider(tmp_path):
    conn = connect(tmp_path / "o.db")
    bootstrap(conn)
    state = {"calls": 0, "slugs": []}

    def handler(request):
        state["calls"] += 1
        slug = request.url.params.get("chain_slug")
        state["slugs"].append(slug)
        return httpx.Response(200, json={"ok": True, "data": [_raw_offer(slug)], "pagination": {}})

    client = httpx.Client(base_url=SHELFATLAS_BASE, transport=httpx.MockTransport(handler))
    return ShelfAtlasOffersProvider(conn, api_key="test", client=client), state


def test_offers_returns_only_selected_chains(tmp_path):
    provider, state = _provider(tmp_path)
    offers = provider.offers(["rema"])
    assert offers  # non-empty
    assert all(o.chain_slug == "rema" for o in offers)
    # a chain not selected is never even fetched
    assert "netto" not in state["slugs"]
    assert set(state["slugs"]) == {"rema"}


def test_offers_parses_fields(tmp_path):
    provider, _ = _provider(tmp_path)
    offer = provider.offers(["rema"])[0]
    assert isinstance(offer, Offer)
    assert offer.name == "REMA tilbud"
    assert offer.price == Decimal("15.00")
    assert offer.currency == "DKK"


def test_offers_are_cached_within_the_week(tmp_path):
    provider, state = _provider(tmp_path)
    provider.offers(["rema"])
    provider.offers(["rema"])
    assert state["calls"] == 1  # second call served from cache


def test_refresh_forces_refetch(tmp_path):
    provider, state = _provider(tmp_path)
    provider.offers(["rema"])
    provider.offers(["rema"], refresh=True)
    assert state["calls"] == 2


def test_multiple_selected_chains_each_fetched(tmp_path):
    provider, state = _provider(tmp_path)
    offers = provider.offers(["rema", "netto"])
    assert {o.chain_slug for o in offers} == {"rema", "netto"}
    assert set(state["slugs"]) == {"rema", "netto"}
