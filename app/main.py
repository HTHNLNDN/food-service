from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import telemetry
from app.config import load_config
from app.db import bootstrap, connect
from app.profile import router as profile_router
from app.services import build_services
from app.ui import router as ui_router

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
