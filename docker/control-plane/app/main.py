import hashlib
import os
import re
import subprocess
from datetime import datetime
from typing import Optional

import psycopg2
from psycopg2 import sql
from authlib.integrations.starlette_client import OAuth
from authlib.integrations.base_client.errors import MismatchingStateError
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

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

DEFAULT_DEV_BRANCH = os.getenv("DEFAULT_DEV_BRANCH", "dev")
DEFAULT_STAGING_BRANCH = os.getenv("DEFAULT_STAGING_BRANCH", "staging")
DEFAULT_PROD_BRANCH = os.getenv("DEFAULT_PROD_BRANCH", "main")
DEFAULT_WORKERS = int(os.getenv("DEFAULT_WORKERS", "1"))
DEFAULT_STORAGE_GB = int(os.getenv("DEFAULT_STORAGE_GB", "10"))
DEFAULT_STAGING_SLOTS = int(os.getenv("DEFAULT_STAGING_SLOTS", "1"))
DEFAULT_ODOO_VERSION = os.getenv("DEFAULT_ODOO_VERSION", "19.0")

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


ensure_schema()

app = FastAPI()
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


def odoo_image_for(project: Project) -> str:
    version = project.odoo_version or DEFAULT_ODOO_VERSION
    if version.startswith("odoo:"):
        return version
    return f"odoo:{version}"


def ingress_name_for_domain(base: str, host: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", host.lower()).strip("-")
    if len(slug) > 40:
        slug = slug[:40]
    digest = hashlib.sha1(host.encode("utf-8")).hexdigest()[:6]
    name = f"{base}-{slug}-{digest}"
    return name[:63].rstrip("-")


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


def kubectl_apply(manifest: str):
    proc = subprocess.run(
        ["kubectl", "-n", ODOO_NAMESPACE, "apply", "-f", "-"],
        input=manifest.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8"))


def kubectl_delete(kind: str, name: str):
    subprocess.run(
        ["kubectl", "-n", ODOO_NAMESPACE, "delete", kind, name, "--ignore-not-found"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


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


def provision_env(project: Project, env: str):
    slug = project.slug
    host = host_for(slug, env)
    db_name = db_name_for(slug, env)
    name = f"odoo-{slug}-{env}"
    pvc_name = f"odoo-filestore-{slug}-{env}"
    cfg_secret = f"odoo-config-{slug}-{env}"
    storage_gb = project.storage_gb or DEFAULT_STORAGE_GB
    workers = project.workers or DEFAULT_WORKERS
    odoo_image = odoo_image_for(project)
    tls_secret = f"{name}-tls"

    create_db(db_name)

    manifest = f"""
apiVersion: v1
kind: Secret
metadata:
  name: {cfg_secret}
  namespace: {ODOO_NAMESPACE}
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
spec:
  accessModes:
    - ReadWriteOnce
  resources:
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
    return host, db_name


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

    with db_session() as db:
        if user.is_admin:
            projects = db.query(Project).all()
        else:
            projects = db.query(Project).filter(Project.owner_id == user.id).all()
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "projects": projects,
                "base_domain": BASE_DOMAIN,
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


@app.post("/projects")
def create_project(request: Request, slug: str = Form(...), display_name: str = Form(None)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login")

    ensure_prereqs()

    slug = slug.strip().lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")

    if not SLUG_RE.match(slug):
        return HTMLResponse("Invalid project slug", status_code=400)

    with db_session() as db:
        if db.query(Project).filter(Project.slug == slug).first():
            return HTMLResponse("Project already exists", status_code=400)
        project = Project(
            slug=slug,
            display_name=(display_name or slug).strip()[:80],
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

        envs = []
        for env in ["dev", "staging", "prod"]:
            host, db_name = provision_env(project, env)
            envs.append(Environment(project_id=project.id, name=env, host=host, db_name=db_name))
        db.add_all(envs)
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
    with db_session() as db:
        project = get_project_for_user(db, user, project_id)
        if not project:
            return HTMLResponse("Not found", status_code=404)
        db.refresh(project)
        return templates.TemplateResponse(
            "project_settings.html",
            {
                "request": request,
                "user": user,
                "project": project,
                "base_domain": BASE_DOMAIN,
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

        # Reconcile environments with new settings.
        for env in list(project.envs):
            provision_env(project, env.name)

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
