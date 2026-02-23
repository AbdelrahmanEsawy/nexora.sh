import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import httpx
import psycopg2
from psycopg2 import sql
from authlib.integrations.starlette_client import OAuth
from authlib.integrations.base_client.errors import MismatchingStateError
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware
import jwt

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "nexora.red")
ODOO_NAMESPACE = os.getenv("ODOO_NAMESPACE", "odoo-system")
ODOO_IMAGE = os.getenv("ODOO_IMAGE", "odoo:19")
ODOO_ADMIN_PASSWD = os.getenv("ODOO_ADMIN_PASSWD")
ADMIN_GITHUB = os.getenv("ADMIN_GITHUB", "").lower()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")

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

HOSTING_LOCATIONS = [
    {
        "code": "ca-tor",
        "label": "Canada",
        "region": "Americas",
        "probe_url": "https://speedtest-tor1.digitalocean.com/1mb.test",
    },
    {
        "code": "us-east",
        "label": "Americas",
        "region": "Americas",
        "probe_url": "https://speedtest-nyc1.digitalocean.com/1mb.test",
    },
    {
        "code": "eu-central",
        "label": "Europe",
        "region": "Europe",
        "probe_url": "https://speedtest-fra1.digitalocean.com/1mb.test",
    },
    {
        "code": "me",
        "label": "Middle East",
        "region": "Middle East",
        "probe_url": "https://speedtest-fra1.digitalocean.com/1mb.test",
    },
    {
        "code": "in-south",
        "label": "Southern Asia",
        "region": "Asia",
        "probe_url": "https://speedtest-blr1.digitalocean.com/1mb.test",
    },
    {
        "code": "sea",
        "label": "Southeast Asia",
        "region": "Asia",
        "probe_url": "https://speedtest-sgp1.digitalocean.com/1mb.test",
    },
    {
        "code": "au-syd",
        "label": "Oceania",
        "region": "Oceania",
        "probe_url": "https://speedtest-syd1.digitalocean.com/1mb.test",
    },
]
HOSTING_LOCATION_MAP = {item["code"]: item for item in HOSTING_LOCATIONS}

DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "nexora.db")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    github_id = Column(String, unique=True, nullable=False)
    username = Column(String, unique=True, nullable=False)
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


Base.metadata.create_all(engine)


def ensure_schema():
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))}
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

        env_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(environments)"))}
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


ensure_schema()

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=1)
        conn.execute("SELECT 1")
        conn.close()
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


def normalize_hosting_location(code: str) -> str:
    code = (code or "").strip().lower()
    if code in HOSTING_LOCATION_MAP:
        return code
    return ""


def parse_ping_ms(value: str) -> Optional[int]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        ping = int(float(raw))
    except Exception:
        return None
    return max(1, min(20000, ping))


def hosting_location_label(code: str) -> str:
    item = HOSTING_LOCATION_MAP.get((code or "").strip().lower())
    if not item:
        return "Auto"
    return item["label"]


def env_logs_tail(slug: str, env_name: str, lines: int = 200) -> str:
    deploy_name = f"odoo-{slug}-{env_name}"
    result = run_kubectl(
        ["logs", f"deployment/{deploy_name}", f"--tail={max(20, min(lines, 1000))}"],
        namespace=ODOO_NAMESPACE,
        timeout=max(KUBECTL_GET_TIMEOUT, 12),
    )
    stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        if "NotFound" in stderr:
            return f"No deployment found for {env_name} yet."
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
        db.commit()


def branch_from_ref(ref: str) -> str:
    if not ref:
        return ""
    if ref.startswith("refs/heads/"):
        return ref.split("/", 2)[2]
    return ref.split("/")[-1]


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
        return db.query(User).filter(User.id == user_id).first()


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
    workers = project.workers or DEFAULT_WORKERS
    storage_gb = project.storage_gb or DEFAULT_STORAGE_GB
    odoo_version = project.odoo_version or DEFAULT_ODOO_VERSION
    if env_obj:
        if env_obj.workers:
            workers = env_obj.workers
        if env_obj.storage_gb:
            storage_gb = env_obj.storage_gb
        if env_obj.odoo_version:
            odoo_version = env_obj.odoo_version
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


