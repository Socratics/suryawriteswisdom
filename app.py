"""A small blog that imports articles from private Google Docs."""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Generator, TypeVar, cast
from urllib.parse import urlparse

import bleach
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google.oauth2.id_token import verify_oauth2_token
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


load_dotenv()

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/drive.readonly",
]
ALLOWED_TAGS = {
    "a", "blockquote", "br", "caption", "code", "dd", "div", "dl", "dt",
    "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "li", "ol",
    "p", "pre", "s", "strong", "sub", "sup", "table", "tbody", "td",
    "th", "thead", "tr", "u", "ul",
}
ALLOWED_ATTRIBUTES = {
    "a": ["href", "title"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
}
F = TypeVar("F", bound=Callable[..., Any])


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    flask_app = Flask(__name__, instance_relative_config=True)
    flask_app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32),
        DATABASE=str(Path(flask_app.instance_path) / "blog.sqlite3"),
        ADMIN_EMAIL=os.environ.get("ADMIN_EMAIL", "").strip().lower(),
        GOOGLE_CLIENT_SECRETS_FILE=os.environ.get(
            "GOOGLE_CLIENT_SECRETS_FILE",
            str(Path(flask_app.instance_path) / "client_secret.json"),
        ),
        GOOGLE_CLIENT_SECRETS_JSON=os.environ.get(
            "GOOGLE_CLIENT_SECRETS_JSON", ""
        ).strip(),
        GOOGLE_REDIRECT_URI=os.environ.get(
            "GOOGLE_REDIRECT_URI", "http://127.0.0.1:5000/oauth/callback"
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        flask_app.config.update(test_config)

    Path(flask_app.instance_path).mkdir(parents=True, exist_ok=True)
    csrf = CSRFProtect()
    csrf.init_app(flask_app)
    initialize_database(flask_app)

    @flask_app.context_processor
    def inject_current_year() -> dict[str, int]:
        return {"now_year": datetime.now(timezone.utc).year}

    @flask_app.get("/")
    def index() -> str:
        with connect_database(flask_app) as database:
            posts = database.execute(
                "SELECT slug, title, summary, published_at FROM posts "
                "ORDER BY published_at DESC"
            ).fetchall()
        return render_template("index.html", posts=posts)

    @flask_app.get("/articles/<slug>")
    def article(slug: str) -> str:
        with connect_database(flask_app) as database:
            post = database.execute(
                "SELECT title, content, published_at FROM posts WHERE slug = ?",
                (slug,),
            ).fetchone()
        if post is None:
            abort(404)
        return render_template("article.html", post=post)

    @flask_app.get("/admin")
    @admin_required
    def admin() -> str:
        with connect_database(flask_app) as database:
            posts = database.execute(
                "SELECT slug, title, published_at FROM posts ORDER BY published_at DESC"
            ).fetchall()
        return render_template("admin.html", posts=posts)

    @flask_app.get("/login")
    def login() -> Any:
        secrets_file = Path(flask_app.config["GOOGLE_CLIENT_SECRETS_FILE"])
        if (
            not flask_app.config["GOOGLE_CLIENT_SECRETS_JSON"]
            and not secrets_file.is_file()
        ):
            flash(
                "Google OAuth is not configured yet. Follow the setup guide in README.md.",
                "error",
            )
            return redirect(url_for("index"))
        if not flask_app.config["ADMIN_EMAIL"]:
            flash("Set ADMIN_EMAIL in your .env file before signing in.", "error")
            return redirect(url_for("index"))

        flow = oauth_flow(flask_app)
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            login_hint=flask_app.config["ADMIN_EMAIL"],
        )
        session["oauth_state"] = state
        return redirect(authorization_url)

    @flask_app.get("/oauth/callback")
    def oauth_callback() -> Any:
        state = session.pop("oauth_state", None)
        if not state:
            flash("The sign-in session expired. Please try again.", "error")
            return redirect(url_for("index"))

        flow = oauth_flow(flask_app, state=state)
        flow.fetch_token(authorization_response=request.url)
        token_response = flow.oauth2session.token
        id_token = token_response.get("id_token") if token_response else None
        if not isinstance(id_token, str):
            flash("Google sign-in did not return an identity token. Please try again.", "error")
            return redirect(url_for("index"))
        claims = verify_oauth2_token(
            id_token,
            GoogleAuthRequest(),
            flow.client_config["client_id"],
        )
        email = str(claims.get("email", "")).strip().lower()
        if not claims.get("email_verified") or email != flask_app.config["ADMIN_EMAIL"]:
            flash("That Google account is not authorized to manage this blog.", "error")
            return redirect(url_for("index"))

        credentials = flow.credentials
        token_path = token_file(flask_app)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        session["admin_email"] = email
        flash("Signed in. You can now import an article from Google Docs.", "success")
        return redirect(url_for("admin"))

    @flask_app.post("/admin/import")
    @admin_required
    def import_article() -> Any:
        document_id = google_document_id(request.form.get("document_url", ""))
        if document_id is None:
            flash("Enter a valid Google Docs URL.", "error")
            return redirect(url_for("admin"))

        credentials = google_credentials(flask_app)
        if credentials is None:
            session.pop("admin_email", None)
            flash("Google access has expired. Please sign in again.", "error")
            return redirect(url_for("login"))

        try:
            drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
            document = drive.files().get(
                fileId=document_id, fields="id,name,mimeType"
            ).execute()
            if document.get("mimeType") != "application/vnd.google-apps.document":
                flash("That link is not a Google Docs document.", "error")
                return redirect(url_for("admin"))
            exported = drive.files().export(
                fileId=document_id, mimeType="text/html"
            ).execute()
        except HttpError as error:
            flask_app.logger.warning("Google Docs import failed: %s", error)
            flash(
                "Google could not import that document. Check the link and confirm "
                "that the signed-in account can open it.",
                "error",
            )
            return redirect(url_for("admin"))

        exported_html = exported.decode("utf-8") if isinstance(exported, bytes) else str(exported)
        body_match = re.search(
            r"<body\b[^>]*>(.*?)</body\s*>", exported_html, flags=re.IGNORECASE | re.DOTALL
        )
        body_html = body_match.group(1) if body_match else exported_html
        safe_html = bleach.clean(
            body_html,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRIBUTES,
            protocols={"http", "https", "mailto"},
            strip=True,
        )
        title = str(document.get("name") or "Untitled article").strip()
        plain_text = re.sub(r"<[^>]*>", " ", safe_html)
        summary = " ".join(html.unescape(plain_text).split())[:240]
        published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        slug = unique_slug(flask_app, title, published_at)

        with connect_database(flask_app) as database:
            database.execute(
                "INSERT INTO posts (slug, title, summary, content, published_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (slug, title, summary, safe_html, published_at),
            )
            database.commit()

        flash(f"Published “{title}” on your blog.", "success")
        return redirect(url_for("admin"))

    @flask_app.post("/logout")
    @admin_required
    def logout() -> Any:
        session.pop("admin_email", None)
        flash("You have signed out.", "success")
        return redirect(url_for("index"))

    return flask_app


