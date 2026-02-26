import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlencode

import httpx
import psycopg2
from psycopg2 import sql
from authlib.integrations.starlette_client import OAuth
from authlib.integrations.base_client.errors import MismatchingStateError
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine, inspect, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware
import jwt

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "nexora.red")
ODOO_NAMESPACE = os.getenv("ODOO_NAMESPACE", "odoo-system")
ODOO_IMAGE = os.getenv("ODOO_IMAGE", "odoo:19")
ODOO_ADMIN_PASSWD = os.getenv("ODOO_ADMIN_PASSWD")
MASTER_ADMIN_EMAIL = os.getenv("MASTER_ADMIN_EMAIL", "").strip().lower()
MASTER_ADMIN_EMAILS_RAW = os.getenv("MASTER_ADMIN_EMAILS", "admin@nexora.red").strip().lower()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")
CONTROL_PLANE_DATABASE_URL = os.getenv("CONTROL_PLANE_DATABASE_URL", "").strip()
CONTROL_PLANE_DB_NAME = os.getenv("CONTROL_PLANE_DB_NAME", "nexora_control_plane").strip()

APP_SECRET = os.getenv("APP_SECRET", "change-me")

GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")
GITHUB_APP_SLUG = os.getenv("GITHUB_APP_SLUG", "nexora-platform")

DEFAULT_DEV_BRANCH = os.getenv("DEFAULT_DEV_BRANCH", "dev")
DEFAULT_STAGING_BRANCH = os.getenv("DEFAULT_STAGING_BRANCH", "staging")
DEFAULT_PROD_BRANCH = os.getenv("DEFAULT_PROD_BRANCH", "main")
DEFAULT_WORKERS = int(os.getenv("DEFAULT_WORKERS", "1"))
DEFAULT_STORAGE_GB = int(os.getenv("DEFAULT_STORAGE_GB", "1"))
DEFAULT_STAGING_SLOTS = int(os.getenv("DEFAULT_STAGING_SLOTS", "1"))
DEFAULT_ODOO_VERSION = os.getenv("DEFAULT_ODOO_VERSION", "19.0")
STORAGE_CLASS_NAME = os.getenv("STORAGE_CLASS_NAME", "csi-cinder-high-speed")
TLS_CLUSTER_ISSUER = os.getenv("TLS_CLUSTER_ISSUER", "letsencrypt-prod").strip()
APEX_ENV = os.getenv("APEX_ENV", "dev").strip().lower()
KUBECTL_GET_TIMEOUT = int(os.getenv("KUBECTL_GET_TIMEOUT", "8"))
KUBECTL_MUTATE_TIMEOUT = int(os.getenv("KUBECTL_MUTATE_TIMEOUT", "30"))
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "8"))
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "20000"))
OVH_ENDPOINT = os.getenv("OVH_ENDPOINT", "ovh-ca").strip().lower()
OVH_PROJECT_ID = (os.getenv("OVH_PROJECT_ID") or os.getenv("OVH_SERVICE_NAME") or "").strip()
OVH_APPLICATION_KEY = os.getenv("OVH_APPLICATION_KEY", "").strip()
OVH_APPLICATION_SECRET = os.getenv("OVH_APPLICATION_SECRET", "").strip()
OVH_CONSUMER_KEY = os.getenv("OVH_CONSUMER_KEY", "").strip()
OVH_API_TIMEOUT = int(os.getenv("OVH_API_TIMEOUT", "12"))
OVH_QUOTA_CACHE_TTL = max(5, int(os.getenv("OVH_QUOTA_CACHE_TTL", "30")))
CONTROL_PLANE_RECONCILE_TTL = max(10, int(os.getenv("CONTROL_PLANE_RECONCILE_TTL", "45")))
MAX_BUILD_EVENTS_PER_PROJECT = max(100, int(os.getenv("MAX_BUILD_EVENTS_PER_PROJECT", "800")))
BUILD_EVENT_RETENTION_DAYS = max(7, int(os.getenv("BUILD_EVENT_RETENTION_DAYS", "120")))
INIT_JOB_MAX_WAIT_SECONDS = max(120, int(os.getenv("INIT_JOB_MAX_WAIT_SECONDS", "900")))
INIT_JOB_POLL_SECONDS = max(2, int(os.getenv("INIT_JOB_POLL_SECONDS", "4")))
INIT_JOB_STALE_SECONDS = max(
    180,
    int(os.getenv("INIT_JOB_STALE_SECONDS", str(INIT_JOB_MAX_WAIT_SECONDS))),
)

DEFAULT_HOSTING_LOCATIONS = [
    {"code": "uk1", "label": "London (UK1)", "region": "Europe", "ovh_region": "UK1"},
    {"code": "mil", "label": "Milan (EU-SOUTH-MIL)", "region": "Europe", "ovh_region": "MIL"},
    {"code": "rbx", "label": "Roubaix (RBX-A)", "region": "Europe", "ovh_region": "RBX"},
    {"code": "par", "label": "Paris (EU-WEST-PAR)", "region": "Europe", "ovh_region": "PAR"},
    {"code": "de1", "label": "Frankfurt (DE1)", "region": "Europe", "ovh_region": "DE1"},
    {"code": "waw1", "label": "Warsaw (WAW1)", "region": "Europe", "ovh_region": "WAW1"},
    {"code": "sbg5", "label": "Strasbourg (SBG5)", "region": "Europe", "ovh_region": "SBG5"},
    {"code": "bhs5", "label": "Beauharnois (BHS5)", "region": "Americas", "ovh_region": "BHS5"},
    {"code": "gra9", "label": "Gravelines (GRA9)", "region": "Europe", "ovh_region": "GRA9"},
]

DEFAULT_HOSTING_LOCATION_MAP = {item["code"]: item for item in DEFAULT_HOSTING_LOCATIONS}

OVH_ENDPOINT_BASE_URLS = {
    "ovh-eu": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
    "ovh-us": "https://api.us.ovhcloud.com/1.0",
}

OVH_REGION_ALIASES = {
    "RBX-A": "RBX",
    "EU-WEST-PAR": "PAR",
    "EU-SOUTH-MIL": "MIL",
    "BHS": "BHS5",
    "GRA": "GRA9",
}

OVH_REGION_LABELS = {
    "UK1": "London (UK1)",
    "MIL": "Milan (EU-SOUTH-MIL)",
    "RBX": "Roubaix (RBX-A)",
    "PAR": "Paris (EU-WEST-PAR)",
    "DE1": "Frankfurt (DE1)",
    "WAW1": "Warsaw (WAW1)",
    "SBG5": "Strasbourg (SBG5)",
    "BHS5": "Beauharnois (BHS5)",
    "GRA9": "Gravelines (GRA9)",
}

OVH_REGION_GROUPS = {
    "UK1": "Europe",
    "MIL": "Europe",
    "RBX": "Europe",
    "PAR": "Europe",
    "DE1": "Europe",
    "WAW1": "Europe",
    "SBG5": "Europe",
    "GRA9": "Europe",
    "BHS5": "Americas",
}

OVH_REGION_ORDER = [
    "UK1",
    "MIL",
    "RBX",
    "PAR",
    "DE1",
    "WAW1",
    "SBG5",
    "BHS5",
    "GRA9",
]

OVH_REGION_PROBE_URLS = {
    "BHS5": "https://s3.bhs.io.cloud.ovh.net",
    "DE1": "https://s3.de.io.cloud.ovh.net",
    "GRA9": "https://s3.gra.io.cloud.ovh.net",
    "PAR": "https://s3.gra.io.cloud.ovh.net",
    "MIL": "https://s3.gra.io.cloud.ovh.net",
    "RBX": "https://s3.rbx.io.cloud.ovh.net",
    "SBG5": "https://s3.sbg.io.cloud.ovh.net",
    "UK1": "https://s3.de.io.cloud.ovh.net",
    "WAW1": "https://s3.waw.io.cloud.ovh.net",
}

OVH_QUOTA_CACHE = {
    "fetched_at": 0.0,
    "data": None,
    "error": "",
}

MASTER_ADMIN_EMAILS = {
    email.strip().lower()
    for email in [*MASTER_ADMIN_EMAILS_RAW.split(","), MASTER_ADMIN_EMAIL]
    if email and "@" in email
}

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "nexora.db")

if not CONTROL_PLANE_DB_NAME:
    CONTROL_PLANE_DB_NAME = "nexora_control_plane"
CONTROL_PLANE_DB_NAME = re.sub(r"[^a-zA-Z0-9_]", "_", CONTROL_PLANE_DB_NAME)


def derived_control_plane_database_url() -> tuple[str, bool]:
    if CONTROL_PLANE_DATABASE_URL:
        return CONTROL_PLANE_DATABASE_URL, False
    if DB_HOST and DB_USER and DB_PASSWORD:
        user = quote(DB_USER, safe="")
        password = quote(DB_PASSWORD, safe="")
        sslmode = quote(DB_SSLMODE, safe="")
        db_name = quote(CONTROL_PLANE_DB_NAME, safe="")
        return (
            f"postgresql+psycopg2://{user}:{password}@{DB_HOST}:{DB_PORT}/{db_name}?sslmode={sslmode}",
            True,
        )
    return f"sqlite:///{DB_PATH}", False


def ensure_control_plane_postgres_database():
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname="postgres",
        sslmode=DB_SSLMODE,
        connect_timeout=DB_CONNECT_TIMEOUT,
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (CONTROL_PLANE_DB_NAME,),
        )
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute(
                sql.SQL("CREATE DATABASE {} OWNER {};").format(
                    sql.Identifier(CONTROL_PLANE_DB_NAME),
                    sql.Identifier(DB_USER),
                )
            )
    conn.close()


SQLALCHEMY_DATABASE_URL, DERIVED_CP_DB_FROM_OVH_POSTGRES = derived_control_plane_database_url()
CONTROL_PLANE_DB_BOOT_ERROR = ""
if DERIVED_CP_DB_FROM_OVH_POSTGRES:
    try:
        ensure_control_plane_postgres_database()
    except Exception as exc:
        CONTROL_PLANE_DB_BOOT_ERROR = str(exc)[:220]
        SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"
        DERIVED_CP_DB_FROM_OVH_POSTGRES = False

IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith("sqlite:")
if IS_SQLITE:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=300,
    )

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

PROJECT_RECONCILE_STATE = {
    "fetched_at": 0.0,
    "error": "",
    "imported_projects": 0,
    "imported_envs": 0,
    "imported_domains": 0,
}
PROJECT_RECONCILE_LOCK = threading.Lock()
GITHUB_INSTALL_SYNC_TTL_SECONDS = max(15, int(os.getenv("GITHUB_INSTALL_SYNC_TTL_SECONDS", "60")))
GITHUB_INSTALL_SYNC_STATE = {"fetched_at": 0.0}
GITHUB_INSTALL_SYNC_LOCK = threading.Lock()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    github_id = Column(String, unique=True, nullable=False)
    username = Column(String, unique=True, nullable=False)
    email = Column(String, nullable=True)
    name = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    projects = relationship("Project", back_populates="owner")


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=True)
    dev_branch = Column(String, nullable=True)
    staging_branch = Column(String, nullable=True)
    prod_branch = Column(String, nullable=True)
    workers = Column(Integer, default=DEFAULT_WORKERS)
    storage_gb = Column(Integer, default=DEFAULT_STORAGE_GB)
    staging_slots = Column(Integer, default=DEFAULT_STAGING_SLOTS)
    subscription_code = Column(String, nullable=True)
    odoo_version = Column(String, nullable=True)
    hosting_location = Column(String, nullable=True)
    hosting_ping_ms = Column(Integer, nullable=True)
    repo_full_name = Column(String, nullable=True)
    repo_id = Column(String, nullable=True)
    installation_id = Column(String, nullable=True)
    status = Column(String, nullable=True)
    last_error = Column(String, nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    owner = relationship("User", back_populates="projects")
    envs = relationship("Environment", back_populates="project", cascade="all, delete-orphan")


class Environment(Base):
    __tablename__ = "environments"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    name = Column(String, nullable=False)
    host = Column(String, nullable=False)
    db_name = Column(String, nullable=False)
    workers = Column(Integer, nullable=True)
    storage_gb = Column(Integer, nullable=True)
    odoo_version = Column(String, nullable=True)
    status = Column(String, nullable=True)
    last_error = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="envs")
    domains = relationship("Domain", back_populates="environment", cascade="all, delete-orphan")


class Domain(Base):
    __tablename__ = "domains"
    id = Column(Integer, primary_key=True)
    env_id = Column(Integer, ForeignKey("environments.id"), nullable=False)
    host = Column(String, unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    environment = relationship("Environment", back_populates="domains")


class Installation(Base):
    __tablename__ = "installations"
    id = Column(Integer, primary_key=True)
    installation_id = Column(String, unique=True, nullable=False)
    account_login = Column(String, nullable=False)
    account_id = Column(String, nullable=True)
    installed_at = Column(DateTime, default=datetime.utcnow)


class Repository(Base):
    __tablename__ = "repositories"
    id = Column(Integer, primary_key=True)
    installation_id = Column(String, nullable=False)
    repo_id = Column(String, nullable=False)
    full_name = Column(String, unique=True, nullable=False)
    default_branch = Column(String, nullable=True)
    private = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class BuildEvent(Base):
    __tablename__ = "build_events"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, nullable=False)
    env = Column(String, nullable=True)
    branch = Column(String, nullable=True)
    sha = Column(String, nullable=True)
    status = Column(String, nullable=True)
    message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProjectTombstone(Base):
    __tablename__ = "project_tombstones"
    id = Column(Integer, primary_key=True)
    slug = Column(String, unique=True, nullable=False)
    reason = Column(String, nullable=True)
    deleted_at = Column(DateTime, default=datetime.utcnow)


Base.metadata.create_all(engine)


def ensure_schema():
    with engine.begin() as conn:
        inspector = inspect(conn)
        user_cols = {col["name"] for col in inspector.get_columns("users")}
        for name, ddl in [
            ("email", "email TEXT"),
            ("is_admin", "is_admin BOOLEAN"),
        ]:
            if name not in user_cols:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {ddl}"))

        inspector = inspect(conn)
        cols = {col["name"] for col in inspector.get_columns("projects")}
        for name, ddl in [
            ("display_name", "display_name TEXT"),
            ("dev_branch", "dev_branch TEXT"),
            ("staging_branch", "staging_branch TEXT"),
            ("prod_branch", "prod_branch TEXT"),
            ("workers", "workers INTEGER"),
            ("storage_gb", "storage_gb INTEGER"),
            ("staging_slots", "staging_slots INTEGER"),
            ("subscription_code", "subscription_code TEXT"),
            ("odoo_version", "odoo_version TEXT"),
            ("hosting_location", "hosting_location TEXT"),
            ("hosting_ping_ms", "hosting_ping_ms INTEGER"),
            ("repo_full_name", "repo_full_name TEXT"),
            ("repo_id", "repo_id TEXT"),
            ("installation_id", "installation_id TEXT"),
            ("status", "status TEXT"),
            ("last_error", "last_error TEXT"),
        ]:
            if name not in cols:
                conn.execute(text(f"ALTER TABLE projects ADD COLUMN {ddl}"))

        conn.execute(text("UPDATE projects SET display_name = slug WHERE display_name IS NULL"))
        conn.execute(
            text("UPDATE projects SET dev_branch = :v WHERE dev_branch IS NULL"),
            {"v": DEFAULT_DEV_BRANCH},
        )
        conn.execute(
            text("UPDATE projects SET staging_branch = :v WHERE staging_branch IS NULL"),
            {"v": DEFAULT_STAGING_BRANCH},
        )
        conn.execute(
            text("UPDATE projects SET prod_branch = :v WHERE prod_branch IS NULL"),
            {"v": DEFAULT_PROD_BRANCH},
        )
        conn.execute(
            text("UPDATE projects SET workers = :v WHERE workers IS NULL"),
            {"v": DEFAULT_WORKERS},
        )
        conn.execute(
            text("UPDATE projects SET storage_gb = :v WHERE storage_gb IS NULL"),
            {"v": DEFAULT_STORAGE_GB},
        )
        conn.execute(
            text("UPDATE projects SET staging_slots = :v WHERE staging_slots IS NULL"),
            {"v": DEFAULT_STAGING_SLOTS},
        )
        conn.execute(
            text("UPDATE projects SET odoo_version = :v WHERE odoo_version IS NULL"),
            {"v": DEFAULT_ODOO_VERSION},
        )
        conn.execute(
            text("UPDATE projects SET status = :v WHERE status IS NULL"),
            {"v": "active"},
        )

        inspector = inspect(conn)
        env_cols = {col["name"] for col in inspector.get_columns("environments")}
        for name, ddl in [
            ("workers", "workers INTEGER"),
            ("storage_gb", "storage_gb INTEGER"),
            ("odoo_version", "odoo_version TEXT"),
            ("status", "status TEXT"),
            ("last_error", "last_error TEXT"),
        ]:
            if name not in env_cols:
                conn.execute(text(f"ALTER TABLE environments ADD COLUMN {ddl}"))

        conn.execute(
            text("UPDATE environments SET workers = :v WHERE workers IS NULL"),
            {"v": DEFAULT_WORKERS},
        )
        conn.execute(
            text("UPDATE environments SET storage_gb = :v WHERE storage_gb IS NULL"),
            {"v": DEFAULT_STORAGE_GB},
        )
        conn.execute(
            text("UPDATE environments SET odoo_version = :v WHERE odoo_version IS NULL"),
            {"v": DEFAULT_ODOO_VERSION},
        )
        conn.execute(
            text("UPDATE environments SET status = :v WHERE status IS NULL"),
            {"v": "active"},
        )
        cutoff = datetime.utcnow() - timedelta(days=BUILD_EVENT_RETENTION_DAYS)
        conn.execute(
            text("DELETE FROM build_events WHERE created_at < :cutoff"),
            {"cutoff": cutoff},
        )


ensure_schema()

app = FastAPI()


@app.get("/health")
def health():
    payload = {
        "status": "ok",
        "control_plane_db": "sqlite" if IS_SQLITE else "postgres",
    }
    if CONTROL_PLANE_DB_BOOT_ERROR:
        payload["warning"] = f"control-plane DB fallback: {CONTROL_PLANE_DB_BOOT_ERROR}"
    return payload


@app.get("/ready")
def ready():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="storage unavailable")
    return {"status": "ready"}
# OAuth state is stored in the session cookie; require HTTPS and use lax SameSite.
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET, https_only=True, same_site="lax")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

oauth = OAuth()
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")

if GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET:
    oauth.register(
        name="github",
        client_id=GITHUB_CLIENT_ID,
        client_secret=GITHUB_CLIENT_SECRET,
        access_token_url="https://github.com/login/oauth/access_token",
        authorize_url="https://github.com/login/oauth/authorize",
        api_base_url="https://api.github.com/",
        client_kwargs={"scope": "read:user user:email"},
    )


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")


def upsert_project_tombstone(db, slug: str, reason: str = ""):
    clean = (slug or "").strip().lower()
    if not clean:
        return
    row = db.query(ProjectTombstone).filter(ProjectTombstone.slug == clean).first()
    if row:
        row.reason = (reason or row.reason or "")[:200]
        row.deleted_at = datetime.utcnow()
        return
    db.add(
        ProjectTombstone(
            slug=clean,
            reason=(reason or "")[:200],
        )
    )


def remove_project_tombstone(db, slug: str):
    clean = (slug or "").strip().lower()
    if not clean:
        return
    row = db.query(ProjectTombstone).filter(ProjectTombstone.slug == clean).first()
    if row:
        db.delete(row)


def request_host(request: Request) -> str:
    raw = (request.headers.get("host") or "").strip().lower()
    if not raw:
        return ""
    return raw.split(":", 1)[0]


def redirect_with_error(path: str, message: str, extra: Optional[dict] = None):
    params = {"error": message}
    if extra:
        params.update(extra)
    return RedirectResponse(f"{path}?{urlencode(params)}", status_code=303)


def redirect_with_notice(path: str, message: str, extra: Optional[dict] = None):
    params = {"notice": message}
    if extra:
        params.update(extra)
    return RedirectResponse(f"{path}?{urlencode(params)}", status_code=303)


@app.middleware("http")
async def deleted_subdomain_fallback(request: Request, call_next):
    host = request_host(request)
    app_host = f"app.{BASE_DOMAIN}".lower()
    base = f".{BASE_DOMAIN}".lower()

    # If a deleted/unknown project subdomain reaches control-plane fallback ingress,
    # redirect users to app dashboard with an explicit message.
    if host and host != app_host and host.endswith(base):
        notice = "This project URL is deleted or inactive. Use app.nexora.red to create/open active projects."
        return RedirectResponse(f"https://{app_host}/?{urlencode({'notice': notice})}", status_code=302)

    return await call_next(request)


def normalize_hosting_location(code: str) -> str:
    code = (code or "").strip().lower()
    if not code:
        return ""
    code = re.sub(r"[^a-z0-9-]", "-", code)
    code = re.sub(r"-+", "-", code).strip("-")
    if not code:
        return ""
    if len(code) > 48:
        return ""
    return code


def parse_ping_ms(value: str) -> Optional[int]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        ping = int(float(raw))
    except Exception:
        return None
    return max(1, min(20000, ping))


def canonical_ovh_region(region: str) -> str:
    raw = (region or "").strip().upper()
    if not raw:
        return ""
    if raw in OVH_REGION_ALIASES:
        return OVH_REGION_ALIASES[raw]
    return raw


def hosting_location_for_region(region: str) -> dict:
    canonical = canonical_ovh_region(region)
    if not canonical:
        return {}
    code = normalize_hosting_location(canonical)
    label = OVH_REGION_LABELS.get(canonical, canonical)
    region_group = OVH_REGION_GROUPS.get(canonical, "OVH")
    probe_url = OVH_REGION_PROBE_URLS.get(canonical)
    if not probe_url:
        if region_group == "Americas":
            probe_url = OVH_REGION_PROBE_URLS.get("BHS5", "")
        else:
            probe_url = OVH_REGION_PROBE_URLS.get("GRA9", "")
    return {
        "code": code,
        "label": label,
        "region": region_group,
        "ovh_region": canonical,
        "probe_url": probe_url,
    }


def default_hosting_locations() -> list[dict]:
    locations = []
    for item in DEFAULT_HOSTING_LOCATIONS:
        ovh_region = item.get("ovh_region") or item.get("code") or ""
        location = hosting_location_for_region(ovh_region)
        if location:
            locations.append(location)
    return locations


def hosting_location_map_from_list(hosting_locations: list[dict]) -> dict:
    return {
        item["code"]: item
        for item in hosting_locations
        if isinstance(item, dict) and item.get("code")
    }


def hosting_location_label(code: str, hosting_location_map: Optional[dict] = None) -> str:
    normalized = normalize_hosting_location(code)
    if not normalized:
        return "Auto"
    location_map = hosting_location_map or DEFAULT_HOSTING_LOCATION_MAP
    item = location_map.get(normalized) if isinstance(location_map, dict) else None
    if item and item.get("label"):
        return str(item["label"])
    return normalized.upper()


def parse_hosting_probe_results(raw: str) -> dict[str, int]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, int] = {}
    for code, value in parsed.items():
        normalized = normalize_hosting_location(str(code))
        if not normalized:
            continue
        ping = parse_ping_ms(str(value))
        if ping is None:
            continue
        result[normalized] = ping
    return result


def hosting_location_order(code: str, hosting_locations: Optional[list[dict]] = None) -> int:
    normalized = normalize_hosting_location(code)
    source = hosting_locations or default_hosting_locations()
    for idx, item in enumerate(source):
        if item["code"] == normalized:
            return idx
    return 10_000


def best_location_by_ping(
    codes: list[str],
    probe_results: dict[str, int],
    hosting_locations: Optional[list[dict]] = None,
) -> str:
    if not codes:
        return ""

    def sort_key(code: str):
        ping = probe_results.get(code)
        has_ping = isinstance(ping, int)
        return (
            0 if has_ping else 1,
            ping if has_ping else 10_000_000,
            hosting_location_order(code, hosting_locations),
        )

    return sorted(codes, key=sort_key)[0]


def ovh_quota_checks_enabled() -> bool:
    return bool(
        OVH_PROJECT_ID
        and OVH_APPLICATION_KEY
        and OVH_APPLICATION_SECRET
        and OVH_CONSUMER_KEY
    )


def ovh_api_base_url() -> str:
    return OVH_ENDPOINT_BASE_URLS.get(OVH_ENDPOINT, OVH_ENDPOINT_BASE_URLS["ovh-ca"])


def ovh_auth_time(base_url: str) -> int:
    resp = httpx.get(f"{base_url}/auth/time", timeout=max(3, min(OVH_API_TIMEOUT, 10)))
    resp.raise_for_status()
    return int((resp.text or "").strip())


