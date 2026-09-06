"""Builds the swappable collaborators (agent, offers, nutrition) from config.

Kept in one place so routes stay thin and tests can inject fakes via
`app.state.build_services`.
"""

import sqlite3
from dataclasses import dataclass

from app.agent import GeminiGroundedAgent, MealPlanAgent, OpenAICompatibleAgent
from app.config import Config
from app.db import connect
from app.nutrition import LLMCandidatePicker, LocalNutritionSource, NutritionSource
from app.offers import OffersProvider, ShelfAtlasOffersProvider
from app.shopping import LLMOfferMatcher, OfferMatcher


@dataclass
class Services:
    agent: MealPlanAgent
    offers: OffersProvider
    nutrition: NutritionSource
    offer_matcher: OfferMatcher


def _build_agent(config: Config) -> MealPlanAgent:
    key = config.llm_api_key or ""
    if "generativelanguage.googleapis.com" in config.llm_base_url:
        # Gemini: use the native API with Google Search grounding for recipes.
        native = config.llm_base_url.rsplit("/openai", 1)[0]  # .../v1beta/openai -> .../v1beta
        return GeminiGroundedAgent(native, config.llm_model, key)
    return OpenAICompatibleAgent(config.llm_base_url, config.llm_model, key)


def build_services(config: Config, conn: sqlite3.Connection) -> Services:
    # Picker uses the OpenAI-compatible endpoint (food selection needs no grounding).
    picker = LLMCandidatePicker(config.llm_base_url, config.llm_model, config.llm_api_key or "")
    key = config.llm_api_key or ""
    return Services(
        agent=_build_agent(config),
        offers=ShelfAtlasOffersProvider(conn, config.shelfatlas_api_key or ""),
        # numbers come from the local USDA store; the picker only chooses which food, cached in `conn`
        nutrition=LocalNutritionSource(connect(config.usda_db_path), conn, picker),
        # offer matcher picks which Danish offer fits an English ingredient (never a price)
        offer_matcher=LLMOfferMatcher(config.llm_base_url, config.llm_model, key),
    )
