"""Spike (T2): prove ShelfAtlas access + resolve R1b (chain vs physical-store offers).

Throwaway discovery script, not production code. Self-contained (reads .env directly).
Dumps the raw offer structure so T5 (offers slice) can be built against the real shape.

Run:   uv run python scripts/spike_shelfatlas.py [chain_slug]   # default: rema
Needs: SHELFATLAS_API_KEY in .env  (free tier: https://app.shelfatlas.com/app/keys/new)
"""

import json
import os
import sys

import httpx
from dotenv import load_dotenv

BASE = "https://api.shelfatlas.com/api/v1/public/catalog"


def _as_list(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "offers", "items", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def main(chain_slug: str) -> None:
    load_dotenv()
    api_key = os.environ.get("SHELFATLAS_API_KEY")
    if not api_key:
        sys.exit("SHELFATLAS_API_KEY not set — add it to .env (see the script docstring).")

    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(base_url=BASE, headers=headers, timeout=30) as client:
        chains = _as_list(client.get("/chains").raise_for_status().json())
        print(f"=== {len(chains)} chains ===")
        for c in chains:
            print(f"  {c.get('slug')} - {c.get('name')}")

        print(f"\n=== offers for chain_slug={chain_slug!r} ===")
        resp = client.get("/offers", params={"chain_slug": chain_slug, "limit": 5})
        print("status:", resp.status_code)
        resp.raise_for_status()
        payload = resp.json()
        print("payload type:", type(payload).__name__)
        if isinstance(payload, dict):
            print("payload keys:", list(payload.keys()))
        offers = _as_list(payload)
        print("offers in page:", len(offers))
        print(json.dumps(offers[:2], indent=2, ensure_ascii=False)[:2000])
        has_store = any(isinstance(o, dict) and "storeId" in o for o in offers[:5])
        print(f"\nR1b probe — offers carry a storeId field: {has_store}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "rema")
