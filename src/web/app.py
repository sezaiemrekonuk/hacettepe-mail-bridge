"""
FastAPI web application for HU Mail Bridge.
Serves the public apply form and the admin dashboard.
The scraper loop is started as a background daemon thread on startup.
"""
import collections
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer

# --------------------------------------------------------------------------- #
# In-memory log store
# --------------------------------------------------------------------------- #

_LOG_BUFFER: collections.deque = collections.deque(maxlen=500)


class _MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:
            pass

from src.web.db import (
    create_application,
    delete_application,
    encrypt_password,
    get_application,
    init_db,
    list_applications,
    update_status,
)

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
serializer = URLSafeSerializer(SECRET_KEY)


def get_admin_email(request: Request) -> str | None:
    cookie = request.cookies.get("admin_session")
    if not cookie:
        return None
    try:
        return serializer.loads(cookie)
    except Exception:
        return None


def require_admin(request: Request) -> str:
    email = get_admin_email(request)
    admin_email = os.environ.get("ADMIN_EMAIL", "sezaiemrekonuk@gmail.com")
    if email != admin_email:
        # Raise a special sentinel; caught in the route handlers
        raise _AdminRedirect()
    return email


class _AdminRedirect(Exception):
    pass


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Attach memory log handler to root logger so all modules' logs are captured
    _handler = _MemoryLogHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s – %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S")
    )
    logging.getLogger().addHandler(_handler)

    init_db()
    from src.main import run_scraper_loop
    thread = threading.Thread(target=run_scraper_loop, daemon=True)
    thread.start()
    yield


# --------------------------------------------------------------------------- #
# App & templates
# --------------------------------------------------------------------------- #

app = FastAPI(title="HU Mail Bridge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --------------------------------------------------------------------------- #
# Public routes
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    applied = request.query_params.get("applied", "")
    msg = request.query_params.get("msg", "")
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "applied": applied == "1", "msg": msg},
    )


@app.post("/apply")
async def apply(
    request: Request,
    hu_email: str = Form(...),
    hu_password: str = Form(...),
    gmail_target: str = Form(...),
):
    try:
        enc_pass = encrypt_password(hu_password)
        create_application(hu_email, enc_pass, gmail_target)
    except Exception as exc:
        # Likely a UNIQUE constraint violation (already applied)
        msg = "An application with that Hacettepe email already exists."
        return RedirectResponse(f"/?msg={msg}", status_code=303)
    return RedirectResponse("/?applied=1", status_code=303)


# --------------------------------------------------------------------------- #
# Admin auth routes
# --------------------------------------------------------------------------- #


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    error = request.query_params.get("error", "")
    admin_email = os.environ.get("ADMIN_EMAIL", "sezaiemrekonuk@gmail.com")
    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "error": error == "1", "admin_email": admin_email},
    )


@app.post("/admin/login")
async def admin_login(
    request: Request,
    password: str = Form(...),
):
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    admin_email = os.environ.get("ADMIN_EMAIL", "sezaiemrekonuk@gmail.com")

    if not admin_password or password != admin_password:
        return RedirectResponse("/admin/login?error=1", status_code=303)

    token = serializer.dumps(admin_email)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,  # 1 week
    )
    return response


@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response


# --------------------------------------------------------------------------- #
# Admin dashboard routes
# --------------------------------------------------------------------------- #


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    try:
        require_admin(request)
    except _AdminRedirect:
        return RedirectResponse("/admin/login", status_code=303)

    fernet_key_set = bool(os.environ.get("FERNET_KEY", ""))
    pending = list_applications(status="pending")
    active = list_applications(status="active")
    rejected = list_applications(status="rejected")

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "pending": pending,
            "active": active,
            "rejected": rejected,
            "fernet_key_set": fernet_key_set,
            "admin_email": os.environ.get("ADMIN_EMAIL", "sezaiemrekonuk@gmail.com"),
        },
    )


@app.post("/admin/approve/{app_id}")
async def admin_approve(request: Request, app_id: int):
    try:
        require_admin(request)
    except _AdminRedirect:
        return RedirectResponse("/admin/login", status_code=303)

    update_status(app_id, "active")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/reject/{app_id}")
async def admin_reject(request: Request, app_id: int):
    try:
        require_admin(request)
    except _AdminRedirect:
        return RedirectResponse("/admin/login", status_code=303)

    update_status(app_id, "rejected")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/delete/{app_id}")
async def admin_delete(request: Request, app_id: int):
    try:
        require_admin(request)
    except _AdminRedirect:
        return RedirectResponse("/admin/login", status_code=303)

    delete_application(app_id)
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/logs")
async def admin_logs(request: Request, n: int = 200):
    """Return the last n log lines as JSON (used by the admin live-log panel)."""
    try:
        require_admin(request)
    except _AdminRedirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    lines = list(_LOG_BUFFER)[-n:]
    return JSONResponse({"lines": lines})
