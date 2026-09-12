from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import i18n, telemetry
from app.config import load_config
from app.db import bootstrap, connect
from app.profile import load_preferences
from app.profile import router as profile_router
from app.services import build_services
from app.ui import router as ui_router

BASE_DIR = Path(__file__).resolve().parent


def _i18n_context(request: Request) -> dict:
    """Injects `t(english) -> translated`, backed by the ui_translations cache (app/i18n.py).
    Falls back to English for any string not yet cached — never blocks a page render."""
    conn = connect(request.app.state.config.db_path)
    try:
        strings = i18n.cached(conn, load_preferences(conn).language)
    finally:
        conn.close()
    return {"t": lambda s: strings.get(s, s)}


templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"), context_processors=[_i18n_context]
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.config = config
    app.state.templates = templates
    app.state.build_services = build_services  # tests override this with fakes
    conn = connect(config.db_path)
    bootstrap(conn)
    conn.close()
    telemetry.configure(config.db_path)  # record LLM token/grounding spend to llm_calls
    yield
    telemetry.configure(None)


app = FastAPI(title="food-service", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(ui_router)
app.include_router(profile_router)
