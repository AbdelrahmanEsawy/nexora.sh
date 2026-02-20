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
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "nexora.red")
ODOO_NAMESPACE = os.getenv("ODOO_NAMESPACE", "odoo-system")
ODOO_IMAGE = os.getenv("ODOO_IMAGE", "odoo:17")
ODOO_ADMIN_PASSWD = os.getenv("ODOO_ADMIN_PASSWD")
ADMIN_GITHUB = os.getenv("ADMIN_GITHUB", "").lower()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")

APP_SECRET = os.getenv("APP_SECRET", "change-me")

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


Base.metadata.create_all(engine)

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
        cur.execute(sql.SQL("CREATE DATABASE {};").format(sql.Identifier(db_name)))
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


def provision_env(slug: str, env: str):
    host = host_for(slug, env)
    db_name = db_name_for(slug, env)
    name = f"odoo-{slug}-{env}"
    pvc_name = f"odoo-filestore-{slug}-{env}"
    cfg_secret = f"odoo-config-{slug}-{env}"

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
      storage: 10Gi
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
          image: {ODOO_IMAGE}
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
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
spec:
  ingressClassName: nginx
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
def create_project(request: Request, slug: str = Form(...)):
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
        project = Project(slug=slug, owner_id=user.id)
        db.add(project)
        db.commit()
        db.refresh(project)

        envs = []
        for env in ["dev", "staging", "prod"]:
            host, db_name = provision_env(slug, env)
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
