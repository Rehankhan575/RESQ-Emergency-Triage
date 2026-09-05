import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from app.api.routes.calls import router as calls_router
from app.api.routes.websocket import router as ws_router
from app.api.routes.geocode import router as geocode_router
from app.api.routes.config import router as config_router
from app.api.routes.silent_reports import router as silent_reports_router
from app.api.routes.auth import router as auth_router
from app.models.database import init_db

from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import RedirectResponse
from fastapi import Request

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize the database on startup
    await init_db()
    yield

app = FastAPI(title="SIH Triage API", lifespan=lifespan)

# Add session middleware for securely signed cookies (12 hours)
app.add_middleware(
    SessionMiddleware, 
    secret_key="SUPER_SECRET_CHANGE_IN_PRODUCTION",
    max_age=12 * 60 * 60
)

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/dashboard"):
            if not request.session.get("user"):
                return RedirectResponse(url="/login", status_code=303)
        return await call_next(request)

app.add_middleware(AuthMiddleware)

app.include_router(auth_router)
app.include_router(calls_router)
app.include_router(ws_router)
app.include_router(geocode_router)
app.include_router(config_router)
app.include_router(silent_reports_router)

# Serve the dashboard at /dashboard/index.html
_DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "dashboard", "static")
app.mount("/dashboard", StaticFiles(directory=_DASHBOARD_DIR, html=True), name="dashboard")

@app.get("/")
async def root():
    return {"status": "healthy"}
