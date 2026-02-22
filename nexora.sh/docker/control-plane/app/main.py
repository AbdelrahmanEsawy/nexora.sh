import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
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
    repo_full_name = Column(String, nullable=True)
    repo_id = Column(String, nullable=True)
    installation_id = Column(String, nullable=True)
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
            ("repo_full_name", "repo_full_name TEXT"),
            ("repo_id", "repo_id TEXT"),
            ("installation_id", "installation_id TEXT"),
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

        env_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(environments)"))}
        for name, ddl in [
            ("workers", "workers INTEGER"),
            ("storage_gb", "storage_gb INTEGER"),
            ("odoo_version", "odoo_version TEXT"),
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


def restart_env(slug: str, env: str):
    name = f"odoo-{slug}-{env}"
    subprocess.run(
        ["kubectl", "-n", ODOO_NAMESPACE, "rollout", "restart", "deployment", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
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
    if env == "prod":
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


def init_job_status(slug: str, env: str) -> Optional[dict]:
    job = kubectl_get_json(ODOO_NAMESPACE, "job", init_job_name(slug, env))
    if not job:
        return None
    status = job.get("status", {})
    return {
        "active": int(status.get("active") or 0),
        "succeeded": int(status.get("succeeded") or 0),
        "failed": int(status.get("failed") or 0),
    }


def create_db(db_name: str):
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname="postgres",
        sslmode=DB_SSLMODE,
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        try:
            cur.execute(sql.SQL("CREATE DATABASE {};").format(sql.Identifier(db_name)))
        except psycopg2.errors.DuplicateDatabase:
            pass
    conn.close()


def drop_db(db_name: str):
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname="postgres",
        sslmode=DB_SSLMODE,
    )
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
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname="postgres",
        sslmode=DB_SSLMODE,
    )
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
    proc = subprocess.run(
        ["kubectl", "-n", ODOO_NAMESPACE, "apply", "-f", "-"],
        input=manifest.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8"))


def kubectl_get_json(namespace: str, kind: str, name: str) -> Optional[dict]:
    proc = subprocess.run(
        ["kubectl", "-n", namespace, "get", kind, name, "-o", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        return None


def env_runtime_status(project: Project, env_name: str) -> dict:
    name = f"odoo-{project.slug}-{env_name}"
    deploy = kubectl_get_json(ODOO_NAMESPACE, "deployment", name)
    if not deploy:
        return {"ready": False, "message": "Not created yet"}
    status = deploy.get("status", {})
    replicas = int(status.get("replicas") or 0)
    ready = int(status.get("readyReplicas") or 0)
    if replicas and ready >= replicas:
        job_status = init_job_status(project.slug, env_name)
        if job_status:
            if job_status.get("failed", 0) > 0:
                return {"ready": False, "message": "Init failed"}
            if job_status.get("active", 0) > 0:
                return {"ready": False, "message": "Initializing database"}
        http_code = odoo_http_status(project.slug, env_name)
        if http_code and 200 <= http_code < 400:
            return {"ready": True, "message": "Ready"}
        if http_code and http_code >= 500:
            return {"ready": False, "message": "Starting Odoo"}
        return {"ready": False, "message": "Starting"}
    pvc_name = f"odoo-filestore-{project.slug}-{env_name}"
    pvc = kubectl_get_json(ODOO_NAMESPACE, "pvc", pvc_name)
    if pvc:
        phase = pvc.get("status", {}).get("phase")
        if phase == "Pending":
            return {"ready": False, "message": "Storage pending"}
    return {"ready": False, "message": "Starting"}


def kubectl_delete(kind: str, name: str):
    subprocess.run(
        ["kubectl", "-n", ODOO_NAMESPACE, "delete", kind, name, "--ignore-not-found"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def kubectl_wait(kind: str, name: str, timeout: str = "600s"):
    subprocess.run(
        ["kubectl", "-n", ODOO_NAMESPACE, "wait", "--for=condition=complete", kind, name, f"--timeout={timeout}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
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


def ensure_odoo_init(project: Project, env: str, env_obj: Optional[Environment] = None):
    slug = project.slug
    job_name = init_job_name(slug, env)
    status = init_job_status(slug, env)
    if status:
        if status.get("succeeded", 0) > 0 or status.get("active", 0) > 0:
            return
        if status.get("failed", 0) > 0:
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
      restartPolicy: Never
      containers:
        - name: init
          image: {odoo_image}
          command:
            [
              "sh",
              "-c",
              "odoo -c /etc/odoo/odoo.conf -i base --stop-after-init --log-level=info",
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
    cert-manager.io/cluster-issuer: letsencrypt-http
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
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
    tls_secret = f"{name}-tls"
    storage_class_line = f"  storageClassName: {STORAGE_CLASS_NAME}\n" if STORAGE_CLASS_NAME else ""

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
    cert-manager.io/cluster-issuer: letsencrypt-http
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
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
    ensure_odoo_init(project, env, env_obj)
    return host, db_name


def reset_env(project: Project, env: str):
    db_name = db_name_for(project.slug, env)
    pvc_name = f"odoo-filestore-{project.slug}-{env}"
    drop_db(db_name)
    create_db(db_name)
    wipe_filestore(pvc_name)
    restart_env(project.slug, env)
    ensure_odoo_init(project, env)


def promote_env(project: Project, source_env: str, target_env: str):
    source_db = db_name_for(project.slug, source_env)
    target_db = db_name_for(project.slug, target_env)
    source_pvc = f"odoo-filestore-{project.slug}-{source_env}"
    target_pvc = f"odoo-filestore-{project.slug}-{target_env}"

    clone_db(source_db, target_db)
    copy_filestore(source_pvc, target_pvc)
    restart_env(project.slug, target_env)


def reset_from_prod(project: Project, target_env: str):
    promote_env(project, "prod", target_env)


def delete_env(slug: str, env: str):
    name = f"odoo-{slug}-{env}"
    pvc_name = f"odoo-filestore-{slug}-{env}"
    cfg_secret = f"odoo-config-{slug}-{env}"

    # Remove custom domain ingresses tied to this env.
    with db_session() as db:
        project = db.query(Project).filter(Project.slug == slug).first()
        if project:
            env_obj = db.query(Environment).filter(
                Environment.project_id == project.id,
                Environment.name == env,
            ).first()
            if env_obj:
                for domain in list(env_obj.domains):
                    ingress_name = ingress_name_for_domain(name, domain.host)
                    kubectl_delete("ingress", ingress_name)

    kubectl_delete("ingress", name)
    kubectl_delete("service", name)
    kubectl_delete("deployment", name)
    kubectl_delete("secret", cfg_secret)
    kubectl_delete("pvc", pvc_name)

    drop_db(db_name_for(slug, env))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    error = request.query_params.get("error")
    prefill_slug = request.query_params.get("slug", "")
    prefill_display = request.query_params.get("display_name", "")

    with db_session() as db:
        if user.is_admin:
            projects = db.query(Project).all()
        else:
            projects = db.query(Project).filter(Project.owner_id == user.id).all()
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
                "prefill_slug": prefill_slug,
                "prefill_display": prefill_display,
            },
        )


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
def create_project(request: Request, slug: str = Form(...), display_name: str = Form(None)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    ensure_prereqs()

    raw_slug = (slug or "").strip().lower()
    raw_display = (display_name or "").strip()
    slug = re.sub(r"[^a-z0-9-]", "-", raw_slug)
    slug = re.sub(r"-+", "-", slug).strip("-")

    if not SLUG_RE.match(slug):
        return redirect_with_error(
            "/",
            "Slug must be 2–31 chars, lowercase letters/numbers, and hyphens only.",
            {"slug": slug, "display_name": raw_display},
        )

    with db_session() as db:
        if db.query(Project).filter(Project.slug == slug).first():
            return redirect_with_error(
                "/",
                "Project already exists.",
                {"slug": slug, "display_name": raw_display},
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
            owner_id=user.id,
        )
        db.add(project)
        db.commit()
        db.refresh(project)

        # Create only dev by default (Odoo.sh-like: dev first).
        host, db_name = provision_env(project, "dev")
        db.add(
            Environment(
                project_id=project.id,
                name="dev",
                host=host,
                db_name=db_name,
                workers=project.workers or DEFAULT_WORKERS,
                storage_gb=project.storage_gb or DEFAULT_STORAGE_GB,
                odoo_version=project.odoo_version or DEFAULT_ODOO_VERSION,
            )
        )
        db.commit()

    return RedirectResponse("/", status_code=302)


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

        for env in list(project.envs):
            delete_env(project.slug, env.name)

        db.delete(project)
        db.commit()

    return RedirectResponse("/", status_code=302)


def get_project_for_user(db, user: User, project_id: int) -> Optional[Project]:
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return None
    if not user.is_admin and project.owner_id != user.id:
        return None
    return project


@app.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(request: Request, project_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")
    error = request.query_params.get("error")
    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        db.refresh(project)
        env_order = {"prod": 0, "staging": 1, "dev": 2}
        project.envs.sort(key=lambda e: env_order.get(e.name, 99))
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
        builds = (
            db.query(BuildEvent)
            .filter(BuildEvent.project_id == project.id)
            .order_by(BuildEvent.created_at.desc())
            .limit(10)
            .all()
        )
        env_status = {}
        for env in project.envs:
            if env.name == "dev":
                ensure_odoo_init(project, env.name, env)
            env_status[env.name] = env_runtime_status(project, env.name)
        return templates.TemplateResponse(
            "project_settings.html",
            {
                "request": request,
                "user": user,
                "project": project,
                "base_domain": BASE_DOMAIN,
                "installations": installations,
                "repos": repos,
                "builds": builds,
                "github_install_url": github_install_url(),
                "error": error,
                "env_status": env_status,
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
        env = db.query(Environment).filter(
            Environment.project_id == project.id, Environment.name == env_name
        ).first()
        if not env:
            return JSONResponse({"ready": False, "message": "Not created yet"}, status_code=404)

        if env_name == "dev":
            ensure_odoo_init(project, env_name, env)

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
        env = db.query(Environment).filter(
            Environment.project_id == project.id, Environment.name == env_name
        ).first()
        if not env:
            return HTMLResponse("Environment not created yet", status_code=404)

        if env_name == "dev":
            ensure_odoo_init(project, env_name, env)

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
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)

        project.display_name = display_name.strip()[:80] or project.slug
        project.dev_branch = dev_branch.strip() or DEFAULT_DEV_BRANCH
        project.staging_branch = staging_branch.strip() or DEFAULT_STAGING_BRANCH
        project.prod_branch = prod_branch.strip() or DEFAULT_PROD_BRANCH
        project.workers = max(1, int(workers))
        project.storage_gb = max(1, int(storage_gb))
        project.staging_slots = max(1, int(staging_slots))
        project.subscription_code = subscription_code.strip()[:120]
        project.odoo_version = (odoo_version.strip() or DEFAULT_ODOO_VERSION)
        db.commit()
        db.refresh(project)

        # Reconcile existing environments with new settings.
        for env in list(project.envs):
            provision_env(project, env.name, env)

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

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
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

        provision_env(project, env.name, env)

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

    allowed = {("dev", "staging"), ("staging", "prod")}
    if (source_env, target_env) not in allowed:
        return redirect_with_error(
            f"/projects/{project_id}/settings",
            "Promotion order must be dev → staging → prod.",
        )

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)

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
            )
            db.add(env_obj)
            db.commit()
            db.refresh(env_obj)
            envs[target_env] = env_obj

        promote_env(project, source_env, target_env)

        record_build_event(
            project.id,
            target_env,
            "",
            "",
            "promoted",
            f"Promoted {source_env} → {target_env}",
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

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)

        reset_env(project, env)
        record_build_event(project.id, env, "", "", "reset", "Dev reset")

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

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)

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
            )
            db.add(env_obj)
            db.commit()
            db.refresh(env_obj)

        reset_from_prod(project, target_env)
        record_build_event(project.id, target_env, "", "", "reset", f"Reset {target_env} from prod")

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

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)

        envs = {e.name: e for e in project.envs}
        if env in envs:
            return redirect_with_error(
                f"/projects/{project_id}/settings",
                f"{env} environment already exists.",
            )

        host, db_name = provision_env(project, env)
        env_obj = Environment(project_id=project.id, name=env, host=host, db_name=db_name)
        db.add(env_obj)
        db.commit()

    return RedirectResponse(f"/projects/{project_id}/settings", status_code=302)


@app.post("/projects/{project_id}/domains/{domain_id}/delete")
def delete_domain(request: Request, project_id: int, domain_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
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