def ovh_api_request(method: str, path: str, payload: Optional[dict] = None):
    if not ovh_quota_checks_enabled():
        raise RuntimeError("OVH quota checks are not configured")

    base_url = ovh_api_base_url()
    method = method.upper()
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload is not None else ""
    url = f"{base_url}{path}"
    timestamp = ovh_auth_time(base_url)
    signature_base = f"{OVH_APPLICATION_SECRET}+{OVH_CONSUMER_KEY}+{method}+{url}+{body}+{timestamp}"
    signature = "$1$" + hashlib.sha1(signature_base.encode("utf-8")).hexdigest()

    headers = {
        "X-Ovh-Application": OVH_APPLICATION_KEY,
        "X-Ovh-Consumer": OVH_CONSUMER_KEY,
        "X-Ovh-Timestamp": str(timestamp),
        "X-Ovh-Signature": signature,
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"

    resp = httpx.request(
        method,
        url,
        headers=headers,
        content=body.encode("utf-8") if body else None,
        timeout=OVH_API_TIMEOUT,
    )
    if resp.status_code >= 400:
        msg = (resp.text or "").strip().replace("\n", " ")
        raise RuntimeError(f"OVH API {resp.status_code}: {msg[:200] or 'request failed'}")
    if not (resp.text or "").strip():
        return None
    try:
        return resp.json()
    except Exception:
        return None


def ovh_project_quotas(force: bool = False) -> tuple[Optional[list], str]:
    if not ovh_quota_checks_enabled():
        return None, "disabled"

    now = time.time()
    cached_at = float(OVH_QUOTA_CACHE.get("fetched_at") or 0.0)
    cached_data = OVH_QUOTA_CACHE.get("data")
    cached_error = str(OVH_QUOTA_CACHE.get("error") or "")
    if not force and (now - cached_at) < OVH_QUOTA_CACHE_TTL:
        if isinstance(cached_data, list):
            return cached_data, ""
        if cached_error:
            return None, cached_error

    try:
        encoded_project = quote(OVH_PROJECT_ID, safe="")
        data = ovh_api_request("GET", f"/cloud/project/{encoded_project}/quota")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected OVH quota response")
        OVH_QUOTA_CACHE["fetched_at"] = now
        OVH_QUOTA_CACHE["data"] = data
        OVH_QUOTA_CACHE["error"] = ""
        return data, ""
    except Exception as exc:
        message = str(exc)[:220]
        OVH_QUOTA_CACHE["fetched_at"] = now
        OVH_QUOTA_CACHE["data"] = None
        OVH_QUOTA_CACHE["error"] = message
        return None, message


def quota_remaining(quota_block: dict, max_key: str, used_key: str) -> Optional[int]:
    if not isinstance(quota_block, dict):
        return None
    try:
        maximum = int(quota_block.get(max_key) or 0)
        used = int(quota_block.get(used_key) or 0)
    except Exception:
        return None
    return maximum - used


def summarize_region_quota(quota: dict) -> dict:
    instance = quota.get("instance") or {}
    volume = quota.get("volume") or {}
    remaining_instances = quota_remaining(instance, "maxInstances", "usedInstances")
    remaining_cores = quota_remaining(instance, "maxCores", "usedCores")
    remaining_ram = quota_remaining(instance, "maxRam", "usedRAM")
    remaining_volume_count = quota_remaining(volume, "maxVolumeCount", "volumeCount")
    remaining_volume_gb = quota_remaining(volume, "maxGigabytes", "usedGigabytes")

    required = [remaining_instances, remaining_cores, remaining_ram]
    available = all((value is not None and value > 0) for value in required)
    if remaining_volume_count is not None:
        available = available and remaining_volume_count > 0
    if remaining_volume_gb is not None:
        available = available and remaining_volume_gb > 0

    return {
        "region": str(quota.get("region") or ""),
        "available": bool(available),
        "remaining_instances": remaining_instances,
        "remaining_cores": remaining_cores,
        "remaining_ram": remaining_ram,
        "remaining_volume_count": remaining_volume_count,
        "remaining_volume_gb": remaining_volume_gb,
    }


def hosting_capacity_snapshot(force: bool = False) -> dict:
    fallback_hosting_locations = default_hosting_locations()
    fallback_location_map = hosting_location_map_from_list(fallback_hosting_locations)
    default_locations = {
        item["code"]: {
            "available": True,
            "region": item.get("ovh_region", ""),
            "reason": "",
            "checked": False,
        }
        for item in fallback_hosting_locations
    }

    if not ovh_quota_checks_enabled():
        for code in default_locations:
            default_locations[code]["reason"] = "OVH quota check disabled"
        return {
            "mode": "disabled",
            "message": "OVH quota check disabled",
            "locations": default_locations,
            "hosting_locations": fallback_hosting_locations,
            "hosting_location_map": fallback_location_map,
        }

    quotas, error = ovh_project_quotas(force=force)
    if not isinstance(quotas, list):
        for code in default_locations:
            default_locations[code]["reason"] = "OVH quota lookup failed"
        return {
            "mode": "error",
            "message": error or "OVH quota lookup failed",
            "locations": default_locations,
            "hosting_locations": fallback_hosting_locations,
            "hosting_location_map": fallback_location_map,
        }

    region_summaries = [
        summarize_region_quota(item or {})
        for item in quotas
        if isinstance(item, dict) and (item.get("region") or "")
    ]
    if not region_summaries:
        for code in default_locations:
            default_locations[code]["reason"] = "No OVH regions returned"
        return {
            "mode": "error",
            "message": "No OVH regions returned for this project",
            "locations": default_locations,
            "hosting_locations": fallback_hosting_locations,
            "hosting_location_map": fallback_location_map,
        }

    hosting_locations: list[dict] = []
    locations: dict[str, dict] = {}
    seen_codes = set()
    for summary in region_summaries:
        location_item = hosting_location_for_region(summary["region"])
        code = location_item.get("code", "")
        if not code:
            continue
        if code not in seen_codes:
            hosting_locations.append(location_item)
            seen_codes.add(code)
        reason = "Free quota available" if summary["available"] else "No free quota"
        locations[code] = {
            "available": bool(summary["available"]),
            "region": summary["region"],
            "reason": reason,
            "checked": True,
            "remaining_instances": summary["remaining_instances"],
            "remaining_cores": summary["remaining_cores"],
            "remaining_ram": summary["remaining_ram"],
            "remaining_volume_count": summary["remaining_volume_count"],
            "remaining_volume_gb": summary["remaining_volume_gb"],
        }

    order_index = {region: idx for idx, region in enumerate(OVH_REGION_ORDER)}
    hosting_locations.sort(
        key=lambda item: (
            order_index.get(canonical_ovh_region(item.get("ovh_region", "")), 999),
            item.get("label", ""),
        )
    )
    hosting_location_map = hosting_location_map_from_list(hosting_locations)

    return {
        "mode": "active",
        "message": "",
        "locations": locations,
        "hosting_locations": hosting_locations,
        "hosting_location_map": hosting_location_map,
    }


def hosting_catalog(force: bool = False) -> tuple[list[dict], dict, dict]:
    snapshot = hosting_capacity_snapshot(force=force)
    hosting_locations = snapshot.get("hosting_locations")
    if not isinstance(hosting_locations, list) or not hosting_locations:
        hosting_locations = default_hosting_locations()
    hosting_location_map = snapshot.get("hosting_location_map")
    if not isinstance(hosting_location_map, dict) or not hosting_location_map:
        hosting_location_map = hosting_location_map_from_list(hosting_locations)
    return hosting_locations, hosting_location_map, snapshot


def init_job_logs_tail(slug: str, env_name: str, lines: int = 200) -> str:
    job_name = init_job_name(slug, env_name)
    pod_lookup = run_kubectl(
        ["get", "pods", "-l", f"job-name={job_name}", "--sort-by=.metadata.creationTimestamp", "-o", "name"],
        namespace=ODOO_NAMESPACE,
        timeout=max(KUBECTL_GET_TIMEOUT, 12),
    )
    lookup_err = (pod_lookup.stderr or b"").decode("utf-8", errors="replace").strip()
    if pod_lookup.returncode != 0:
        if "forbidden" in lookup_err.lower():
            return "Init logs permission is missing (RBAC)."
        return ""

    pod_lines = [
        line.strip()
        for line in (pod_lookup.stdout or b"").decode("utf-8", errors="replace").splitlines()
        if line.strip().startswith("pod/")
    ]
    if not pod_lines:
        return ""

    pod_name = pod_lines[-1].split("/", 1)[1]
    result = run_kubectl(
        ["logs", f"pod/{pod_name}", f"--tail={max(20, min(lines, 1000))}"],
        namespace=ODOO_NAMESPACE,
        timeout=max(KUBECTL_GET_TIMEOUT, 12),
    )
    stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        if "forbidden" in stderr.lower():
            return "Init logs permission is missing (RBAC)."
        return ""
    return stdout


def env_logs_tail(slug: str, env_name: str, lines: int = 200) -> str:
    label = f"app=odoo,project={slug},env={env_name}"
    pod_lookup = run_kubectl(
        ["get", "pods", "-l", label, "--sort-by=.metadata.creationTimestamp", "-o", "name"],
        namespace=ODOO_NAMESPACE,
        timeout=max(KUBECTL_GET_TIMEOUT, 12),
    )
    lookup_err = (pod_lookup.stderr or b"").decode("utf-8", errors="replace").strip()
    if pod_lookup.returncode != 0:
        if "forbidden" in lookup_err.lower():
            return "Logs permission is missing (RBAC). Apply updated control-plane RBAC and retry."
        if lookup_err:
            return f"Unable to fetch logs: {lookup_err}"
        return "Unable to fetch logs."

    pod_lines = [
        line.strip()
        for line in (pod_lookup.stdout or b"").decode("utf-8", errors="replace").splitlines()
        if line.strip().startswith("pod/")
    ]
    if not pod_lines:
        init_logs = init_job_logs_tail(slug, env_name, lines=lines)
        if init_logs:
            return (
                f"No runtime pod yet for {env_name}. Showing init job logs.\n\n"
                f"{init_logs}"
            )
        return f"No runtime pod yet for {env_name}. Environment is still provisioning."
    pod_name = pod_lines[-1].split("/", 1)[1]

    result = run_kubectl(
        ["logs", f"pod/{pod_name}", f"--tail={max(20, min(lines, 1000))}"],
        namespace=ODOO_NAMESPACE,
        timeout=max(KUBECTL_GET_TIMEOUT, 12),
    )
    stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        if "NotFound" in stderr:
            init_logs = init_job_logs_tail(slug, env_name, lines=lines)
            if init_logs:
                return (
                    f"No runtime pod found for {env_name} yet. Showing init job logs.\n\n"
                    f"{init_logs}"
                )
            return f"No runtime pod found for {env_name} yet."
        if "forbidden" in stderr.lower():
            return "Logs permission is missing (RBAC). Apply updated control-plane RBAC and retry."
        if stderr:
            return f"Unable to fetch logs: {stderr}"
        return "Unable to fetch logs."
    return stdout or "No logs yet."


def github_app_enabled() -> bool:
    return bool(GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY and GITHUB_WEBHOOK_SECRET)


def github_app_jwt() -> str:
    if not GITHUB_APP_ID or not GITHUB_APP_PRIVATE_KEY:
        raise RuntimeError("GitHub App credentials are missing")
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": GITHUB_APP_ID}
    token = jwt.encode(payload, GITHUB_APP_PRIVATE_KEY, algorithm="RS256")
    return token


def github_app_headers() -> dict:
    return {
        "Authorization": f"Bearer {github_app_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_installation_token(installation_id: str) -> str:
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    resp = httpx.post(url, headers=github_app_headers(), timeout=20)
    resp.raise_for_status()
    return resp.json()["token"]


def github_install_url() -> str:
    return f"https://github.com/apps/{GITHUB_APP_SLUG}/installations/new"


def verify_github_signature(secret: str, body: bytes, signature: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)


def is_master_admin_user(email: str) -> bool:
    mail = (email or "").strip().lower()
    if mail and mail in MASTER_ADMIN_EMAILS:
        return True
    return False


async def github_primary_email(token: dict, profile: dict) -> str:
    direct = (profile.get("email") or "").strip().lower()
    if direct:
        return direct
    try:
        resp = await oauth.github.get("user/emails", token=token)
        payload = resp.json()
    except Exception:
        return ""
    if not isinstance(payload, list):
        return ""
    verified_primary = next(
        (
            item
            for item in payload
            if isinstance(item, dict)
            and item.get("verified")
            and item.get("primary")
            and item.get("email")
        ),
        None,
    )
    if verified_primary:
        return str(verified_primary.get("email") or "").strip().lower()
    verified_any = next(
        (
            item
            for item in payload
            if isinstance(item, dict)
            and item.get("verified")
            and item.get("email")
        ),
        None,
    )
    if verified_any:
        return str(verified_any.get("email") or "").strip().lower()
    first = next(
        (item for item in payload if isinstance(item, dict) and item.get("email")),
        None,
    )
    if not first:
        return ""
    return str(first.get("email") or "").strip().lower()


def sync_installation_repos(installation_id: str):
    token = github_installation_token(installation_id)
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = "https://api.github.com/installation/repositories"
    resp = httpx.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    repos = data.get("repositories", [])
    with db_session() as db:
        for repo in repos:
            full_name = repo.get("full_name")
            repo_id = str(repo.get("id"))
            if not full_name or not repo_id:
                continue
            existing = db.query(Repository).filter(Repository.full_name == full_name).first()
            if existing:
                existing.installation_id = installation_id
                existing.default_branch = repo.get("default_branch")
                existing.private = bool(repo.get("private"))
            else:
                db.add(
                    Repository(
                        installation_id=installation_id,
                        repo_id=repo_id,
                        full_name=full_name,
                        default_branch=repo.get("default_branch"),
                        private=bool(repo.get("private")),
                    )
                )
        db.commit()


def sync_installations_from_github(force: bool = False, sync_repos: bool = False) -> int:
    if not github_app_enabled():
        return 0

    now = time.time()
    if not force and (now - float(GITHUB_INSTALL_SYNC_STATE.get("fetched_at") or 0.0)) < GITHUB_INSTALL_SYNC_TTL_SECONDS:
        return 0

    with GITHUB_INSTALL_SYNC_LOCK:
        now = time.time()
        if not force and (now - float(GITHUB_INSTALL_SYNC_STATE.get("fetched_at") or 0.0)) < GITHUB_INSTALL_SYNC_TTL_SECONDS:
            return 0

        resp = httpx.get(
            "https://api.github.com/app/installations",
            headers=github_app_headers(),
            params={"per_page": 100},
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
        items = payload if isinstance(payload, list) else []

        imported = 0
        install_ids: list[str] = []
        with db_session() as db:
            for item in items:
                if not isinstance(item, dict):
                    continue
                installation_id = str(item.get("id") or "").strip()
                account = item.get("account") or {}
                account_login = str((account or {}).get("login") or "").strip()
                account_id = str((account or {}).get("id") or "").strip()
                if not installation_id or not account_login:
                    continue

                install_ids.append(installation_id)
                existing = db.query(Installation).filter(
                    Installation.installation_id == installation_id
                ).first()
                if existing:
                    existing.account_login = account_login
                    existing.account_id = account_id
                else:
                    db.add(
                        Installation(
                            installation_id=installation_id,
                            account_login=account_login,
                            account_id=account_id,
                        )
                    )
                    imported += 1
            db.commit()

        if sync_repos:
            for installation_id in install_ids:
                try:
                    sync_installation_repos(installation_id)
                except Exception:
                    pass

        GITHUB_INSTALL_SYNC_STATE["fetched_at"] = time.time()
        return imported


def candidate_installations_for_user(db, user: User) -> list[Installation]:
    if user.is_admin:
        return db.query(Installation).order_by(Installation.installed_at.desc()).all()

    scoped = (
        db.query(Installation)
        .filter(Installation.account_login == user.username)
        .order_by(Installation.installed_at.desc())
        .all()
    )
    if scoped:
        return scoped

    all_installs = db.query(Installation).order_by(Installation.installed_at.desc()).all()
    if len(all_installs) == 1:
        return all_installs
    return []


def github_installation_headers(installation_id: str) -> dict:
    token = github_installation_token(installation_id)
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_repo_get(headers: dict, repo_full_name: str) -> tuple[Optional[dict], str]:
    resp = httpx.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers, timeout=20)
    if resp.status_code == 404:
        return None, "not_found"
    if resp.status_code >= 400:
        msg = (resp.text or "").strip().replace("\n", " ")
        return None, msg[:220] or f"http {resp.status_code}"
    try:
        return resp.json(), ""
    except Exception:
        return None, "invalid github response"


def github_repo_create(headers: dict, account_login: str, repo_name: str) -> dict:
    payload = {"name": repo_name, "private": True, "auto_init": True}
    errors = []
    for url in [
        f"https://api.github.com/orgs/{account_login}/repos",
        "https://api.github.com/user/repos",
    ]:
        resp = httpx.post(url, headers=headers, json=payload, timeout=20)
        if resp.status_code in {200, 201}:
            return resp.json()
        msg = (resp.text or "").strip().replace("\n", " ")
        errors.append(f"{url}: {resp.status_code} {msg[:140]}")
    raise RuntimeError(" ; ".join(errors)[:280] or "repository creation failed")


def github_ensure_branches(headers: dict, repo_full_name: str, branches: list[str]) -> list[str]:
    repo, repo_err = github_repo_get(headers, repo_full_name)
    if not repo:
        raise RuntimeError(f"Unable to read repository metadata: {repo_err or 'unknown error'}")

    default_branch = str(repo.get("default_branch") or DEFAULT_PROD_BRANCH or "main").strip() or "main"
    ref_resp = httpx.get(
        f"https://api.github.com/repos/{repo_full_name}/git/ref/heads/{quote(default_branch, safe='')}",
        headers=headers,
        timeout=20,
    )
    if ref_resp.status_code >= 400:
        msg = (ref_resp.text or "").strip().replace("\n", " ")
        raise RuntimeError(f"Unable to read default branch ref: {msg[:180] or ref_resp.status_code}")
    base_sha = (
        (ref_resp.json().get("object") or {}).get("sha")
        if isinstance(ref_resp.json(), dict)
        else ""
    )
    if not base_sha:
        raise RuntimeError("Missing base commit SHA for default branch")

    created: list[str] = []
    seen = set()
    for raw_branch in branches:
        branch = (raw_branch or "").strip()
        if not branch or branch in seen or branch == default_branch:
            continue
        seen.add(branch)
        existing = httpx.get(
            f"https://api.github.com/repos/{repo_full_name}/git/ref/heads/{quote(branch, safe='')}",
            headers=headers,
            timeout=20,
        )
        if existing.status_code == 200:
            continue
        if existing.status_code != 404:
            msg = (existing.text or "").strip().replace("\n", " ")
            raise RuntimeError(f"Unable to verify branch '{branch}': {msg[:160] or existing.status_code}")

        create_resp = httpx.post(
            f"https://api.github.com/repos/{repo_full_name}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            timeout=20,
        )
        if create_resp.status_code in {200, 201, 422}:
            if create_resp.status_code in {200, 201}:
                created.append(branch)
            continue
        msg = (create_resp.text or "").strip().replace("\n", " ")
        raise RuntimeError(f"Unable to create branch '{branch}': {msg[:160] or create_resp.status_code}")

    return created


def connect_or_create_project_repo(
    *,
    project: Project,
    installation: Installation,
    repo_full_name: str,
    allow_create: bool,
) -> tuple[dict, bool, list[str]]:
    headers = github_installation_headers(installation.installation_id)
    target_full_name = repo_full_name.strip()
    if not target_full_name or "/" not in target_full_name:
        raise RuntimeError("Invalid repository name")

    repo, repo_err = github_repo_get(headers, target_full_name)
    created_repo = False
    if not repo and allow_create and repo_err == "not_found":
        owner = target_full_name.split("/", 1)[0].strip()
        name = target_full_name.split("/", 1)[1].strip()
        repo = github_repo_create(headers, owner, name)
        created_repo = True

    if not repo:
        raise RuntimeError(f"Unable to access repository with this installation: {repo_err or 'unknown error'}")

    resolved_full_name = str(repo.get("full_name") or target_full_name).strip()
    if not resolved_full_name or "/" not in resolved_full_name:
        raise RuntimeError("GitHub returned an invalid repository name")

    branches_created = github_ensure_branches(
        headers,
        resolved_full_name,
        [
            project.dev_branch or DEFAULT_DEV_BRANCH,
            project.staging_branch or DEFAULT_STAGING_BRANCH,
            project.prod_branch or DEFAULT_PROD_BRANCH,
        ],
    )
    return repo, created_repo, branches_created


def spawn_repo_bootstrap(project_id: int, user_id: int):
    def _worker():
        if not github_app_enabled():
            return
        try:
            sync_installations_from_github(force=True, sync_repos=True)
        except Exception:
            pass

        with db_session() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            user = db.query(User).filter(User.id == user_id).first()
            if not project or not user:
                return
            if project.repo_full_name:
                return
            installations = candidate_installations_for_user(db, user)
            if len(installations) != 1:
                if not installations:
                    record_build_event(
                        project.id,
                        "",
                        "",
                        "",
                        "repo_pending",
                        "GitHub repository not linked yet. Install the GitHub App, then connect a repository.",
                    )
                else:
                    record_build_event(
                        project.id,
                        "",
                        "",
                        "",
                        "repo_pending",
                        "Multiple GitHub installations detected. Select the installation in Project Settings and connect.",
                    )
                return
            installation = installations[0]
            chosen_installation_id = installation.installation_id
            repo_full_name = f"{installation.account_login}/{project.slug}"

        try:
            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                installation = db.query(Installation).filter(
                    Installation.installation_id == chosen_installation_id
                ).first()
                if not project or not installation or project.repo_full_name:
                    return

                repo, created_repo, branches_created = connect_or_create_project_repo(
                    project=project,
                    installation=installation,
                    repo_full_name=repo_full_name,
                    allow_create=True,
                )
                project.repo_full_name = str(repo.get("full_name") or repo_full_name)
                project.repo_id = str(repo.get("id") or "")
                project.installation_id = installation.installation_id
                db.commit()

                details = []
                if created_repo:
                    details.append("repository created")
                if branches_created:
                    details.append(f"branches created: {', '.join(branches_created)}")
                suffix = f" ({'; '.join(details)})" if details else ""
                record_build_event(
                    project.id,
                    "",
                    "",
                    "",
                    "repo_connected",
                    f"Repository linked: {project.repo_full_name}{suffix}",
                )
        except Exception as exc:
            record_build_event(
                project_id,
                "",
                "",
                "",
                "repo_failed",
                f"Repository auto-link failed: {str(exc)[:220]}",
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def record_build_event(project_id: int, env: str, branch: str, sha: str, status: str, message: str):
    with db_session() as db:
        db.add(
            BuildEvent(
                project_id=project_id,
                env=env,
                branch=branch,
                sha=sha,
                status=status,
                message=message,
            )
        )
        cutoff = datetime.utcnow() - timedelta(days=BUILD_EVENT_RETENTION_DAYS)
        db.query(BuildEvent).filter(BuildEvent.created_at < cutoff).delete(synchronize_session=False)
        overflow = (
            db.query(BuildEvent.id)
            .filter(BuildEvent.project_id == project_id)
            .order_by(BuildEvent.created_at.desc(), BuildEvent.id.desc())
            .offset(MAX_BUILD_EVENTS_PER_PROJECT)
            .all()
        )
        if overflow:
            db.query(BuildEvent).filter(
                BuildEvent.id.in_([row[0] for row in overflow if row and row[0]])
            ).delete(synchronize_session=False)
        db.commit()


def branch_from_ref(ref: str) -> str:
    if not ref:
        return ""
    if ref.startswith("refs/heads/"):
        return ref.split("/", 2)[2]
    return ref.split("/")[-1]


def env_for_project_branch(project: Project, branch: str) -> str:
    clean_branch = (branch or "").strip()
    if not clean_branch:
        return ""
    mapping = {
        (project.dev_branch or DEFAULT_DEV_BRANCH): "dev",
        (project.staging_branch or DEFAULT_STAGING_BRANCH): "staging",
        (project.prod_branch or DEFAULT_PROD_BRANCH): "prod",
    }
    return mapping.get(clean_branch, "")


def queue_env_provision_for_branch(project_id: int, env_name: str) -> str:
    if env_name not in {"dev", "staging", "prod"}:
        return "invalid_env"
    with db_session() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return "missing_project"
        env_obj = db.query(Environment).filter(
            Environment.project_id == project.id,
            Environment.name == env_name,
        ).first()

        if env_obj and (env_obj.status or "").strip().lower() == "provisioning":
            return "already_provisioning"
        if env_obj and (env_obj.status or "").strip().lower() == "active":
            return "already_active"

        if not env_obj:
            db.add(
                Environment(
                    project_id=project.id,
                    name=env_name,
                    host=host_for(project.slug, env_name),
                    db_name=db_name_for(project.slug, env_name),
                    workers=project.workers or DEFAULT_WORKERS,
                    storage_gb=project.storage_gb or DEFAULT_STORAGE_GB,
                    odoo_version=project.odoo_version or DEFAULT_ODOO_VERSION,
                    status="provisioning",
                    last_error="",
                )
            )
        else:
            env_obj.status = "provisioning"
            env_obj.last_error = ""
        db.commit()

    spawn_env_provision(project_id, env_name)
    return "queued"


def delete_env_for_branch(project_id: int, env_name: str) -> str:
    if env_name not in {"dev", "staging", "prod"}:
        return "invalid_env"

    with db_session() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return "missing_project"
        env_obj = db.query(Environment).filter(
            Environment.project_id == project.id,
            Environment.name == env_name,
        ).first()
        if not env_obj:
            return "missing_env"
        domain_hosts = [domain.host for domain in list(env_obj.domains)]
        env_id = env_obj.id
        project_slug = project.slug
        env_obj.status = "deleting"
        env_obj.last_error = ""
        db.commit()

    try:
        delete_env(project_slug, env_name, domain_hosts)
    except Exception as exc:
        set_env_status(project_id, env_name, "delete_failed", str(exc))
        return f"delete_failed:{str(exc)[:180]}"

    with db_session() as db:
        env_obj = db.query(Environment).filter(Environment.id == env_id).first()
        if env_obj:
            db.delete(env_obj)
            db.commit()
    return "deleted"


def promote_on_branch_merge(
    project_id: int,
    source_env: str,
    target_env: str,
) -> str:
    if source_env not in {"dev", "staging", "prod"} or target_env not in {"dev", "staging", "prod"}:
        return "invalid_env"
    if source_env == target_env:
        return "same_env"

    try:
        with db_session() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                return "missing_project"

            envs = {env.name: env for env in project.envs}
            if source_env not in envs:
                return "missing_source"
            source_state = (envs[source_env].status or "").strip().lower()
            if source_state in {"provisioning", "failed"}:
                return f"source_{source_state}"

            if target_env not in envs:
                source_settings = effective_env_settings(project, envs[source_env])
                temp_env = Environment(
                    workers=source_settings["workers"],
                    storage_gb=source_settings["storage_gb"],
                    odoo_version=source_settings["odoo_version"],
                )
                host, db_name = provision_env(project, target_env, temp_env)
                db.add(
                    Environment(
                        project_id=project.id,
                        name=target_env,
                        host=host,
                        db_name=db_name,
                        workers=source_settings["workers"],
                        storage_gb=source_settings["storage_gb"],
                        odoo_version=source_settings["odoo_version"],
                        status="active",
                        last_error="",
                    )
                )
                db.commit()

            promote_env(project, source_env, target_env)
            set_env_status(project.id, target_env, "active", "")
        return "promoted"
    except Exception as exc:
        return f"promote_failed:{str(exc)[:180]}"


def spawn_webhook_task(task):
    thread = threading.Thread(target=task, daemon=True)
    thread.start()


def run_kubectl(
    args: list[str],
    namespace: Optional[str] = None,
    *,
    input_bytes: Optional[bytes] = None,
    timeout: int = KUBECTL_MUTATE_TIMEOUT,
) -> subprocess.CompletedProcess:
    cmd = ["kubectl"]
    if namespace:
        cmd += ["-n", namespace]
    cmd += args
    try:
        return subprocess.run(
            cmd,
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"kubectl timed out after {timeout}s: {' '.join(cmd)}") from exc


def restart_env(slug: str, env: str):
    name = f"odoo-{slug}-{env}"
    run_kubectl(
        ["rollout", "restart", "deployment", name],
        namespace=ODOO_NAMESPACE,
        timeout=KUBECTL_MUTATE_TIMEOUT,
    )


def scale_env(slug: str, env: str, replicas: int):
    name = f"odoo-{slug}-{env}"
    run_kubectl(
        ["scale", "deployment", name, f"--replicas={max(0, int(replicas))}"],
        namespace=ODOO_NAMESPACE,
        timeout=KUBECTL_MUTATE_TIMEOUT,
    )


def db_session():
    return SessionLocal()


def current_user(request: Request) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with db_session() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return None
        should_be_admin = is_master_admin_user(user.email)
        if bool(user.is_admin) != bool(should_be_admin):
            user.is_admin = bool(should_be_admin)
            db.commit()
            db.refresh(user)
        return user


def ensure_prereqs():
    missing = [k for k in [ODOO_ADMIN_PASSWD, DB_HOST, DB_USER, DB_PASSWORD] if not k]
    if missing:
        raise RuntimeError("Missing required env vars")


def host_for(slug: str, env: str) -> str:
    if env == APEX_ENV:
        return f"{slug}.{BASE_DOMAIN}"
    return f"{slug}--{env}.{BASE_DOMAIN}"


def db_name_for(slug: str, env: str) -> str:
    return f"{slug}_{env}"


def odoo_image_for_version(version: str) -> str:
    version = version or DEFAULT_ODOO_VERSION
    if version.startswith("odoo:"):
        return version
    return f"odoo:{version}"


def effective_env_settings(project: Project, env_obj: Optional["Environment"] = None) -> dict:
    # Runtime profile is project-scoped by design (not per-branch).
    workers = project.workers or DEFAULT_WORKERS
    storage_gb = project.storage_gb or DEFAULT_STORAGE_GB
    odoo_version = project.odoo_version or DEFAULT_ODOO_VERSION
    return {
        "workers": max(1, int(workers)),
        "storage_gb": max(1, int(storage_gb)),
        "odoo_version": odoo_version,
    }


def ingress_name_for_domain(base: str, host: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", host.lower()).strip("-")
    if len(slug) > 40:
        slug = slug[:40]
    digest = hashlib.sha1(host.encode("utf-8")).hexdigest()[:6]
    name = f"{base}-{slug}-{digest}"
    return name[:63].rstrip("-")


def env_service_url(slug: str, env: str) -> str:
    return f"http://odoo-{slug}-{env}.{ODOO_NAMESPACE}.svc.cluster.local"


def odoo_http_status(slug: str, env: str) -> Optional[int]:
    url = f"{env_service_url(slug, env)}/web/login"
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(url, follow_redirects=False)
        return resp.status_code
    except Exception:
        return None


def init_job_name(slug: str, env: str) -> str:
    name = f"odoo-init-{slug}-{env}"
    return name[:63].rstrip("-")


def kubectl_get_list_json(namespace: str, kind: str, selector: str) -> Optional[dict]:
    proc = run_kubectl(
        ["get", kind, "-l", selector, "-o", "json"],
        namespace=namespace,
        timeout=KUBECTL_GET_TIMEOUT,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        return None


def kubectl_names_by_selector(namespace: str, kind: str, selector: str) -> list[str]:
    payload = kubectl_get_list_json(namespace, kind, selector) or {}
    names: list[str] = []
    for item in (payload.get("items") or []):
        name = str((item.get("metadata") or {}).get("name") or "").strip()
        if name:
            names.append(name)
    return sorted(set(names))


def parse_k8s_timestamp(raw: str) -> Optional[datetime]:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def k8s_age_seconds(raw: str) -> Optional[int]:
    parsed = parse_k8s_timestamp(raw)
    if not parsed:
        return None
    return max(0, int((datetime.utcnow() - parsed).total_seconds()))


def init_job_failure_reason(slug: str, env: str) -> str:
    job_name = init_job_name(slug, env)
    pods = kubectl_get_list_json(ODOO_NAMESPACE, "pods", f"job-name={job_name}")
    items = (pods or {}).get("items") or []
    if not items:
        return ""

    pod = sorted(items, key=lambda p: p.get("metadata", {}).get("creationTimestamp", ""))[-1]
    status = pod.get("status", {})
    for container in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or []):
        state = container.get("state") or {}
        terminated = state.get("terminated") or {}
        if terminated:
            reason = terminated.get("reason") or "Terminated"
            message = (terminated.get("message") or "").strip()
            return f"{reason}: {message}"[:220].rstrip(": ")
        waiting = state.get("waiting") or {}
        if waiting:
            reason = waiting.get("reason") or "Waiting"
            message = (waiting.get("message") or "").strip()
            return f"{reason}: {message}"[:220].rstrip(": ")
        running = state.get("running") or {}
        if running:
            started_at = str(running.get("startedAt") or "").strip()
            if started_at:
                age = k8s_age_seconds(started_at)
                if age is not None:
                    return f"Running for {age}s"
            return "Running"

    phase = status.get("phase")
    if phase and phase not in {"Running", "Succeeded"}:
        return str(phase)[:220]
    return ""


def init_job_status(slug: str, env: str) -> Optional[dict]:
    job = kubectl_get_json(ODOO_NAMESPACE, "job", init_job_name(slug, env))
    if not job:
        return None
    status = job.get("status", {})
    active = int(status.get("active") or 0)
    failed = int(status.get("failed") or 0)
    failed_reason = ""
    active_reason = ""
    started_at = str(
        status.get("startTime")
        or (job.get("metadata") or {}).get("creationTimestamp")
        or ""
    ).strip()
    age_seconds = k8s_age_seconds(started_at)
    if failed > 0:
        for cond in status.get("conditions") or []:
            if cond.get("type") == "Failed" and cond.get("status") == "True":
                failed_reason = (cond.get("message") or cond.get("reason") or "").strip()
                break
        if not failed_reason:
            failed_reason = init_job_failure_reason(slug, env)
    if active > 0:
        active_reason = init_job_failure_reason(slug, env)
    return {
        "active": active,
        "succeeded": int(status.get("succeeded") or 0),
        "failed": failed,
        "reason": failed_reason[:220],
        "active_reason": active_reason[:220],
        "started_at": started_at,
        "age_seconds": age_seconds,
    }


def pg_admin_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname="postgres",
        sslmode=DB_SSLMODE,
        connect_timeout=DB_CONNECT_TIMEOUT,
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )


def create_db(db_name: str):
    conn = pg_admin_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        try:
            cur.execute(sql.SQL("CREATE DATABASE {};").format(sql.Identifier(db_name)))
        except psycopg2.errors.DuplicateDatabase:
            pass
    conn.close()


def db_exists(db_name: str) -> bool:
    conn = pg_admin_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def drop_db(db_name: str):
    conn = pg_admin_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s;",
                (db_name,),
            )
        except Exception:
            pass
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {};").format(sql.Identifier(db_name)))
    conn.close()


def ensure_db_removed(db_name: str, retries: int = 3, retry_delay: float = 1.0):
    last_error = ""
    for _ in range(max(1, retries)):
        try:
            drop_db(db_name)
        except Exception as exc:
            last_error = str(exc)[:220]

        try:
            if not db_exists(db_name):
                return
            last_error = f"database '{db_name}' still exists after drop"
        except Exception as exc:
            last_error = str(exc)[:220]

        time.sleep(max(0.2, retry_delay))

    raise RuntimeError(f"Failed to prepare clean database '{db_name}': {last_error or 'unknown error'}")


def purge_project_databases(slug: str):
    safe_slug = (slug or "").strip().lower()
    if not safe_slug or not SLUG_RE.match(safe_slug):
        return
    for env_name in ("dev", "staging", "prod"):
        db_name = db_name_for(safe_slug, env_name)
        try:
            if db_exists(db_name):
                drop_db(db_name)
        except Exception:
            # Best effort purge for stale databases.
            pass


PROJECT_DB_NAME_RE = re.compile(r"^(?P<slug>[a-z0-9-]{2,31})_(?P<env>dev|staging|prod)$")


def purge_orphan_project_databases(live_slugs: set[str]) -> int:
    conn = pg_admin_connection()
    conn.autocommit = True
    dropped = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT datname
                FROM pg_database
                WHERE datistemplate = false
                  AND datallowconn = true
                """
            )
            names = [str(row[0]) for row in (cur.fetchall() or []) if row and row[0]]
    finally:
        conn.close()

    for db_name in names:
        match = PROJECT_DB_NAME_RE.match(db_name)
        if not match:
            continue
        slug = match.group("slug")
        if slug in live_slugs:
            continue
        try:
            drop_db(db_name)
            dropped += 1
        except Exception:
            pass
    return dropped


def clone_db(source: str, dest: str):
    if source == dest:
        return
    conn = pg_admin_connection()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s;",
            (source,),
        )
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s;",
            (dest,),
        )
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {};").format(sql.Identifier(dest)))
        cur.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE {};").format(
                sql.Identifier(dest),
                sql.Identifier(DB_USER),
                sql.Identifier(source),
            )
        )
    conn.close()


def kubectl_apply(manifest: str):
    proc = run_kubectl(
        ["apply", "-f", "-"],
        namespace=ODOO_NAMESPACE,
        input_bytes=manifest.encode("utf-8"),
        timeout=KUBECTL_MUTATE_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8"))


def kubectl_get_json(namespace: str, kind: str, name: str) -> Optional[dict]:
    proc = run_kubectl(
        ["get", kind, name, "-o", "json"],
        namespace=namespace,
        timeout=KUBECTL_GET_TIMEOUT,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        return None


def k8s_project_env(meta: dict) -> tuple[str, str]:
    labels = (meta.get("labels") or {}) if isinstance(meta, dict) else {}
    slug = str(labels.get("project") or "").strip().lower()
    env = str(labels.get("env") or "").strip().lower()
    name = str(meta.get("name") or "").strip().lower() if isinstance(meta, dict) else ""

    if (not slug or not env) and name:
        m = re.match(r"^odoo-([a-z0-9-]+)-(dev|staging|prod)$", name)
        if m:
            slug = m.group(1)
            env = m.group(2)

    if env not in {"dev", "staging", "prod"}:
        return "", ""
    if not SLUG_RE.match(slug):
        return "", ""
    return slug, env


def discover_cluster_projects() -> dict[str, dict[str, dict]]:
    projects: dict[str, dict[str, dict]] = {}

    deployments = kubectl_get_list_json(ODOO_NAMESPACE, "deployments", "app=odoo") or {}
    for item in deployments.get("items") or []:
        meta = item.get("metadata") or {}
        slug, env = k8s_project_env(meta)
        if not slug or not env:
            continue
        projects.setdefault(slug, {})
        projects[slug].setdefault(env, {"host": host_for(slug, env), "domains": set()})

    ingresses = kubectl_get_list_json(ODOO_NAMESPACE, "ingresses", "app=odoo") or {}
    for item in ingresses.get("items") or []:
        meta = item.get("metadata") or {}
        slug, env = k8s_project_env(meta)
        if not slug or not env:
            continue
        env_entry = projects.setdefault(slug, {}).setdefault(
            env,
            {"host": host_for(slug, env), "domains": set()},
        )
        rules = ((item.get("spec") or {}).get("rules") or [])
        for rule in rules:
            host = str((rule or {}).get("host") or "").strip().lower()
            if host:
                env_entry["domains"].add(host)

    for slug, envs in projects.items():
        for env, info in envs.items():
            domains = {d for d in info.get("domains", set()) if d}
            default_host = host_for(slug, env)
            chosen = default_host
            if default_host not in domains and domains:
                same_base = sorted([d for d in domains if d.endswith(f".{BASE_DOMAIN}")])
                chosen = same_base[0] if same_base else sorted(domains)[0]
            info["host"] = chosen
            domains.add(chosen)
            info["domains"] = sorted(domains)

    return projects


def reconcile_projects_from_cluster(owner_id: int) -> dict:
    discovered = discover_cluster_projects()
    if not discovered:
        return {"projects": 0, "envs": 0, "domains": 0}

    imported_projects = 0
    imported_envs = 0
    imported_domains = 0

    with db_session() as db:
        tombstoned_slugs = {
            row.slug
            for row in db.query(ProjectTombstone).all()
            if row.slug
        }
        existing = {p.slug: p for p in db.query(Project).all()}
        for slug, env_map in discovered.items():
            if slug in tombstoned_slugs:
                continue
            project = existing.get(slug)
            if not project:
                project = Project(
                    slug=slug,
                    display_name=slug,
                    dev_branch=DEFAULT_DEV_BRANCH,
                    staging_branch=DEFAULT_STAGING_BRANCH,
                    prod_branch=DEFAULT_PROD_BRANCH,
                    workers=DEFAULT_WORKERS,
                    storage_gb=DEFAULT_STORAGE_GB,
                    staging_slots=DEFAULT_STAGING_SLOTS,
                    odoo_version=DEFAULT_ODOO_VERSION,
                    status="active",
                    owner_id=owner_id,
                )
                db.add(project)
                db.flush()
                imported_projects += 1
                existing[slug] = project

            env_by_name = {env.name: env for env in project.envs}
            for env_name, env_info in env_map.items():
                env_host = str(env_info.get("host") or host_for(slug, env_name)).strip().lower()
                env_obj = env_by_name.get(env_name)
                if not env_obj:
                    env_obj = Environment(
                        project_id=project.id,
                        name=env_name,
                        host=env_host,
                        db_name=db_name_for(slug, env_name),
                        workers=project.workers or DEFAULT_WORKERS,
                        storage_gb=project.storage_gb or DEFAULT_STORAGE_GB,
                        odoo_version=project.odoo_version or DEFAULT_ODOO_VERSION,
                        status="active",
                        last_error="",
                    )
                    db.add(env_obj)
                    db.flush()
                    imported_envs += 1
                    env_by_name[env_name] = env_obj
                else:
                    if not env_obj.host:
                        env_obj.host = env_host
                    if not env_obj.db_name:
                        env_obj.db_name = db_name_for(slug, env_name)
                    if not env_obj.status:
                        env_obj.status = "active"

                seen_hosts = {d.host for d in env_obj.domains}
                for host in env_info.get("domains", []):
                    if host in seen_hosts:
                        continue
                    if db.query(Domain).filter(Domain.host == host).first():
                        continue
                    db.add(Domain(env_id=env_obj.id, host=host))
                    imported_domains += 1

        if imported_projects or imported_envs or imported_domains:
            db.commit()

    return {
        "projects": imported_projects,
        "envs": imported_envs,
        "domains": imported_domains,
    }


def maybe_reconcile_cluster_projects(user: User):
    if not user or not user.is_admin:
        return
    now = time.time()
    if (now - float(PROJECT_RECONCILE_STATE.get("fetched_at") or 0.0)) < CONTROL_PLANE_RECONCILE_TTL:
        return

    with PROJECT_RECONCILE_LOCK:
        now = time.time()
        if (now - float(PROJECT_RECONCILE_STATE.get("fetched_at") or 0.0)) < CONTROL_PLANE_RECONCILE_TTL:
            return
        try:
            result = reconcile_projects_from_cluster(user.id)
            PROJECT_RECONCILE_STATE["error"] = ""
            PROJECT_RECONCILE_STATE["imported_projects"] = result["projects"]
            PROJECT_RECONCILE_STATE["imported_envs"] = result["envs"]
            PROJECT_RECONCILE_STATE["imported_domains"] = result["domains"]
        except Exception as exc:
            PROJECT_RECONCILE_STATE["error"] = str(exc)[:220]
        finally:
            PROJECT_RECONCILE_STATE["fetched_at"] = time.time()


def tls_secret_name(slug: str, env_name: str) -> str:
    return f"odoo-{slug}-{env_name}-tls"


def tls_secret_ready(secret_name: str) -> bool:
    secret = kubectl_get_json(ODOO_NAMESPACE, "secret", secret_name)
    if not secret:
        return False
    data = secret.get("data", {})
    return bool(data.get("tls.crt") and data.get("tls.key"))


def service_has_ready_endpoints(slug: str, env_name: str) -> Optional[bool]:
    svc_name = f"odoo-{slug}-{env_name}"
    endpoints = kubectl_get_json(ODOO_NAMESPACE, "endpoints", svc_name)
    if not endpoints:
        # Unknown (RBAC/API hiccup): let other checks decide readiness.
        return None
    for subset in endpoints.get("subsets") or []:
        if subset.get("addresses"):
            return True
    return False


def odoo_pod_issue(slug: str, env_name: str) -> str:
    pods = kubectl_get_list_json(ODOO_NAMESPACE, "pods", f"app=odoo,project={slug},env={env_name}")
    if not pods:
        # Unknown (RBAC/API hiccup): do not force a false negative status.
        return ""
    items = (pods or {}).get("items") or []
    if not items:
        return "Pod not scheduled yet"

    pod = sorted(items, key=lambda p: p.get("metadata", {}).get("creationTimestamp", ""))[-1]
    status = pod.get("status", {})
    for container in status.get("containerStatuses") or []:
        state = container.get("state") or {}
        waiting = state.get("waiting") or {}
        if waiting:
            reason = waiting.get("reason") or "Waiting"
            message = (waiting.get("message") or "").strip()
            return f"{reason}: {message}"[:220].rstrip(": ")
        terminated = state.get("terminated") or {}
        if terminated:
            reason = terminated.get("reason") or "Terminated"
            message = (terminated.get("message") or "").strip()
            return f"{reason}: {message}"[:220].rstrip(": ")

    phase = status.get("phase")
    if phase and phase not in {"Running", "Succeeded"}:
        return str(phase)[:220]
    return ""


def env_runtime_status(project: Project, env_name: str) -> dict:
    env_obj = None
    with db_session() as db:
        env_obj = db.query(Environment).filter(
            Environment.project_id == project.id,
            Environment.name == env_name,
        ).first()

    env_error = ""
    if env_obj and env_obj.status == "failed" and env_obj.last_error:
        env_error = env_obj.last_error

    name = f"odoo-{project.slug}-{env_name}"
    deploy = kubectl_get_json(ODOO_NAMESPACE, "deployment", name)
    if not deploy:
        job_status = init_job_status(project.slug, env_name)
        if job_status:
            if job_status.get("failed", 0) > 0:
                reason = (job_status.get("reason") or "").strip()
                if reason:
                    return {"ready": False, "message": f"Init failed: {reason}"}
                return {"ready": False, "message": "Init failed"}
            if job_status.get("active", 0) > 0:
                age_seconds = job_status.get("age_seconds")
                active_reason = (job_status.get("active_reason") or "").strip()
                if isinstance(age_seconds, int) and age_seconds > INIT_JOB_STALE_SECONDS:
                    details = f" ({active_reason})" if active_reason else ""
                    return {
                        "ready": False,
                        "message": f"Init failed: timeout after {age_seconds}s{details}",
                    }
                if active_reason:
                    return {"ready": False, "message": f"Initializing database: {active_reason}"}
                return {"ready": False, "message": "Initializing database"}
        if env_obj and env_obj.status == "provisioning":
            return {"ready": False, "message": "Provisioning resources"}
        if env_obj and env_obj.status == "failed":
            if env_error:
                return {"ready": False, "message": f"Error: {env_error}"}
            return {"ready": False, "message": "Provisioning failed"}
        if env_obj and env_obj.status == "active":
            return {"ready": False, "message": "Runtime deployment missing"}
        return {"ready": False, "message": "Not created yet"}
    status = deploy.get("status", {})
    replicas = int(status.get("replicas") or 0)
    ready = int(status.get("readyReplicas") or 0)
    if replicas and ready >= replicas:
        endpoints_ready = service_has_ready_endpoints(project.slug, env_name)
        if endpoints_ready is False:
            return {"ready": False, "message": "Service endpoints not ready"}

        job_status = init_job_status(project.slug, env_name)
        if job_status:
            if job_status.get("failed", 0) > 0:
                reason = (job_status.get("reason") or "").strip()
                if reason:
                    return {"ready": False, "message": f"Init failed: {reason}"}
                return {"ready": False, "message": "Init failed"}
            if job_status.get("active", 0) > 0:
                age_seconds = job_status.get("age_seconds")
                active_reason = (job_status.get("active_reason") or "").strip()
                if isinstance(age_seconds, int) and age_seconds > INIT_JOB_STALE_SECONDS:
                    details = f" ({active_reason})" if active_reason else ""
                    return {
                        "ready": False,
                        "message": f"Init failed: timeout after {age_seconds}s{details}",
                    }
                if active_reason:
                    return {"ready": False, "message": f"Initializing database: {active_reason}"}
                return {"ready": False, "message": "Initializing database"}
        http_code = odoo_http_status(project.slug, env_name)
        if http_code and 200 <= http_code < 400:
            if not tls_secret_ready(tls_secret_name(project.slug, env_name)):
                return {"ready": False, "message": "Waiting for TLS certificate"}
            return {"ready": True, "message": "Ready"}
        if http_code and http_code >= 500:
            return {"ready": False, "message": "Application returned 5xx"}
        pod_issue = odoo_pod_issue(project.slug, env_name)
        if pod_issue:
            return {"ready": False, "message": f"Pod issue: {pod_issue}"}
        return {"ready": False, "message": "Starting"}

    for cond in status.get("conditions") or []:
        if cond.get("type") == "Progressing" and cond.get("status") == "False":
            message = (cond.get("message") or cond.get("reason") or "").strip()
            if message:
                return {"ready": False, "message": f"Deployment blocked: {message[:220]}"}

    pvc_name = f"odoo-filestore-{project.slug}-{env_name}"
    pvc = kubectl_get_json(ODOO_NAMESPACE, "pvc", pvc_name)
    if pvc:
        phase = pvc.get("status", {}).get("phase")
        if phase == "Pending":
            return {"ready": False, "message": "Storage pending"}

    job_status = init_job_status(project.slug, env_name)
    if job_status:
        if job_status.get("failed", 0) > 0:
            reason = (job_status.get("reason") or "").strip()
            if reason:
                return {"ready": False, "message": f"Init failed: {reason}"}
            return {"ready": False, "message": "Init failed"}
        if job_status.get("active", 0) > 0:
            age_seconds = job_status.get("age_seconds")
            active_reason = (job_status.get("active_reason") or "").strip()
            if isinstance(age_seconds, int) and age_seconds > INIT_JOB_STALE_SECONDS:
                details = f" ({active_reason})" if active_reason else ""
                return {
                    "ready": False,
                    "message": f"Init failed: timeout after {age_seconds}s{details}",
                }
            if active_reason:
                return {"ready": False, "message": f"Initializing database: {active_reason}"}
            return {"ready": False, "message": "Initializing database"}

    pod_issue = odoo_pod_issue(project.slug, env_name)
    if pod_issue:
        return {"ready": False, "message": f"Pod issue: {pod_issue}"}
    return {"ready": False, "message": "Starting"}


def kubectl_delete(kind: str, name: str, *, wait: bool = True):
    args = ["delete", kind, name, "--ignore-not-found"]
    if not wait:
        args.append("--wait=false")
    run_kubectl(
        args,
        namespace=ODOO_NAMESPACE,
        timeout=KUBECTL_MUTATE_TIMEOUT,
    )


def kubectl_wait(kind: str, name: str, timeout: str = "600s"):
    run_kubectl(
        ["wait", "--for=condition=complete", kind, name, f"--timeout={timeout}"],
        namespace=ODOO_NAMESPACE,
        timeout=max(KUBECTL_MUTATE_TIMEOUT, 90),
    )


def submit_backup_cleanup_job(*, purge_all: bool, retention_days: int) -> str:
    secret = kubectl_get_json(ODOO_NAMESPACE, "secret", "nexora-s3")
    if not secret:
        raise RuntimeError("Missing nexora-s3 secret in odoo-system.")

    safe_retention = max(1, min(3650, int(retention_days)))
    ts = int(time.time())
    job_name = f"nexora-backup-cleanup-{ts}"
    if len(job_name) > 63:
        job_name = job_name[:63].rstrip("-")

    if purge_all:
        script = """
set -eu
root_prefix="${S3_PREFIX:-backups}"
aws --endpoint-url "$S3_ENDPOINT" \
  s3 rm "s3://${S3_BUCKET}/${root_prefix}/" \
  --region "$S3_REGION" \
  --recursive >/dev/null || true
echo "Purged all backup objects under s3://${S3_BUCKET}/${root_prefix}/"
""".strip()
    else:
        script = f"""
set -eu
retention_days="{safe_retention}"
cutoff=$(date -u -d "-${{retention_days}} days" +%Y%m%dT%H%M%SZ)
root_prefix="${{S3_PREFIX:-backups}}"
aws --endpoint-url "$S3_ENDPOINT" \
  s3 ls "s3://${{S3_BUCKET}}/${{root_prefix}}/" \
  --region "$S3_REGION" \
  | awk '{{print $2}}' \
  | sed 's:/$::' \
  | awk '/^[0-9]{{8}}T[0-9]{{6}}Z$/' \
  | while read -r dir; do
      if [ "$dir" \\< "$cutoff" ]; then
        aws --endpoint-url "$S3_ENDPOINT" \
          s3 rm "s3://${{S3_BUCKET}}/${{root_prefix}}/${{dir}}/" \
          --region "$S3_REGION" \
          --recursive >/dev/null || true
      fi
    done
echo "Purged backups older than $retention_days day(s) under s3://${{S3_BUCKET}}/${{root_prefix}}/"
""".strip()

    manifest = f"""
apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
  namespace: {ODOO_NAMESPACE}
spec:
  ttlSecondsAfterFinished: 3600
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: cleanup
          image: amazon/aws-cli:2.15.49
          imagePullPolicy: IfNotPresent
          env:
            - name: S3_ENDPOINT
              valueFrom:
                secretKeyRef:
                  name: nexora-s3
                  key: endpoint
            - name: S3_BUCKET
              valueFrom:
                secretKeyRef:
                  name: nexora-s3
                  key: bucket
            - name: S3_REGION
              valueFrom:
                secretKeyRef:
                  name: nexora-s3
                  key: region
            - name: S3_PREFIX
              valueFrom:
                secretKeyRef:
                  name: nexora-s3
                  key: prefix
            - name: AWS_ACCESS_KEY_ID
              valueFrom:
                secretKeyRef:
                  name: nexora-s3
                  key: access_key_id
            - name: AWS_SECRET_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: nexora-s3
                  key: secret_access_key
          command:
            - /bin/sh
            - -c
            - |
{chr(10).join("              " + line for line in script.splitlines())}
""".strip() + "\n"

    kubectl_apply(manifest)
    return job_name


def run_filestore_job(name: str, src_pvc: str, dst_pvc: str, command: str):
    manifest = f"""
apiVersion: batch/v1
kind: Job
metadata:
  name: {name}
  namespace: {ODOO_NAMESPACE}
spec:
  backoffLimit: 1
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: worker
          image: alpine:3.19
          command: ["/bin/sh", "-c", "{command}"]
          volumeMounts:
            - name: src
              mountPath: /src
            - name: dst
              mountPath: /dst
      volumes:
        - name: src
          persistentVolumeClaim:
            claimName: {src_pvc}
        - name: dst
          persistentVolumeClaim:
            claimName: {dst_pvc}
""".strip() + "\n"
    kubectl_apply(manifest)
    kubectl_wait("job", name)
    kubectl_delete("job", name)


def ensure_odoo_init(
    project: Project,
    env: str,
    env_obj: Optional[Environment] = None,
    restart_failed: bool = False,
):
    slug = project.slug
    job_name = init_job_name(slug, env)
    status = init_job_status(slug, env)
    if status:
        if status.get("active", 0) > 0:
            # Keep runtime deployment scaled down while init job holds the PVC.
            try:
                scale_env(slug, env, 0)
            except Exception:
                pass
            return
        if status.get("succeeded", 0) > 0:
            # Init is done; ensure runtime deployment is allowed to start.
            try:
                scale_env(slug, env, 1)
            except Exception:
                pass
            return
        if status.get("failed", 0) > 0:
            if not restart_failed:
                return
            kubectl_delete("job", job_name)

    settings = effective_env_settings(project, env_obj)
    odoo_image = odoo_image_for_version(settings["odoo_version"])
    pvc_name = f"odoo-filestore-{slug}-{env}"
    cfg_secret = f"odoo-config-{slug}-{env}"

    manifest = f"""
apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
  namespace: {ODOO_NAMESPACE}
  labels:
    app: odoo
    project: {slug}
    env: {env}
    role: init
spec:
  backoffLimit: 1
  template:
    spec:
      securityContext:
        fsGroup: 101
      restartPolicy: Never
      initContainers:
        - name: init-permissions
          image: busybox:1.36
          command: ["sh", "-c", "chown -R 101:101 /var/lib/odoo"]
          volumeMounts:
            - name: filestore
              mountPath: /var/lib/odoo
      containers:
        - name: init
          image: {odoo_image}
          command:
            [
              "sh",
              "-c",
              "odoo -c /etc/odoo/odoo.conf -i base --stop-after-init --workers=0 --max-cron-threads=0 --log-level=info",
            ]
          volumeMounts:
            - name: filestore
              mountPath: /var/lib/odoo
            - name: odoo-config
              mountPath: /etc/odoo/odoo.conf
              subPath: odoo.conf
      volumes:
        - name: filestore
          persistentVolumeClaim:
            claimName: {pvc_name}
        - name: odoo-config
          secret:
            secretName: {cfg_secret}
""".strip() + "\n"

    # Avoid PVC multi-attach with runtime pod during DB bootstrap.
    try:
        scale_env(slug, env, 0)
    except Exception:
        pass
    kubectl_apply(manifest)


def wait_for_init_completion(project: Project, env: str, env_obj: Optional[Environment] = None):
    slug = project.slug
    deadline = time.time() + INIT_JOB_MAX_WAIT_SECONDS

    # Ensure a fresh init job exists (and restart if previous one failed).
    ensure_odoo_init(project, env, env_obj, restart_failed=True)

    while time.time() < deadline:
        status = init_job_status(slug, env)
        if status:
            if status.get("failed", 0) > 0:
                reason = (status.get("reason") or "").strip() or "unknown error"
                raise RuntimeError(f"Init failed: {reason}")
            if status.get("succeeded", 0) > 0:
                try:
                    scale_env(slug, env, 1)
                except Exception:
                    pass
                return
        else:
            # If job is missing (e.g. manually removed), re-create once.
            ensure_odoo_init(project, env, env_obj, restart_failed=True)
        time.sleep(INIT_JOB_POLL_SECONDS)

    raise RuntimeError(
        f"Init timeout after {INIT_JOB_MAX_WAIT_SECONDS}s; check pods/logs for odoo-init-{slug}-{env}."
    )


def wipe_filestore(pvc_name: str):
    job = f"wipe-{pvc_name[:45]}".lower()
    run_filestore_job(job, pvc_name, pvc_name, "rm -rf /dst/*")


def copy_filestore(src_pvc: str, dst_pvc: str):
    job = f"copy-{src_pvc[:20]}-{dst_pvc[:20]}".lower().replace("_", "-")
    run_filestore_job(job, src_pvc, dst_pvc, "rm -rf /dst/* && cp -a /src/. /dst/")


def apply_domain_ingress(env: Environment, host: str):
    service_name = f"odoo-{env.project.slug}-{env.name}"
    ingress_name = ingress_name_for_domain(service_name, host)
    tls_secret = f"{ingress_name}-tls"
    issuer_annotation = (
        f"\n    cert-manager.io/cluster-issuer: {TLS_CLUSTER_ISSUER}"
        if TLS_CLUSTER_ISSUER
        else ""
    )
    manifest = f"""
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {ingress_name}
  namespace: {ODOO_NAMESPACE}
  labels:
    app: odoo
    project: {env.project.slug}
    env: {env.name}
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"{issuer_annotation}
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - {host}
      secretName: {tls_secret}
  rules:
    - host: {host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {service_name}
                port:
                  number: 80
""".strip() + "\n"
    kubectl_apply(manifest)


def provision_env(project: Project, env: str, env_obj: Optional[Environment] = None):
    slug = project.slug
    host = host_for(slug, env)
    db_name = db_name_for(slug, env)
    name = f"odoo-{slug}-{env}"
    pvc_name = f"odoo-filestore-{slug}-{env}"
    cfg_secret = f"odoo-config-{slug}-{env}"
    settings = effective_env_settings(project, env_obj)
    storage_gb = settings["storage_gb"]
    workers = settings["workers"]
    odoo_image = odoo_image_for_version(settings["odoo_version"])
    tls_secret = tls_secret_name(slug, env)
    storage_class_line = f"  storageClassName: {STORAGE_CLASS_NAME}\n" if STORAGE_CLASS_NAME else ""
    issuer_annotation = (
        f"\n    cert-manager.io/cluster-issuer: {TLS_CLUSTER_ISSUER}"
        if TLS_CLUSTER_ISSUER
        else ""
    )

    create_db(db_name)

    manifest = f"""
apiVersion: v1
kind: Secret
metadata:
  name: {cfg_secret}
  namespace: {ODOO_NAMESPACE}
  labels:
    app: odoo
    project: {slug}
    env: {env}
type: Opaque
stringData:
  odoo.conf: |
    [options]
    admin_passwd = {ODOO_ADMIN_PASSWD}
    db_host = {DB_HOST}
    db_port = {DB_PORT}
    db_user = {DB_USER}
    db_password = {DB_PASSWORD}
    db_name = {db_name}
    workers = {workers}
    limit_time_cpu = 300
    limit_time_real = 600
    max_cron_threads = 1
    list_db = False
    proxy_mode = True
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {pvc_name}
  namespace: {ODOO_NAMESPACE}
  labels:
    app: odoo
    project: {slug}
    env: {env}
spec:
  accessModes:
    - ReadWriteOnce
{storage_class_line}  resources:
    requests:
      storage: {storage_gb}Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}
  namespace: {ODOO_NAMESPACE}
  labels:
    app: odoo
    project: {slug}
    env: {env}
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: odoo
      project: {slug}
      env: {env}
  template:
    metadata:
      labels:
        app: odoo
        project: {slug}
        env: {env}
    spec:
      securityContext:
        fsGroup: 101
      initContainers:
        - name: init-permissions
          image: busybox:1.36
          command: ["sh", "-c", "chown -R 101:101 /var/lib/odoo"]
          volumeMounts:
            - name: filestore
              mountPath: /var/lib/odoo
      containers:
        - name: odoo
          image: {odoo_image}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8069
              name: http
          env:
            - name: HOST
              value: {DB_HOST}
            - name: PORT
              value: "{DB_PORT}"
            - name: USER
              value: {DB_USER}
            - name: PASSWORD
              value: {DB_PASSWORD}
            - name: PGSSLMODE
              value: {DB_SSLMODE}
          volumeMounts:
            - name: filestore
              mountPath: /var/lib/odoo
            - name: odoo-config
              mountPath: /etc/odoo/odoo.conf
              subPath: odoo.conf
      volumes:
        - name: filestore
          persistentVolumeClaim:
            claimName: {pvc_name}
        - name: odoo-config
          secret:
            secretName: {cfg_secret}
---
apiVersion: v1
kind: Service
metadata:
  name: {name}
  namespace: {ODOO_NAMESPACE}
  labels:
    app: odoo
    project: {slug}
    env: {env}
spec:
  type: ClusterIP
  selector:
    app: odoo
    project: {slug}
    env: {env}
  ports:
    - name: http
      port: 80
      targetPort: 8069
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
  namespace: {ODOO_NAMESPACE}
  labels:
    app: odoo
    project: {slug}
    env: {env}
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"{issuer_annotation}
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - {host}
      secretName: {tls_secret}
  rules:
    - host: {host}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {name}
                port:
                  number: 80
""".strip() + "\n"

    kubectl_apply(manifest)
    ensure_odoo_init(project, env, env_obj, restart_failed=True)
    return host, db_name


def reset_env(project: Project, env: str):
    db_name = db_name_for(project.slug, env)
    pvc_name = f"odoo-filestore-{project.slug}-{env}"
    drop_db(db_name)
    create_db(db_name)
    wipe_filestore(pvc_name)
    restart_env(project.slug, env)
    ensure_odoo_init(project, env, restart_failed=True)


def promote_env(project: Project, source_env: str, target_env: str):
    source_db = db_name_for(project.slug, source_env)
    target_db = db_name_for(project.slug, target_env)
    source_pvc = f"odoo-filestore-{project.slug}-{source_env}"
    target_pvc = f"odoo-filestore-{project.slug}-{target_env}"
    target_init_job = init_job_name(project.slug, target_env)
    source_replicas = None
    target_replicas = None
    source_deploy = kubectl_get_json(ODOO_NAMESPACE, "deployment", f"odoo-{project.slug}-{source_env}")
    if source_deploy:
        source_replicas = int((source_deploy.get("spec", {}) or {}).get("replicas") or 0)
    target_deploy = kubectl_get_json(ODOO_NAMESPACE, "deployment", f"odoo-{project.slug}-{target_env}")
    if target_deploy:
        target_replicas = int((target_deploy.get("spec", {}) or {}).get("replicas") or 0)

    # RWO PVCs cannot be attached from multiple pods/nodes; scale down both envs before copy.
    try:
        scale_env(project.slug, source_env, 0)
    except Exception:
        pass
    try:
        scale_env(project.slug, target_env, 0)
    except Exception:
        pass

    # If target was just provisioned, cancel its bootstrap job; this flow uses DB/PVC copy.
    kubectl_delete("job", target_init_job)
    run_kubectl(
        ["delete", "pod", "-l", f"job-name={target_init_job}", "--ignore-not-found"],
        namespace=ODOO_NAMESPACE,
        timeout=KUBECTL_MUTATE_TIMEOUT,
    )

    clone_db(source_db, target_db)
    copy_filestore(source_pvc, target_pvc)

    if source_replicas and source_replicas > 0:
        try:
            scale_env(project.slug, source_env, source_replicas)
        except Exception:
            pass

    desired_target = 1 if target_replicas is None else max(1, target_replicas)
    scale_env(project.slug, target_env, desired_target)
    restart_env(project.slug, target_env)


def reset_from_prod(project: Project, target_env: str):
    promote_env(project, "prod", target_env)


def delete_env(slug: str, env: str, domain_hosts: Optional[list[str]] = None):
    name = f"odoo-{slug}-{env}"
    pvc_name = f"odoo-filestore-{slug}-{env}"
    cfg_secret = f"odoo-config-{slug}-{env}"

    all_domain_hosts = set(domain_hosts or [])

    # Remove custom domain ingresses tied to this env if project metadata still exists.
    with db_session() as db:
        project = db.query(Project).filter(Project.slug == slug).first()
        if project:
            env_obj = db.query(Environment).filter(
                Environment.project_id == project.id,
                Environment.name == env,
            ).first()
            if env_obj:
                for domain in list(env_obj.domains):
                    all_domain_hosts.add(domain.host)

    for host in sorted(all_domain_hosts):
        ingress_name = ingress_name_for_domain(name, host)
        try:
            kubectl_delete("ingress", ingress_name, wait=False)
        except Exception:
            pass

    for kind, resource_name in [
        ("ingress", name),
        ("service", name),
        ("deployment", name),
        ("secret", cfg_secret),
        ("pvc", pvc_name),
    ]:
        try:
            kubectl_delete(kind, resource_name, wait=False)
        except Exception:
            pass

    try:
        drop_db(db_name_for(slug, env))
    except Exception:
        pass


def has_live_project_for_slug(slug: str) -> bool:
    safe_slug = (slug or "").strip().lower()
    if not safe_slug:
        return False
    with db_session() as db:
        project = db.query(Project).filter(Project.slug == safe_slug).first()
        if not project:
            return False
        status = (project.status or "active").strip().lower()
        return status not in {"deleting", "delete_failed"}


def cleanup_project_slug_artifacts(slug: str, env_domains: Optional[dict[str, list[str]]] = None):
    safe_slug = (slug or "").strip().lower()
    if not safe_slug or not SLUG_RE.match(safe_slug):
        return
    if has_live_project_for_slug(safe_slug):
        return

    domains_by_env = env_domains or {}
    for env_name in ["dev", "staging", "prod"]:
        if has_live_project_for_slug(safe_slug):
            return
        try:
            delete_env(safe_slug, env_name, domains_by_env.get(env_name) or [])
        except Exception:
            # Best-effort cleanup.
            pass

    selector = f"app=odoo,project={safe_slug}"
    for singular, plural in [
        ("ingress", "ingresses"),
        ("service", "services"),
        ("deployment", "deployments"),
        ("secret", "secrets"),
        ("pvc", "persistentvolumeclaims"),
        ("job", "jobs"),
    ]:
        if has_live_project_for_slug(safe_slug):
            return
        names = kubectl_names_by_selector(ODOO_NAMESPACE, plural, selector)
        for name in names:
            try:
                kubectl_delete(singular, name, wait=False)
            except Exception:
                pass


def set_env_status(project_id: int, env_name: str, status: str, error: str = ""):
    with db_session() as db:
        env = db.query(Environment).filter(
            Environment.project_id == project_id,
            Environment.name == env_name,
        ).first()
        if not env:
            return
        env.status = status
        env.last_error = (error or "")[:500]
        db.commit()


def set_project_status(project_id: int, status: str, error: str = ""):
    with db_session() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return
        project.status = status
        project.last_error = (error or "")[:500]
        db.commit()


def spawn_project_delete(project_id: int):
    def _worker():
        try:
            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                slug = project.slug
                env_names = sorted({*["dev", "staging", "prod"], *[env.name for env in project.envs]})
                env_domains = {
                    env.name: [domain.host for domain in env.domains]
                    for env in list(project.envs)
                }
                upsert_project_tombstone(db, slug, "delete")
                db.commit()

            errors = []
            for env_name in env_names:
                try:
                    delete_env(slug, env_name, env_domains.get(env_name) or [])
                except Exception as exc:
                    errors.append(f"{env_name}: {str(exc)[:120]}")

            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                upsert_project_tombstone(db, project.slug, "delete")
                db.delete(project)
                db.commit()
            if errors:
                # Keep deleting resources best-effort even after project is removed from dashboard.
                spawn_orphan_cleanup(slug, env_domains)
        except Exception as exc:
            set_project_status(project_id, "delete_failed", str(exc))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def spawn_orphan_cleanup(slug: str, env_domains: dict[str, list[str]]):
    def _worker():
        cleanup_project_slug_artifacts(slug, env_domains)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def spawn_maintenance_cleanup(
    *,
    slugs: list[str],
    backups_mode: str = "none",
    backup_retention_days: int = 2,
):
    def _worker():
        for slug in slugs:
            cleanup_project_slug_artifacts(slug)

        if backups_mode in {"purge_all", "retention"}:
            try:
                submit_backup_cleanup_job(
                    purge_all=(backups_mode == "purge_all"),
                    retention_days=backup_retention_days,
                )
            except Exception:
                pass

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def spawn_env_provision(project_id: int, env_name: str):
    def _worker():
        set_env_status(project_id, env_name, "provisioning", "")
        try:
            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                env_obj = db.query(Environment).filter(
                    Environment.project_id == project.id,
                    Environment.name == env_name,
                ).first()
                if not env_obj:
                    return
                db.add(
                    BuildEvent(
                        project_id=project.id,
                        env=env_name,
                        branch="",
                        sha="",
                        status="provisioning",
                        message=f"Provisioning {env_name} environment",
                    )
                )
                db.commit()
                # Always start a new provisioning cycle from a clean database name.
                ensure_db_removed(db_name_for(project.slug, env_name))

                host, db_name = provision_env(project, env_name, env_obj)
                env_obj.host = host
                env_obj.db_name = db_name
                env_obj.status = "provisioning"
                env_obj.last_error = ""
                db.commit()
                record_build_event(
                    project.id,
                    env_name,
                    "",
                    "",
                    "provisioning",
                    f"{env_name} resources created. Waiting for database initialization job.",
                )

            # Wait outside an open DB transaction; init can take minutes.
            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                env_obj = db.query(Environment).filter(
                    Environment.project_id == project.id,
                    Environment.name == env_name,
                ).first()
                if not env_obj:
                    return
                wait_for_init_completion(project, env_name, env_obj)

            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                env_obj = db.query(Environment).filter(
                    Environment.project_id == project.id,
                    Environment.name == env_name,
                ).first()
                if not env_obj:
                    return
                env_obj.status = "active"
                env_obj.last_error = ""
                db.add(
                    BuildEvent(
                        project_id=project.id,
                        env=env_name,
                        branch="",
                        sha="",
                        status="active",
                        message=f"{env_name} environment is active",
                    )
                )
                db.commit()
        except Exception as exc:
            set_env_status(project_id, env_name, "failed", str(exc))
            record_build_event(
                project_id,
                env_name,
                "",
                "",
                "failed",
                f"{env_name} provisioning failed: {str(exc)[:180]}",
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def spawn_env_restart(project_id: int, env_name: str):
    def _worker():
        record_build_event(
            project_id,
            env_name,
            "",
            "",
            "restarting",
            f"Restarting {env_name} runtime",
        )
        try:
            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                env_obj = db.query(Environment).filter(
                    Environment.project_id == project.id,
                    Environment.name == env_name,
                ).first()
                if not env_obj:
                    return
                restart_env(project.slug, env_name)

            set_env_status(project_id, env_name, "active", "")
            record_build_event(
                project_id,
                env_name,
                "",
                "",
                "active",
                f"{env_name} runtime restarted",
            )
        except Exception as exc:
            set_env_status(project_id, env_name, "failed", str(exc))
            record_build_event(
                project_id,
                env_name,
                "",
                "",
                "failed",
                f"{env_name} restart failed: {str(exc)[:180]}",
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def spawn_env_redeploy(project_id: int, env_name: str):
    def _worker():
        set_env_status(project_id, env_name, "provisioning", "")
        record_build_event(
            project_id,
            env_name,
            "",
            "",
            "redeploying",
            f"Redeploying {env_name} environment",
        )
        try:
            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                env_obj = db.query(Environment).filter(
                    Environment.project_id == project.id,
                    Environment.name == env_name,
                ).first()
                if not env_obj:
                    return

                # Force a fresh init cycle to recover from stale/failed init jobs.
                job_name = init_job_name(project.slug, env_name)
                kubectl_delete("job", job_name)
                run_kubectl(
                    ["delete", "pod", "-l", f"job-name={job_name}", "--ignore-not-found"],
                    namespace=ODOO_NAMESPACE,
                    timeout=KUBECTL_MUTATE_TIMEOUT,
                )

                host, db_name = provision_env(project, env_name, env_obj)
                env_obj.host = host
                env_obj.db_name = db_name
                env_obj.last_error = ""
                db.commit()

            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                env_obj = db.query(Environment).filter(
                    Environment.project_id == project.id,
                    Environment.name == env_name,
                ).first()
                if not env_obj:
                    return
                wait_for_init_completion(project, env_name, env_obj)

            set_env_status(project_id, env_name, "active", "")
            record_build_event(
                project_id,
                env_name,
                "",
                "",
                "active",
                f"{env_name} environment redeployed",
            )
        except Exception as exc:
            set_env_status(project_id, env_name, "failed", str(exc))
            record_build_event(
                project_id,
                env_name,
                "",
                "",
                "failed",
                f"{env_name} redeploy failed: {str(exc)[:180]}",
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def ensure_target_env_exists(db, project: Project, envs: dict[str, Environment], source_env: str, target_env: str):
    if target_env in envs:
        return envs[target_env]
    source_settings = effective_env_settings(project, envs[source_env])
    temp_env = Environment(
        workers=source_settings["workers"],
        storage_gb=source_settings["storage_gb"],
        odoo_version=source_settings["odoo_version"],
    )
    host, db_name = provision_env(project, target_env, temp_env)
    env_obj = Environment(
        project_id=project.id,
        name=target_env,
        host=host,
        db_name=db_name,
        workers=source_settings["workers"],
        storage_gb=source_settings["storage_gb"],
        odoo_version=source_settings["odoo_version"],
        status="active",
        last_error="",
    )
    db.add(env_obj)
    db.commit()
    db.refresh(env_obj)
    envs[target_env] = env_obj
    return env_obj


def spawn_env_transfer(
    project_id: int,
    source_env: str,
    target_env: str,
    *,
    mode: str = "copy",
    trigger: str = "manual",
):
    normalized_mode = "move" if mode == "move" else "copy"

    def _worker():
        if target_env not in {"dev", "staging", "prod"}:
            return
        if source_env not in {"dev", "staging", "prod"}:
            return
        if source_env == target_env:
            return
        if normalized_mode == "move" and target_env != "prod":
            return
        if normalized_mode == "move" and source_env == "prod":
            return

        action_message = f"Copy {source_env} → {target_env}"
        if target_env == "dev":
            action_message = f"Reset dev from {source_env}"
        elif normalized_mode == "move":
            action_message = f"Move {source_env} → {target_env}"

        set_env_status(project_id, target_env, "provisioning", "")
        record_build_event(
            project_id,
            target_env,
            "",
            "",
            "promoting",
            f"{action_message} started ({trigger})",
        )

        try:
            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                envs = {e.name: e for e in project.envs}
                if source_env not in envs:
                    raise RuntimeError(f"Source environment '{source_env}' is missing")

                source_state = (envs[source_env].status or "").strip().lower()
                if source_state in {"provisioning", "failed"}:
                    raise RuntimeError(
                        f"Source environment '{source_env}' is {source_state}. Wait until it is active."
                    )

                ensure_target_env_exists(db, project, envs, source_env, target_env)
                promote_env(project, source_env, target_env)

                envs[target_env].status = "active"
                envs[target_env].last_error = ""

                if normalized_mode == "move":
                    reset_env(project, source_env)
                    envs[source_env].status = "active"
                    envs[source_env].last_error = ""
                    record_build_event(
                        project.id,
                        source_env,
                        "",
                        "",
                        "reset",
                        f"{source_env} reset after move to {target_env}",
                    )
                db.commit()

            set_env_status(project_id, target_env, "active", "")
            if normalized_mode == "move":
                set_env_status(project_id, source_env, "active", "")

            final_status = "promoted"
            if normalized_mode == "move":
                final_status = "moved"
            if target_env == "dev":
                final_status = "reset"

            record_build_event(
                project_id,
                target_env,
                "",
                "",
                final_status,
                action_message,
            )
        except Exception as exc:
            set_env_status(project_id, target_env, "failed", str(exc))
            record_build_event(
                project_id,
                target_env,
                "",
                "",
                "failed",
                f"{action_message} failed: {str(exc)[:180]}",
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    maybe_reconcile_cluster_projects(user)

    error = request.query_params.get("error")
    notice = request.query_params.get("notice")
    prefill_slug = request.query_params.get("slug", "")
    prefill_display = request.query_params.get("display_name", "")
    prefill_hosting_location = normalize_hosting_location(request.query_params.get("hosting_location", ""))
    prefill_hosting_ping = parse_ping_ms(request.query_params.get("hosting_ping_ms", ""))
    hosting_locations, hosting_location_map, _ = hosting_catalog()

    with db_session() as db:
        if user.is_admin:
            projects = db.query(Project).all()
        else:
            projects = db.query(Project).filter(Project.owner_id == user.id).all()
        projects.sort(key=lambda p: p.created_at, reverse=True)
        stale_project_count = sum(
            1
            for project in projects
            if (project.status or "").strip().lower() in {"deleting", "delete_failed"}
        )
        env_order = {"prod": 0, "staging": 1, "dev": 2}
        for project in projects:
            project.envs.sort(key=lambda e: env_order.get(e.name, 99))
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "projects": projects,
                "base_domain": BASE_DOMAIN,
                "error": error,
                "notice": notice,
                "prefill_slug": prefill_slug,
                "prefill_display": prefill_display,
                "prefill_hosting_location": prefill_hosting_location,
                "prefill_hosting_ping": prefill_hosting_ping,
                "hosting_locations": hosting_locations,
                "hosting_location_map": hosting_location_map,
                "stale_project_count": stale_project_count,
            },
        )


@app.get("/projects", response_class=HTMLResponse)
def projects_index(request: Request):
    return index(request)


@app.get("/api/hosting/availability")
def hosting_availability(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    force = (request.query_params.get("force") or "").strip().lower() in {"1", "true", "yes"}
    return JSONResponse(hosting_capacity_snapshot(force=force))


@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    if current_user(request):
        return RedirectResponse("/")
    error = request.query_params.get("error")
    notice = request.query_params.get("notice")
    oauth_ready = bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET and hasattr(oauth, "github"))
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": error,
            "notice": notice,
            "oauth_ready": oauth_ready,
        },
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/github/install")
def github_install(request: Request):
    return RedirectResponse(github_install_url())


@app.get("/auth/github")
async def auth_github(request: Request):
    if not GITHUB_CLIENT_ID or not GITHUB_CLIENT_SECRET:
        return redirect_with_error("/login", "GitHub OAuth is not configured.")
    try:
        client = oauth.github
    except AttributeError:
        return redirect_with_error("/login", "GitHub OAuth client is not available.")
    redirect_uri = request.url_for("auth_callback")
    try:
        return await client.authorize_redirect(request, redirect_uri)
    except Exception:
        return redirect_with_error(
            "/login",
            "GitHub OAuth redirect failed. Verify client ID, secret, and callback URL.",
        )


@app.get("/auth/github/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.github.authorize_access_token(request)
    except MismatchingStateError:
        request.session.clear()
        return redirect_with_error("/login", "Login session expired. Please try again.")
    except Exception:
        request.session.clear()
        return redirect_with_error("/login", "GitHub callback failed. Please retry sign in.")
    resp = await oauth.github.get("user", token=token)
    profile = resp.json()

    username = profile.get("login")
    github_id = str(profile.get("id"))
    name = profile.get("name")
    email = await github_primary_email(token, profile)
    if not username or not github_id:
        return HTMLResponse("GitHub login failed", status_code=400)
    admin_match = is_master_admin_user(email)

    with db_session() as db:
        user = db.query(User).filter(User.github_id == github_id).first()
        if not user:
            user = User(
                github_id=github_id,
                username=username,
                email=email or None,
                name=name,
                is_admin=admin_match,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        else:
            user.username = username
            user.name = name
            if email:
                user.email = email
            user.is_admin = bool(admin_match)
            db.commit()
            db.refresh(user)
        request.session["user_id"] = user.id
    return RedirectResponse("/")


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    if not GITHUB_WEBHOOK_SECRET:
        return HTMLResponse("Webhook secret not configured", status_code=500)
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_github_signature(GITHUB_WEBHOOK_SECRET, body, signature):
        return HTMLResponse("Invalid signature", status_code=401)

    event = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()

    if event == "ping":
        return {"ok": True}

    if event in {"installation", "installation_repositories"}:
        installation = payload.get("installation", {})
        installation_id = str(installation.get("id") or "")
        account = installation.get("account") or payload.get("sender") or {}
        account_login = account.get("login") or ""
        account_id = str(account.get("id") or "")
        if installation_id and account_login:
            with db_session() as db:
                existing = db.query(Installation).filter(
                    Installation.installation_id == installation_id
                ).first()
                if existing:
                    existing.account_login = account_login
                    existing.account_id = account_id
                else:
                    db.add(
                        Installation(
                            installation_id=installation_id,
                            account_login=account_login,
                            account_id=account_id,
                        )
                    )
                db.commit()
            try:
                sync_installation_repos(installation_id)
            except Exception:
                pass
        return {"ok": True}

    if event == "push":
        repo = payload.get("repository", {})
        full_name = repo.get("full_name")
        ref = payload.get("ref", "")
        branch = branch_from_ref(ref)
        sha = payload.get("after") or ""

        if full_name and branch:
            with db_session() as db:
                project = db.query(Project).filter(Project.repo_full_name == full_name).first()
                if project:
                    env = env_for_project_branch(project, branch)
                    if env:
                        try:
                            if env == "dev":
                                reset_env(project, env)
                                msg = f"Reset dev on push to {branch}"
                            else:
                                restart_env(project.slug, env)
                                msg = f"Restarted {env} on push to {branch}"
                            record_build_event(project.id, env, branch, sha, "deployed", msg)
                        except Exception as exc:
                            record_build_event(
                                project.id,
                                env,
                                branch,
                                sha,
                                "deploy_failed",
                                f"Push deploy failed for {env}: {str(exc)[:180]}",
                            )
        return {"ok": True}

    if event == "create":
        repo = payload.get("repository", {})
        full_name = str(repo.get("full_name") or "").strip()
        ref_type = str(payload.get("ref_type") or "").strip().lower()
        branch = str(payload.get("ref") or "").strip()
        sha = str(payload.get("after") or "")

        if full_name and ref_type == "branch" and branch:
            with db_session() as db:
                project = db.query(Project).filter(Project.repo_full_name == full_name).first()
                project_id = int(project.id) if project else 0
                env_name = env_for_project_branch(project, branch) if project else ""
            if project_id and env_name:
                def _handle_branch_create():
                    outcome = queue_env_provision_for_branch(project_id, env_name)
                    if outcome == "queued":
                        record_build_event(
                            project_id,
                            env_name,
                            branch,
                            sha,
                            "branch_created",
                            f"Branch '{branch}' created. Provisioning {env_name}.",
                        )
                    elif outcome in {"already_active", "already_provisioning"}:
                        record_build_event(
                            project_id,
                            env_name,
                            branch,
                            sha,
                            "branch_skipped",
                            f"Branch '{branch}' created. {env_name} is already {outcome.replace('already_', '')}.",
                        )
                    else:
                        record_build_event(
                            project_id,
                            env_name,
                            branch,
                            sha,
                            "branch_failed",
                            f"Branch '{branch}' create handling failed: {outcome}",
                        )

                spawn_webhook_task(_handle_branch_create)
        return {"ok": True}

    if event == "delete":
        repo = payload.get("repository", {})
        full_name = str(repo.get("full_name") or "").strip()
        ref_type = str(payload.get("ref_type") or "").strip().lower()
        branch = str(payload.get("ref") or "").strip()

        if full_name and ref_type == "branch" and branch:
            with db_session() as db:
                project = db.query(Project).filter(Project.repo_full_name == full_name).first()
                project_id = int(project.id) if project else 0
                env_name = env_for_project_branch(project, branch) if project else ""
            if project_id and env_name:
                def _handle_branch_delete():
                    outcome = delete_env_for_branch(project_id, env_name)
                    if outcome == "deleted":
                        record_build_event(
                            project_id,
                            env_name,
                            branch,
                            "",
                            "branch_deleted",
                            f"Branch '{branch}' deleted. {env_name} environment removed.",
                        )
                    elif outcome == "missing_env":
                        record_build_event(
                            project_id,
                            env_name,
                            branch,
                            "",
                            "branch_skipped",
                            f"Branch '{branch}' deleted. No {env_name} environment was present.",
                        )
                    else:
                        record_build_event(
                            project_id,
                            env_name,
                            branch,
                            "",
                            "branch_failed",
                            f"Branch '{branch}' delete handling failed: {outcome}",
                        )

                spawn_webhook_task(_handle_branch_delete)
        return {"ok": True}

    if event == "pull_request":
        action = str(payload.get("action") or "").strip().lower()
        pr = payload.get("pull_request") or {}
        merged = bool(pr.get("merged"))
        if action == "closed" and merged:
            base = pr.get("base") or {}
            head = pr.get("head") or {}
            base_ref = str(base.get("ref") or "").strip()
            head_ref = str(head.get("ref") or "").strip()
            repo = payload.get("repository") or (base.get("repo") or {})
            full_name = str(repo.get("full_name") or "").strip()
            merge_sha = str(pr.get("merge_commit_sha") or "")

            if full_name and base_ref and head_ref:
                with db_session() as db:
                    project = db.query(Project).filter(Project.repo_full_name == full_name).first()
                    project_id = int(project.id) if project else 0
                    source_env = env_for_project_branch(project, head_ref) if project else ""
                    target_env = env_for_project_branch(project, base_ref) if project else ""
                if project_id and source_env and target_env and source_env != target_env:
                    def _handle_merge():
                        outcome = promote_on_branch_merge(project_id, source_env, target_env)
                        if outcome == "promoted":
                            record_build_event(
                                project_id,
                                target_env,
                                base_ref,
                                merge_sha,
                                "promoted",
                                f"Auto-promoted {source_env} → {target_env} after merge {head_ref} → {base_ref}.",
                            )
                        else:
                            record_build_event(
                                project_id,
                                target_env,
                                base_ref,
                                merge_sha,
                                "promotion_failed",
                                f"Auto-promotion on merge {head_ref} → {base_ref} failed: {outcome}",
                            )

                    spawn_webhook_task(_handle_merge)
        return {"ok": True}

    return {"ok": True}


@app.post("/projects")
def create_project(
    request: Request,
    slug: str = Form(...),
    display_name: str = Form(None),
    hosting_location: str = Form(""),
    hosting_ping_ms: str = Form(""),
    hosting_probe_results: str = Form(""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    raw_slug = (slug or "").strip().lower()
    raw_display = (display_name or "").strip()
    selected_hosting_location = normalize_hosting_location(hosting_location)
    selected_hosting_ping = parse_ping_ms(hosting_ping_ms)
    probe_results = parse_hosting_probe_results(hosting_probe_results)
    if (
        selected_hosting_location
        and selected_hosting_ping is not None
        and selected_hosting_location not in probe_results
    ):
        probe_results[selected_hosting_location] = selected_hosting_ping
    fallback_notice = ""

    try:
        ensure_prereqs()
    except RuntimeError as exc:
        return redirect_with_error(
            "/",
            str(exc),
            {
                "slug": (slug or "").strip(),
                "display_name": (display_name or "").strip(),
                "hosting_location": selected_hosting_location,
                "hosting_ping_ms": selected_hosting_ping or "",
            },
        )
    slug = re.sub(r"[^a-z0-9-]", "-", raw_slug)
    slug = re.sub(r"-+", "-", slug).strip("-")

    if not SLUG_RE.match(slug):
        return redirect_with_error(
            "/",
            "Slug must be 2–31 chars, lowercase letters/numbers, and hyphens only.",
            {
                "slug": slug,
                "display_name": raw_display,
                "hosting_location": selected_hosting_location,
                "hosting_ping_ms": selected_hosting_ping or "",
            },
        )

    hosting_locations, hosting_location_map, availability = hosting_catalog()
    availability_mode = str(availability.get("mode") or "")
    availability_locations = availability.get("locations") or {}
    if selected_hosting_location and selected_hosting_location not in hosting_location_map:
        selected_hosting_location = ""
        selected_hosting_ping = None

    if availability_mode == "active":
        available_codes = [
            item["code"]
            for item in hosting_locations
            if bool((availability_locations.get(item["code"]) or {}).get("available"))
        ]
        if not available_codes:
            return redirect_with_error(
                "/",
                "No OVH location currently has free quota. Increase OVH quota and retry.",
                {
                    "slug": slug,
                    "display_name": raw_display,
                    "hosting_location": selected_hosting_location,
                    "hosting_ping_ms": selected_hosting_ping or "",
                },
            )

        if not selected_hosting_location or selected_hosting_location not in available_codes:
            chosen = best_location_by_ping(available_codes, probe_results, hosting_locations)
            if chosen:
                if selected_hosting_location and selected_hosting_location != chosen:
                    from_label = hosting_location_label(selected_hosting_location, hosting_location_map)
                    reason = (
                        (availability_locations.get(selected_hosting_location) or {}).get("reason")
                        or "No free quota"
                    )
                    to_label = hosting_location_label(chosen, hosting_location_map)
                    fallback_notice = f"{from_label} has no OVH capacity ({reason}). Switched to {to_label}."
                selected_hosting_location = chosen

        if selected_hosting_location and selected_hosting_ping is None:
            selected_hosting_ping = probe_results.get(selected_hosting_location)

    elif not selected_hosting_location:
        auto_code = best_location_by_ping(list(probe_results.keys()), probe_results, hosting_locations)
        if auto_code:
            selected_hosting_location = auto_code
            selected_hosting_ping = probe_results.get(auto_code)

    with db_session() as db:
        if db.query(Project).filter(Project.slug == slug).first():
            return redirect_with_error(
                "/",
                "Project already exists.",
                {
                    "slug": slug,
                    "display_name": raw_display,
                    "hosting_location": selected_hosting_location,
                    "hosting_ping_ms": selected_hosting_ping or "",
                },
            )
        had_tombstone = (
            db.query(ProjectTombstone).filter(ProjectTombstone.slug == slug).first() is not None
        )

    # If this slug existed before, purge stale artifacts/DB names before creating the new project.
    if had_tombstone:
        cleanup_project_slug_artifacts(slug)
        purge_project_databases(slug)

    with db_session() as db:
        if db.query(Project).filter(Project.slug == slug).first():
            return redirect_with_error(
                "/",
                "Project already exists.",
                {
                    "slug": slug,
                    "display_name": raw_display,
                    "hosting_location": selected_hosting_location,
                    "hosting_ping_ms": selected_hosting_ping or "",
                },
            )
        remove_project_tombstone(db, slug)
        project = Project(
            slug=slug,
            display_name=(raw_display or slug).strip()[:80],
            dev_branch=DEFAULT_DEV_BRANCH,
            staging_branch=DEFAULT_STAGING_BRANCH,
            prod_branch=DEFAULT_PROD_BRANCH,
            workers=DEFAULT_WORKERS,
            storage_gb=DEFAULT_STORAGE_GB,
            staging_slots=DEFAULT_STAGING_SLOTS,
            subscription_code="",
            odoo_version=DEFAULT_ODOO_VERSION,
            hosting_location=selected_hosting_location or None,
            hosting_ping_ms=selected_hosting_ping,
            status="active",
            last_error="",
            owner_id=user.id,
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        # Create dev record immediately, then provision in background.
        db.add(
            Environment(
                project_id=project.id,
                name="dev",
                host=host_for(project.slug, "dev"),
                db_name=db_name_for(project.slug, "dev"),
                workers=project.workers or DEFAULT_WORKERS,
                storage_gb=project.storage_gb or DEFAULT_STORAGE_GB,
                odoo_version=project.odoo_version or DEFAULT_ODOO_VERSION,
                status="provisioning",
                last_error="",
            )
        )
        db.commit()
        created_project_id = project.id

    record_build_event(
        created_project_id,
        "dev",
        "",
        "",
        "queued",
        "Project created. Dev provisioning queued.",
    )
    spawn_env_provision(created_project_id, "dev")
    spawn_repo_bootstrap(created_project_id, user.id)
    if fallback_notice:
        return RedirectResponse(
            f"/projects/{created_project_id}/settings?{urlencode({'notice': fallback_notice})}#env-dev",
            status_code=303,
        )
    return RedirectResponse(f"/projects/{created_project_id}/settings#env-dev", status_code=303)


@app.post("/projects/{project_id}/delete")
def delete_project(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    with db_session() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return RedirectResponse("/")
        if not user.is_admin and project.owner_id != user.id:
            return HTMLResponse("Forbidden", status_code=403)
        if project.status == "deleting":
            return redirect_with_notice("/", f"Deletion is already running for {project.display_name or project.slug}.")
        project.status = "deleting"
        project.last_error = ""
        upsert_project_tombstone(db, project.slug, "delete")
        db.commit()
        project_name = project.display_name or project.slug

    spawn_project_delete(project_id)
    return redirect_with_notice("/", f"Deletion started for {project_name}.")


@app.post("/projects/{project_id}/delete/force")
def force_delete_project(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    with db_session() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return RedirectResponse("/")
        if not user.is_admin and project.owner_id != user.id:
            return HTMLResponse("Forbidden", status_code=403)

        slug = project.slug
        project_name = project.display_name or slug
        env_domains = {
            env.name: [domain.host for domain in env.domains]
            for env in list(project.envs)
        }
        upsert_project_tombstone(db, slug, "force-delete")

        db.delete(project)
        db.commit()

    spawn_orphan_cleanup(slug, env_domains)
    return redirect_with_notice("/", f"{project_name} removed from dashboard. Cleanup continues in background.")


@app.post("/projects/cleanup/stale")
def cleanup_stale_projects(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    payloads: list[tuple[str, dict[str, list[str]]]] = []
    removed = 0
    with db_session() as db:
        query = db.query(Project).filter(Project.status.in_(["deleting", "delete_failed"]))
        if not user.is_admin:
            query = query.filter(Project.owner_id == user.id)
        stale_projects = query.all()
        for project in stale_projects:
            slug = project.slug
            env_domains = {
                env.name: [domain.host for domain in env.domains]
                for env in list(project.envs)
            }
            upsert_project_tombstone(db, slug, "stale-cleanup")
            payloads.append((slug, env_domains))
            db.delete(project)
            removed += 1
        db.commit()

    for slug, env_domains in payloads:
        spawn_orphan_cleanup(slug, env_domains)

    if removed == 0:
        return redirect_with_notice("/", "No stale deletions found.")
    return redirect_with_notice(
        "/",
        f"Removed {removed} stale project(s) from dashboard. Cleanup continues in background.",
    )


@app.post("/admin/maintenance/purge-old")
def admin_purge_old_artifacts(
    request: Request,
    backups_mode: str = Form("none"),
    backup_retention_days: int = Form(2),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not user.is_admin:
        return HTMLResponse("Forbidden", status_code=403)

    selected_backups_mode = (backups_mode or "none").strip().lower()
    if selected_backups_mode not in {"none", "retention", "purge_all"}:
        selected_backups_mode = "none"
    safe_retention = max(1, min(3650, int(backup_retention_days or 2)))

    orphan_dbs_dropped = 0
    with db_session() as db:
        live_slugs = {
            (slug or "").strip().lower()
            for (slug,) in db.query(Project.slug).all()
            if slug
        }
        tombstoned_slugs = {
            (slug or "").strip().lower()
            for (slug,) in db.query(ProjectTombstone.slug).all()
            if slug
        }

        stale_projects = db.query(Project).filter(Project.status.in_(["deleting", "delete_failed"])).all()
        stale_slugs = set()
        for project in stale_projects:
            stale_slugs.add(project.slug)
            upsert_project_tombstone(db, project.slug, "maintenance-purge")
            db.delete(project)
        db.commit()

        live_slugs = {
            (slug or "").strip().lower()
            for (slug,) in db.query(Project.slug).all()
            if slug
        }

        current_project_ids = [pid for (pid,) in db.query(Project.id).all() if pid is not None]
        if current_project_ids:
            db.query(BuildEvent).filter(
                ~BuildEvent.project_id.in_(current_project_ids)
            ).delete(synchronize_session=False)
        else:
            db.query(BuildEvent).delete(synchronize_session=False)
        db.commit()

    try:
        orphan_dbs_dropped = purge_orphan_project_databases(live_slugs)
    except Exception:
        orphan_dbs_dropped = 0

    orphan_slugs = set()
    try:
        discovered = discover_cluster_projects()
        orphan_slugs = {slug for slug in discovered.keys() if slug not in live_slugs}
    except Exception:
        orphan_slugs = set()

    cleanup_slugs = sorted(
        {
            slug
            for slug in {*(tombstoned_slugs | stale_slugs | orphan_slugs)}
            if slug and SLUG_RE.match(slug)
        }
    )

    spawn_maintenance_cleanup(
        slugs=cleanup_slugs,
        backups_mode=selected_backups_mode,
        backup_retention_days=safe_retention,
    )

    backup_part = "without backup cleanup"
    if selected_backups_mode == "purge_all":
        backup_part = "and purging ALL backup snapshots"
    elif selected_backups_mode == "retention":
        backup_part = f"and purging backup snapshots older than {safe_retention} day(s)"

    return redirect_with_notice(
        "/",
        f"Maintenance cleanup started for {len(cleanup_slugs)} project slug(s), dropped {orphan_dbs_dropped} orphan database(s), {backup_part}. 404 on deleted subdomains is expected.",
    )


def get_project_for_user(db, user: User, project_id: int) -> Optional[Project]:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return None
    if not user.is_admin and project.owner_id != user.id:
        return None
    return project


def ensure_project_mutable(project: Project, path: str):
    if (project.status or "active") == "deleting":
        return redirect_with_notice(path, "Project deletion is in progress.")
    return None


@app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if user.is_admin:
        maybe_reconcile_cluster_projects(user)
    error = request.query_params.get("error")
    notice = request.query_params.get("notice")
    selected_env = (request.query_params.get("env") or "").strip().lower()
    selected_tab = (request.query_params.get("tab") or "history").strip().lower()
    if selected_tab not in {"history", "logs", "settings"}:
        selected_tab = "history"
    hosting_locations, hosting_location_map, _ = hosting_catalog()
    if github_app_enabled():
        try:
            sync_installations_from_github(force=False, sync_repos=False)
        except Exception:
            pass
    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        db.refresh(project)
        env_order = {"prod": 0, "staging": 1, "dev": 2}
        project.envs.sort(key=lambda e: env_order.get(e.name, 99))
        env_by_name = {e.name: e for e in project.envs}
        if selected_env not in {"dev", "staging", "prod"}:
            for fallback in ("dev", "staging", "prod"):
                if fallback in env_by_name:
                    selected_env = fallback
                    break
            if not selected_env:
                selected_env = "dev"

        installations = candidate_installations_for_user(db, user)
        if project.installation_id and not any(
            inst.installation_id == project.installation_id for inst in installations
        ):
            pinned = db.query(Installation).filter(
                Installation.installation_id == project.installation_id
            ).first()
            if pinned:
                installations = [pinned, *installations]

        install_ids = [i.installation_id for i in installations]
        repos = []
        if install_ids:
            repos = (
                db.query(Repository)
                .filter(Repository.installation_id.in_(install_ids))
                .order_by(Repository.full_name.asc())
                .all()
            )
        recent_builds = (
            db.query(BuildEvent)
            .filter(BuildEvent.project_id == project.id)
            .order_by(BuildEvent.created_at.desc())
            .limit(60)
            .all()
        )
        env_status = {}
        for env in project.envs:
            if (project.status or "active") == "deleting":
                env_status[env.name] = {"ready": False, "message": "Deleting resources"}
                continue
            if env.name == "dev":
                try:
                    ensure_odoo_init(project, env.name, env)
                except Exception as exc:
                    env.status = "failed"
                    env.last_error = str(exc)[:500]
                    db.commit()
            env_status[env.name] = env_runtime_status(project, env.name)

        selected_env_obj = env_by_name.get(selected_env)
        branch_history = [
            build
            for build in recent_builds
            if (build.env or "").strip() in {"", selected_env}
        ]
        if selected_env_obj and not branch_history:
            runtime_msg = (env_status.get(selected_env) or {}).get("message") or ""
            seed_status = (selected_env_obj.status or "active").strip().lower() or "active"
            seed_message = runtime_msg
            if not seed_message:
                if seed_status == "failed" and selected_env_obj.last_error:
                    seed_message = selected_env_obj.last_error
                else:
                    seed_message = f"{selected_env} environment state: {seed_status}"
            last_env_event = (
                db.query(BuildEvent)
                .filter(BuildEvent.project_id == project.id, BuildEvent.env == selected_env)
                .order_by(BuildEvent.created_at.desc(), BuildEvent.id.desc())
                .first()
            )
            if not last_env_event:
                db.add(
                    BuildEvent(
                        project_id=project.id,
                        env=selected_env,
                        branch="",
                        sha="",
                        status=seed_status,
                        message=seed_message[:500],
                    )
                )
                db.commit()
                recent_builds = (
                    db.query(BuildEvent)
                    .filter(BuildEvent.project_id == project.id)
                    .order_by(BuildEvent.created_at.desc())
                    .limit(60)
                    .all()
                )
                branch_history = [
                    build
                    for build in recent_builds
                    if (build.env or "").strip() in {"", selected_env}
                ]
        branch_logs = ""
        if selected_tab == "logs":
            if selected_env_obj:
                branch_logs = env_logs_tail(project.slug, selected_env, lines=260)
            else:
                branch_logs = f"{selected_env} environment has not been created yet."

        current_loc = normalize_hosting_location(project.hosting_location or "")
        if current_loc and current_loc not in hosting_location_map:
            hosting_locations.append(
                {
                    "code": current_loc,
                    "label": hosting_location_label(current_loc, hosting_location_map),
                    "region": "Saved",
                    "ovh_region": current_loc.upper(),
                    "probe_url": "",
                }
            )
            hosting_location_map = hosting_location_map_from_list(hosting_locations)

        return templates.TemplateResponse(
            "project_settings.html",
            {
                "request": request,
                "user": user,
                "project": project,
                "base_domain": BASE_DOMAIN,
                "installations": installations,
                "repos": repos,
                "builds": recent_builds[:10],
                "branch_history": branch_history[:40],
                "selected_env": selected_env,
                "selected_tab": selected_tab,
                "selected_env_obj": selected_env_obj,
                "branch_logs": branch_logs,
                "github_install_url": github_install_url(),
                "error": error,
                "notice": notice,
                "env_status": env_status,
                "hosting_locations": hosting_locations,
                "hosting_location_map": hosting_location_map,
            },
        )


@app.get("/projects/{project_id}/env/{env_name}/status")
def project_env_status(request: Request, project_id: int, env_name: str):
    user = current_user(request)
    if not user:
        return JSONResponse({"ready": False, "message": "Unauthorized"}, status_code=401)
    if env_name not in {"dev", "staging", "prod"}:
        return JSONResponse({"ready": False, "message": "Invalid environment"}, status_code=400)

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return JSONResponse({"ready": False, "message": "Not found"}, status_code=404)
        if (project.status or "active") == "deleting":
            return JSONResponse({"ready": False, "message": "Project deletion in progress"}, status_code=409)
        env = db.query(Environment).filter(
            Environment.project_id == project.id, Environment.name == env_name
        ).first()
        if not env:
            return JSONResponse({"ready": False, "message": "Not created yet"}, status_code=404)

        if env_name == "dev":
            try:
                ensure_odoo_init(project, env_name, env)
            except Exception as exc:
                set_env_status(project.id, env_name, "failed", str(exc))

        status = env_runtime_status(project, env_name)
        status_message = (status.get("message") or "").strip()
        if status_message.lower().startswith("init failed") and (env.status or "").strip().lower() != "failed":
            set_env_status(project.id, env_name, "failed", status_message)
        return JSONResponse(
            {
                "ready": bool(status.get("ready")),
                "message": status_message,
                "host": env.host,
            }
        )


@app.get("/projects/{project_id}/env/{env_name}/open", response_class=HTMLResponse)
def project_env_open(request: Request, project_id: int, env_name: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if env_name not in {"dev", "staging", "prod"}:
        return HTMLResponse("Invalid environment", status_code=400)

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        if (project.status or "active") == "deleting":
            return redirect_with_notice(f"/projects/{project_id}/settings", "Project deletion is in progress.")
        env = db.query(Environment).filter(
            Environment.project_id == project.id, Environment.name == env_name
        ).first()
        if not env:
            return HTMLResponse("Environment not created yet", status_code=404)

        if env_name == "dev":
            try:
                ensure_odoo_init(project, env_name, env)
            except Exception as exc:
                set_env_status(project.id, env_name, "failed", str(exc))

        status = env_runtime_status(project, env_name)
        if status.get("ready"):
            return RedirectResponse(f"https://{env.host}")

        return templates.TemplateResponse(
            "env_loading.html",
            {
                "request": request,
                "user": user,
                "project": project,
                "env": env,
                "env_name": env_name,
                "status": status,
            },
        )


@app.post("/projects/{project_id}/settings")
def update_project_settings(
    request: Request,
    project_id: int,
    display_name: str = Form(...),
    dev_branch: str = Form(...),
    staging_branch: str = Form(...),
    prod_branch: str = Form(...),
    workers: int = Form(...),
    storage_gb: int = Form(...),
    staging_slots: int = Form(...),
    subscription_code: str = Form(""),
    odoo_version: str = Form(DEFAULT_ODOO_VERSION),
    hosting_location: str = Form(""),
    hosting_ping_ms: str = Form(""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    hosting_locations, hosting_location_map, _ = hosting_catalog()

    try:
        with db_session() as db:
            project = get_project_for_user(db, user, project_id)
            if not project:
                return HTMLResponse("Not found", status_code=404)
            locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
            if locked:
                return locked

            new_workers = max(1, int(workers))
            new_storage = max(1, int(storage_gb))
            new_odoo_version = (odoo_version.strip() or DEFAULT_ODOO_VERSION)

            # PVC size cannot be decreased; enforce at project level.
            max_current_storage = max(
                [project.storage_gb or DEFAULT_STORAGE_GB]
                + [(env.storage_gb or project.storage_gb or DEFAULT_STORAGE_GB) for env in list(project.envs)]
            )
            if new_storage < max_current_storage:
                return redirect_with_error(
                    f"/projects/{project_id}/settings",
                    f"Filestore size cannot be decreased below {max_current_storage}Gi.",
                )

            project.display_name = display_name.strip()[:80] or project.slug
            project.dev_branch = dev_branch.strip() or DEFAULT_DEV_BRANCH
            project.staging_branch = staging_branch.strip() or DEFAULT_STAGING_BRANCH
            project.prod_branch = prod_branch.strip() or DEFAULT_PROD_BRANCH
            project.workers = new_workers
            project.storage_gb = new_storage
            project.staging_slots = max(1, int(staging_slots))
            project.subscription_code = subscription_code.strip()[:120]
            project.odoo_version = new_odoo_version
            normalized_hosting = normalize_hosting_location(hosting_location)
            if normalized_hosting and normalized_hosting not in hosting_location_map:
                normalized_hosting = ""
            project.hosting_location = normalized_hosting or None
            project.hosting_ping_ms = parse_ping_ms(hosting_ping_ms)
            db.commit()
            db.refresh(project)

            # Reconcile existing environments with new settings.
            for env in list(project.envs):
                env.workers = project.workers
                env.storage_gb = project.storage_gb
                env.odoo_version = project.odoo_version
                host, db_name = provision_env(project, env.name, env)
                env.host = host
                env.db_name = db_name
                env.last_error = ""
                if env.status == "provisioning":
                    env.status = "active"
            db.commit()
    except Exception as exc:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            f"Saving project settings failed: {str(exc)[:200]}",
        )

    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)


@app.post("/projects/{project_id}/envs/{env_id}/settings")
def update_env_settings(
    request: Request,
    project_id: int,
    env_id: int,
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    try:
        with db_session() as db:
            project = get_project_for_user(db, user, project_id)
            if not project:
                return HTMLResponse("Not found", status_code=404)
            locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
            if locked:
                return locked
            env = db.query(Environment).filter(Environment.id == env_id).first()
            if not env or env.project_id != project.id:
                return redirect_with_error(f"/projects/{project_id}/settings", "Invalid environment.")
            return redirect_with_notice(
                f"/projects/{project_id}/settings",
                "Database workers, filestore size, and Odoo version are managed from Project Settings only.",
                {"env": env.name, "tab": "settings"},
            )
    except Exception as exc:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            f"Saving environment settings failed: {str(exc)[:200]}",
        )
    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)


@app.post("/projects/{project_id}/repo")
def connect_repo(
    request: Request,
    project_id: int,
    repo_full_name: str = Form(...),
    installation_id: str = Form(""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    repo_full_name = repo_full_name.strip()
    auto_bootstrap = not repo_full_name
    if github_app_enabled():
        try:
            sync_installations_from_github(force=True, sync_repos=False)
        except Exception:
            pass

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return redirect_with_error("/projects", "Project not found.")
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked

        if not installation_id:
            installs = candidate_installations_for_user(db, user)
            if len(installs) == 1:
                installation_id = installs[0].installation_id

        if not installation_id:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                "Installation is required. Install/select GitHub App first.",
            )

        installation = db.query(Installation).filter(
            Installation.installation_id == installation_id
        ).first()
        if not installation:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                "Selected installation is not available. Reinstall GitHub App and retry.",
            )

        try:
            target_repo = repo_full_name
            allow_create = False
            if auto_bootstrap:
                target_repo = f"{installation.account_login}/{project.slug}"
                allow_create = True
            elif "/" not in target_repo:
                return redirect_with_error(
                    f"/projects/{project_id}/settings",
                    "Repository must be in owner/name format.",
                )

            repo, created_repo, branches_created = connect_or_create_project_repo(
                project=project,
                installation=installation,
                repo_full_name=target_repo,
                allow_create=allow_create,
            )
        except Exception as exc:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"Repository connect failed: {str(exc)[:220]}",
            )

        project.repo_full_name = str(repo.get("full_name") or target_repo)
        project.repo_id = str(repo.get("id"))
        project.installation_id = str(installation_id)
        db.commit()

    details = []
    if auto_bootstrap and created_repo:
        details.append("created")
    if branches_created:
        details.append(f"branches: {', '.join(branches_created)}")
    detail_msg = f" ({'; '.join(details)})" if details else ""
    return redirect_with_notice(
        f"/projects/{project_id}/settings",
        f"Repository connected: {project.repo_full_name}{detail_msg}",
    )


@app.post("/projects/{project_id}/domains")
def add_domain(
    request: Request,
    project_id: int,
    env_id: int = Form(...),
    host: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    host = host.strip().lower()
    if not host or not re.match(r"^[a-z0-9][a-z0-9.-]+$", host):
        return HTMLResponse("Invalid domain", status_code=400)

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked
        env = db.query(Environment).filter(Environment.id == env_id).first()
        if not env or env.project_id != project.id:
            return HTMLResponse("Invalid environment", status_code=400)

        if db.query(Domain).filter(Domain.host == host).first():
            return HTMLResponse("Domain already exists", status_code=400)

        domain = Domain(env_id=env.id, host=host)
        db.add(domain)
        db.commit()
        db.refresh(domain)

        apply_domain_ingress(env, host)

    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)


@app.post("/projects/{project_id}/promote")
def promote_project_env(
    request: Request,
    project_id: int,
    source_env: str = Form(...),
    target_env: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    if source_env not in {"dev", "staging", "prod"} or target_env not in {"dev", "staging", "prod"}:
        return redirect_with_error(f"/projects/{project_id}/settings", "Invalid environment.")
    if source_env == target_env:
        return redirect_with_error(f"/projects/{project_id}/settings", "Invalid promotion.")

    allowed = {("dev", "staging"), ("staging", "prod"), ("dev", "prod")}
    if (source_env, target_env) not in allowed:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            "Promotion order must be dev → staging/prod or staging → prod.",
        )

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked

        envs = {e.name: e for e in project.envs}
        if source_env not in envs:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"Source environment '{source_env}' is missing. Create it first.",
            )
        source_state = (envs[source_env].status or "").strip().lower()
        if source_state in {"provisioning", "failed"}:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"Source environment '{source_env}' is {source_state}. Wait until it is active before promotion.",
                {"env": source_env, "tab": "history"},
            )

    mode = "move" if target_env == "prod" and source_env in {"dev", "staging"} else "copy"
    spawn_env_transfer(project_id, source_env, target_env, mode=mode, trigger="button")
    notice = f"Promotion started: {source_env} → {target_env}"
    if mode == "move":
        notice = f"Move to live started: {source_env} → {target_env} (source will be reset)"
    return redirect_with_notice(
        f"/projects/{project_id}/settings",
        notice,
        {"env": target_env, "tab": "history"},
    )


@app.post("/projects/{project_id}/branches/transfer")
def transfer_branch_workspace(
    request: Request,
    project_id: int,
    source_env: str = Form(...),
    target_env: str = Form(...),
    mode: str = Form("copy"),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    source_env = (source_env or "").strip().lower()
    target_env = (target_env or "").strip().lower()
    mode = (mode or "copy").strip().lower()
    if source_env not in {"dev", "staging", "prod"} or target_env not in {"dev", "staging", "prod"}:
        return redirect_with_error(f"/projects/{project_id}/settings", "Invalid environment.")
    if source_env == target_env:
        return redirect_with_error(f"/projects/{project_id}/settings", "Source and target are the same.")
    if mode not in {"copy", "move"}:
        mode = "copy"
    if mode == "move" and target_env != "prod":
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            "Move mode is allowed only when target is prod.",
        )
    if mode == "move" and source_env == "prod":
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            "Cannot move from prod.",
        )

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked
        envs = {e.name: e for e in project.envs}
        if source_env not in envs:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"Source environment '{source_env}' is missing.",
                {"env": target_env, "tab": "history"},
            )
        source_state = (envs[source_env].status or "").strip().lower()
        if source_state in {"provisioning", "failed"}:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"Source environment '{source_env}' is {source_state}.",
                {"env": source_env, "tab": "history"},
            )

    spawn_env_transfer(project_id, source_env, target_env, mode=mode, trigger="drag-drop")
    notice = f"Copy started: {source_env} → {target_env}"
    if target_env == "dev":
        notice = f"Reset started: dev from {source_env}"
    elif mode == "move":
        notice = f"Move to live started: {source_env} → {target_env}"

    return redirect_with_notice(
        f"/projects/{project_id}/settings",
        notice,
        {"env": target_env, "tab": "history"},
    )


@app.post("/projects/{project_id}/reset")
def reset_project_env(
    request: Request,
    project_id: int,
    env: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if env not in {"dev"}:
        return redirect_with_error(f"/projects/{project_id}/settings", "Only dev can be reset.")

    try:
        with db_session() as db:
            project = get_project_for_user(db, user, project_id)
            if not project:
                return HTMLResponse("Not found", status_code=404)
            locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
            if locked:
                return locked

            reset_env(project, env)
            set_env_status(project.id, env, "active", "")
            record_build_event(project.id, env, "", "", "reset", "Dev reset")
    except Exception as exc:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            f"Reset failed: {str(exc)[:200]}",
        )

    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)


@app.post("/projects/{project_id}/reset-from-prod")
def reset_project_env_from_prod(
    request: Request,
    project_id: int,
    target_env: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if target_env not in {"dev", "staging"}:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            "Only dev or staging can be reset from prod.",
        )

    try:
        with db_session() as db:
            project = get_project_for_user(db, user, project_id)
            if not project:
                return HTMLResponse("Not found", status_code=404)
            locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
            if locked:
                return locked

            envs = {e.name: e for e in project.envs}
            if "prod" not in envs:
                return redirect_with_error(
                    f"/projects/{project_id}/settings",
                    "Production environment missing. Create prod first.",
                )

            if target_env not in envs:
                prod_settings = effective_env_settings(project, envs["prod"])
                temp_env = Environment(
                    workers=prod_settings["workers"],
                    storage_gb=prod_settings["storage_gb"],
                    odoo_version=prod_settings["odoo_version"],
                )
                host, db_name = provision_env(project, target_env, temp_env)
                env_obj = Environment(
                    project_id=project.id,
                    name=target_env,
                    host=host,
                    db_name=db_name,
                    workers=prod_settings["workers"],
                    storage_gb=prod_settings["storage_gb"],
                    odoo_version=prod_settings["odoo_version"],
                    status="active",
                    last_error="",
                )
                db.add(env_obj)
                db.commit()
                db.refresh(env_obj)

            reset_from_prod(project, target_env)
            set_env_status(project.id, target_env, "active", "")
            record_build_event(project.id, target_env, "", "", "reset", f"Reset {target_env} from prod")
    except Exception as exc:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            f"Reset from prod failed: {str(exc)[:200]}",
        )

    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)


@app.post("/projects/{project_id}/envs/create")
def create_env_manual(
    request: Request,
    project_id: int,
    env: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if env not in {"dev"}:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            "Only dev can be created manually.",
        )
    try:
        ensure_prereqs()
    except RuntimeError as exc:
        return redirect_with_error(f"/projects/{project_id}/settings", str(exc))

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked

        env_obj = db.query(Environment).filter(
            Environment.project_id == project.id,
            Environment.name == env,
        ).first()
        if env_obj and env_obj.status == "provisioning":
            return RedirectResponse(f"/projects/{project_id}/settings#env-{env}", status_code=303)
        if env_obj and env_obj.status == "active":
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"{env} environment already exists.",
            )
        if not env_obj:
            db.add(
                Environment(
                    project_id=project.id,
                    name=env,
                    host=host_for(project.slug, env),
                    db_name=db_name_for(project.slug, env),
                    workers=project.workers or DEFAULT_WORKERS,
                    storage_gb=project.storage_gb or DEFAULT_STORAGE_GB,
                    odoo_version=project.odoo_version or DEFAULT_ODOO_VERSION,
                    status="provisioning",
                    last_error="",
                )
            )
        else:
            env_obj.status = "provisioning"
            env_obj.last_error = ""
        db.commit()

    spawn_env_provision(project_id, env)
    return RedirectResponse(f"/projects/{project_id}/settings#env-{env}", status_code=303)


@app.post("/projects/{project_id}/envs/restart")
def restart_env_manual(
    request: Request,
    project_id: int,
    env: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if env not in {"dev", "staging", "prod"}:
        return redirect_with_error(f"/projects/{project_id}/settings", "Invalid environment.")

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked
        env_obj = db.query(Environment).filter(
            Environment.project_id == project.id,
            Environment.name == env,
        ).first()
        if not env_obj:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"{env} environment is not created yet.",
                {"env": env, "tab": "history"},
            )
        env_obj.last_error = ""
        db.commit()

    spawn_env_restart(project_id, env)
    return redirect_with_notice(
        f"/projects/{project_id}/settings",
        f"{env} restart started.",
        {"env": env, "tab": "history"},
    )


@app.post("/projects/{project_id}/envs/redeploy")
def redeploy_env_manual(
    request: Request,
    project_id: int,
    env: str = Form(...),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    if env not in {"dev", "staging", "prod"}:
        return redirect_with_error(f"/projects/{project_id}/settings", "Invalid environment.")
    try:
        ensure_prereqs()
    except RuntimeError as exc:
        return redirect_with_error(f"/projects/{project_id}/settings", str(exc))

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked
        env_obj = db.query(Environment).filter(
            Environment.project_id == project.id,
            Environment.name == env,
        ).first()
        if not env_obj:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"{env} environment is not created yet.",
                {"env": env, "tab": "history"},
            )
        env_obj.status = "provisioning"
        env_obj.last_error = ""
        db.commit()

    spawn_env_redeploy(project_id, env)
    return redirect_with_notice(
        f"/projects/{project_id}/settings",
        f"{env} redeploy started.",
        {"env": env, "tab": "history"},
    )


@app.post("/projects/{project_id}/domains/{domain_id}/delete")
def delete_domain(request: Request, project_id: int, domain_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked
        domain = db.query(Domain).filter(Domain.id == domain_id).first()
        if not domain:
            return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)
        env = db.query(Environment).filter(Environment.id == domain.env_id).first()
        if not env or env.project_id != project.id:
            return HTMLResponse("Forbidden", status_code=403)

        ingress_name = ingress_name_for_domain(f"odoo-{project.slug}-{env.name}", domain.host)
        kubectl_delete("ingress", ingress_name)
        db.delete(domain)
        db.commit()

    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)
