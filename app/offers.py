import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

import httpx

from app.dates import iso_week

SHELFATLAS_BASE = "https://api.shelfatlas.com/api/v1/public/catalog"


@dataclass(frozen=True)
class Offer:
    chain_slug: str
    name: str            # rawName — free-text Danish promo name
    price: Decimal
    currency: str
    valid_from: str
    valid_to: str
    unit_price: Decimal | None
    volume_ml: int | None
    unit_count: int | None
    price_kind: str | None


class OffersProvider(Protocol):
    def offers(self, selected: list[str]) -> list[Offer]:
        """Current offers for the selected chains only (the store whitelist)."""
        ...


def _to_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _parse_offer(raw: dict, chain_slug: str) -> Offer:
    return Offer(
        chain_slug=chain_slug,
        name=raw.get("rawName", ""),
        price=_to_decimal(raw.get("price")) or Decimal(0),
        currency=raw.get("currency", "DKK"),
        valid_from=raw.get("validFrom", ""),
        valid_to=raw.get("validTo", ""),
        unit_price=_to_decimal(raw.get("unitPrice")),
        volume_ml=raw.get("volumeMl"),
        unit_count=raw.get("unitCount"),
        price_kind=raw.get("priceKind"),
    )


class ShelfAtlasOffersProvider:
    """Fetches ShelfAtlas offers for whitelisted chains, cached per ISO week."""

    def __init__(self, conn: sqlite3.Connection, api_key: str, client: httpx.Client | None = None):
        self.conn = conn
        self._client = client or httpx.Client(
            base_url=SHELFATLAS_BASE, headers={"Authorization": f"Bearer {api_key}"}, timeout=30
        )

    def offers(self, selected: list[str], refresh: bool = False) -> list[Offer]:
        week = iso_week(datetime.now(UTC).date())
        allowed = set(selected)
        result: list[Offer] = []
        for slug in selected:
            for raw in self._raw_for_chain(slug, week, refresh):
                offer = _parse_offer(raw, slug)
                if offer.chain_slug in allowed:  # defensive: whitelist only
                    result.append(offer)
        return result

    def _raw_for_chain(self, slug: str, week: str, refresh: bool) -> list[dict]:
        if not refresh:
            row = self.conn.execute(
                "SELECT payload FROM offers_cache WHERE chain_slug = ? AND week = ?", (slug, week)
            ).fetchone()
            if row is not None:
                return json.loads(row["payload"])

        resp = self._client.get("/offers", params={"chain_slug": slug, "limit": 100})
        resp.raise_for_status()
        payload = resp.json()
        raw = payload.get("data", []) if isinstance(payload, dict) else payload
        self.conn.execute(
            "INSERT OR REPLACE INTO offers_cache(chain_slug, week, payload) VALUES (?, ?, ?)",
            (slug, week, json.dumps(raw)),
        )
        self.conn.commit()
        return raw