def init_job_failure_reason(slug: str, env: str) -> str:
    job_name = init_job_name(slug, env)
    pods = kubectl_get_list_json(ODOO_NAMESPACE, "pods", f"job-name={job_name}")
    items = (pods or {}).get("items") or []
    if not items:
        return ""

    pod = sorted(items, key=lambda p: p.get("metadata", {}).get("creationTimestamp", ""))[-1]
    status = pod.get("status", {})
    for container in status.get("containerStatuses") or []:
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

    phase = status.get("phase")
    if phase and phase not in {"Running", "Succeeded"}:
        return str(phase)[:220]
    return ""


def init_job_status(slug: str, env: str) -> Optional[dict]:
    job = kubectl_get_json(ODOO_NAMESPACE, "job", init_job_name(slug, env))
    if not job:
        return None
    status = job.get("status", {})
    failed = int(status.get("failed") or 0)
    reason = ""
    if failed > 0:
        for cond in status.get("conditions") or []:
            if cond.get("type") == "Failed" and cond.get("status") == "True":
                reason = (cond.get("message") or cond.get("reason") or "").strip()
                break
        if not reason:
            reason = init_job_failure_reason(slug, env)
    return {
        "active": int(status.get("active") or 0),
        "succeeded": int(status.get("succeeded") or 0),
        "failed": failed,
        "reason": reason[:220],
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
        if env_obj and env_obj.status == "provisioning":
            return {"ready": False, "message": "Provisioning resources"}
        if env_obj and env_obj.status == "failed":
            if env_error:
                return {"ready": False, "message": f"Error: {env_error}"}
            return {"ready": False, "message": "Provisioning failed"}
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
            return {"ready": False, "message": "Initializing database"}

    pod_issue = odoo_pod_issue(project.slug, env_name)
    if pod_issue:
        return {"ready": False, "message": f"Pod issue: {pod_issue}"}
    return {"ready": False, "message": "Starting"}


def kubectl_delete(kind: str, name: str):
    run_kubectl(
        ["delete", kind, name, "--ignore-not-found"],
        namespace=ODOO_NAMESPACE,
        timeout=KUBECTL_MUTATE_TIMEOUT,
    )


def kubectl_wait(kind: str, name: str, timeout: str = "600s"):
    run_kubectl(
        ["wait", "--for=condition=complete", kind, name, f"--timeout={timeout}"],
        namespace=ODOO_NAMESPACE,
        timeout=max(KUBECTL_MUTATE_TIMEOUT, 90),
    )


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
        kubectl_delete("ingress", ingress_name)

    kubectl_delete("ingress", name)
    kubectl_delete("service", name)
    kubectl_delete("deployment", name)
    kubectl_delete("secret", cfg_secret)
    kubectl_delete("pvc", pvc_name)

    drop_db(db_name_for(slug, env))


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
                env_names = [env.name for env in project.envs]

            errors = []
            for env_name in env_names:
                try:
                    delete_env(slug, env_name)
                except Exception as exc:
                    errors.append(f"{env_name}: {str(exc)[:120]}")

            with db_session() as db:
                project = db.query(Project).filter(Project.id == project_id).first()
                if not project:
                    return
                if errors:
                    project.status = "delete_failed"
                    project.last_error = "; ".join(errors)[:500]
                    db.commit()
                    return
                db.delete(project)
                db.commit()
        except Exception as exc:
            set_project_status(project_id, "delete_failed", str(exc))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def spawn_orphan_cleanup(slug: str, env_domains: dict[str, list[str]]):
    def _worker():
        for env_name, hosts in env_domains.items():
            try:
                delete_env(slug, env_name, hosts)
            except Exception:
                # Best-effort cleanup for force-delete mode.
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
                host, db_name = provision_env(project, env_name, env_obj)
                env_obj.host = host
                env_obj.db_name = db_name
                env_obj.status = "active"
                env_obj.last_error = ""
                db.commit()
        except Exception as exc:
            set_env_status(project_id, env_name, "failed", str(exc))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    error = request.query_params.get("error")
    notice = request.query_params.get("notice")
    prefill_slug = request.query_params.get("slug", "")
    prefill_display = request.query_params.get("display_name", "")
    prefill_hosting_location = normalize_hosting_location(request.query_params.get("hosting_location", ""))
    prefill_hosting_ping = parse_ping_ms(request.query_params.get("hosting_ping_ms", ""))

    with db_session() as db:
        if user.is_admin:
            projects = db.query(Project).all()
        else:
            projects = db.query(Project).filter(Project.owner_id == user.id).all()
        projects.sort(key=lambda p: p.created_at, reverse=True)
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
                "hosting_locations": HOSTING_LOCATIONS,
                "hosting_location_map": HOSTING_LOCATION_MAP,
            },
        )