@contextmanager
def connect_database(app: Flask) -> Generator[sqlite3.Connection, None, None]:
    database = sqlite3.connect(app.config["DATABASE"])
    database.row_factory = sqlite3.Row
    try:
        with database:
            yield database
    finally:
        database.close()


def initialize_database(app: Flask) -> None:
    with connect_database(app) as database:
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                content TEXT NOT NULL,
                published_at TEXT NOT NULL
            )
            """
        )


def oauth_flow(app: Flask, state: str | None = None) -> Flow:
    client_secrets_json = app.config["GOOGLE_CLIENT_SECRETS_JSON"]
    if client_secrets_json:
        flow = Flow.from_client_config(
            json.loads(client_secrets_json),
            scopes=SCOPES,
            state=state,
        )
    else:
        flow = Flow.from_client_secrets_file(
            app.config["GOOGLE_CLIENT_SECRETS_FILE"],
            scopes=SCOPES,
            state=state,
        )
    flow.redirect_uri = app.config["GOOGLE_REDIRECT_URI"]
    return flow


def token_file(app: Flask) -> Path:
    return Path(app.instance_path) / "google_token.json"


def google_credentials(app: Flask) -> Credentials | None:
    path = token_file(app)
    if not path.is_file():
        return None

    credentials = Credentials.from_authorized_user_info(
        json.loads(path.read_text(encoding="utf-8")), SCOPES
    )
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        path.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        return None
    return credentials


def google_document_id(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname != "docs.google.com":
        return None
    match = re.match(r"^/document/d/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
    return match.group(1) if match else None


def unique_slug(app: Flask, title: str, published_at: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "article"
    date_suffix = published_at[:10]
    candidate = f"{base}-{date_suffix}"
    suffix = 2
    with connect_database(app) as database:
        while database.execute(
            "SELECT 1 FROM posts WHERE slug = ?", (candidate,)
        ).fetchone():
            candidate = f"{base}-{date_suffix}-{suffix}"
            suffix += 1
    return candidate


def admin_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not session.get("admin_email") or (
            session["admin_email"] != current_app.config["ADMIN_EMAIL"]
        ):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return cast(F, wrapped)


application = create_app()


if __name__ == "__main__":
    application.run(debug=os.environ.get("FLASK_DEBUG") == "1")