@app.get("/projects", response_class=HTMLResponse)
def projects_index(request: Request):
    return index(request)


@app.get("/login", response_class=HTMLResponse)
def login(request: Request):
    if current_user(request):
        return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/github/install")
def github_install(request: Request):
    return RedirectResponse(github_install_url())


@app.get("/auth/github")
async def auth_github(request: Request):
    try:
        client = oauth.github
    except AttributeError:
        return HTMLResponse("GitHub OAuth not configured", status_code=500)
    redirect_uri = request.url_for("auth_callback")
    return await client.authorize_redirect(request, redirect_uri)


@app.get("/auth/github/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.github.authorize_access_token(request)
    except MismatchingStateError:
        request.session.clear()
        return RedirectResponse("/login")
    resp = await oauth.github.get("user", token=token)
    profile = resp.json()

    username = profile.get("login")
    github_id = str(profile.get("id"))
    name = profile.get("name")
    if not username or not github_id:
        return HTMLResponse("GitHub login failed", status_code=400)

    with db_session() as db:
        user = db.query(User).filter(User.github_id == github_id).first()
        if not user:
            user = User(
                github_id=github_id,
                username=username,
                name=name,
                is_admin=username.lower() == ADMIN_GITHUB,
            )
            db.add(user)
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
                    env = None
                    if branch == (project.dev_branch or DEFAULT_DEV_BRANCH):
                        env = "dev"
                    elif branch == (project.staging_branch or DEFAULT_STAGING_BRANCH):
                        env = "staging"
                    elif branch == (project.prod_branch or DEFAULT_PROD_BRANCH):
                        env = "prod"
                    if env:
                        if env == "dev":
                            reset_env(project, env)
                            msg = f"Reset dev on push to {branch}"
                        else:
                            restart_env(project.slug, env)
                            msg = f"Restarted {env} on push to {branch}"
                        record_build_event(project.id, env, branch, sha, "deployed", msg)
        return {"ok": True}

    return {"ok": True}


@app.post("/projects")
def create_project(
    request: Request,
    slug: str = Form(...),
    display_name: str = Form(None),
    hosting_location: str = Form(""),
    hosting_ping_ms: str = Form(""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    raw_slug = (slug or "").strip().lower()
    raw_display = (display_name or "").strip()
    selected_hosting_location = normalize_hosting_location(hosting_location)
    selected_hosting_ping = parse_ping_ms(hosting_ping_ms)

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

    spawn_env_provision(created_project_id, "dev")
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

        db.delete(project)
        db.commit()

    spawn_orphan_cleanup(slug, env_domains)
    return redirect_with_notice("/", f"{project_name} removed from dashboard. Cleanup continues in background.")


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
    error = request.query_params.get("error")
    notice = request.query_params.get("notice")
    selected_env = (request.query_params.get("env") or "").strip().lower()
    selected_tab = (request.query_params.get("tab") or "history").strip().lower()
    if selected_tab not in {"history", "logs", "settings"}:
        selected_tab = "history"
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

        if user.is_admin:
            installations = db.query(Installation).order_by(Installation.installed_at.desc()).all()
        else:
            installations = (
                db.query(Installation)
                .filter(Installation.account_login == user.username)
                .order_by(Installation.installed_at.desc())
                .all()
            )
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
        branch_logs = ""
        if selected_tab == "logs":
            if selected_env_obj:
                branch_logs = env_logs_tail(project.slug, selected_env, lines=260)
            else:
                branch_logs = f"{selected_env} environment has not been created yet."

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
                "hosting_locations": HOSTING_LOCATIONS,
                "hosting_location_map": HOSTING_LOCATION_MAP,
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
        return JSONResponse(
            {
                "ready": bool(status.get("ready")),
                "message": status.get("message") or "",
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

    try:
        with db_session() as db:
            project = get_project_for_user(db, user, project_id)
            if not project:
                return HTMLResponse("Not found", status_code=404)
            locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
            if locked:
                return locked

            project.display_name = display_name.strip()[:80] or project.slug
            project.dev_branch = dev_branch.strip() or DEFAULT_DEV_BRANCH
            project.staging_branch = staging_branch.strip() or DEFAULT_STAGING_BRANCH
            project.prod_branch = prod_branch.strip() or DEFAULT_PROD_BRANCH
            project.workers = max(1, int(workers))
            project.storage_gb = max(1, int(storage_gb))
            project.staging_slots = max(1, int(staging_slots))
            project.subscription_code = subscription_code.strip()[:120]
            project.odoo_version = (odoo_version.strip() or DEFAULT_ODOO_VERSION)
            project.hosting_location = normalize_hosting_location(hosting_location) or None
            project.hosting_ping_ms = parse_ping_ms(hosting_ping_ms)
            db.commit()
            db.refresh(project)

            # Reconcile existing environments with new settings.
            for env in list(project.envs):
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
    workers: int = Form(...),
    storage_gb: int = Form(...),
    odoo_version: str = Form(DEFAULT_ODOO_VERSION),
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

            new_workers = max(1, int(workers))
            new_storage = max(1, int(storage_gb))
            current_storage = env.storage_gb or project.storage_gb or DEFAULT_STORAGE_GB
            if new_storage < current_storage:
                return redirect_with_error(
                    f"/projects/{project_id}/settings",
                    "Filestore size cannot be decreased.",
                )

            env.workers = new_workers
            env.storage_gb = new_storage
            env.odoo_version = (odoo_version.strip() or DEFAULT_ODOO_VERSION)
            db.commit()
            db.refresh(env)

            host, db_name = provision_env(project, env.name, env)
            env.host = host
            env.db_name = db_name
            env.status = "active"
            env.last_error = ""
            db.commit()
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
    if not repo_full_name or "/" not in repo_full_name:
        return HTMLResponse("Invalid repository name", status_code=400)

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        locked = ensure_project_mutable(project, f"/projects/{project_id}/settings")
        if locked:
            return locked

        if not installation_id:
            # Auto-select if exactly one installation matches this user.
            q = db.query(Installation)
            if not user.is_admin:
                q = q.filter(Installation.account_login == user.username)
            installs = q.all()
            if len(installs) == 1:
                installation_id = installs[0].installation_id

        if not installation_id:
            return HTMLResponse("Installation ID required", status_code=400)

        # Verify access via GitHub App installation token.
        try:
            token = github_installation_token(installation_id)
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            resp = httpx.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers, timeout=20)
            resp.raise_for_status()
            repo = resp.json()
        except Exception:
            return HTMLResponse("Unable to access repository with this installation", status_code=400)

        project.repo_full_name = repo_full_name
        project.repo_id = str(repo.get("id"))
        project.installation_id = str(installation_id)
        db.commit()

    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)


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

    try:
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

            if target_env not in envs:
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

            promote_env(project, source_env, target_env)
            set_env_status(project.id, target_env, "active", "")

            record_build_event(
                project.id,
                target_env,
                "",
                "",
                "promoted",
                f"Promoted {source_env} → {target_env}",
            )
    except Exception as exc:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            f"Promotion failed: {str(exc)[:200]}",
        )

    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)


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
