import os
import json
import base64
from datetime import date, datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify, render_template, send_from_directory, session
from google.oauth2 import id_token
from google.auth.transport import requests as grequests
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship, sessionmaker

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB upload limit
app.config["SECRET_KEY"] = os.environ.get("WEB_SESSION_SECRET")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") != "development"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
MOBILE_API_BASE_URL = os.environ.get(
    "MOBILE_API_BASE_URL", "https://api.nutritiontracker.fr/api"
).rstrip("/")

client = None
db_engine = None
DbSessionLocal = None
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    meal_analyses: Mapped[list["MealAnalysis"]] = relationship(back_populates="user")
    recommendations: Mapped[list["RecommendationRecord"]] = relationship(back_populates="user")


class MealAnalysis(Base):
    __tablename__ = "meal_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="pt")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="meal_analyses")


class RecommendationRecord(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="recommendations")


def get_missing_env_vars() -> list[str]:
    missing = []
    if not os.environ.get("MISTRAL_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        # Keep the public API's historical configuration error stable. Mistral is
        # an internal provider option; API clients should not need to know about it.
        missing.append("OPENAI_API_KEY")
    if not os.environ.get("GOOGLE_CLIENT_ID"):
        missing.append("GOOGLE_CLIENT_ID")
    return missing


def get_missing_db_env_vars() -> list[str]:
    required_vars = ["DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "DB_PORT"]
    return [name for name in required_vars if not os.environ.get(name)]


def get_database_url() -> str | None:
    missing_db_env_vars = get_missing_db_env_vars()
    if missing_db_env_vars:
        return None

    return (
        f"postgresql+psycopg2://{os.environ.get('DB_USER')}:{os.environ.get('DB_PASSWORD')}"
        f"@{os.environ.get('DB_HOST')}:{os.environ.get('DB_PORT', '5432')}/{os.environ.get('DB_NAME')}"
    )


def ensure_database_exists() -> tuple[bool, str | None]:
    missing_db_env_vars = get_missing_db_env_vars()
    if missing_db_env_vars:
        return False, "database_not_configured"

    try:
        import psycopg2
        from psycopg2 import sql
    except Exception:
        return False, "psycopg2_not_installed"

    db_name = os.environ.get("DB_NAME")
    maintenance_db = os.environ.get("DB_MAINTENANCE_DB", "postgres")

    try:
        connection = psycopg2.connect(
            host=os.environ.get("DB_HOST"),
            user=os.environ.get("DB_USER"),
            password=os.environ.get("DB_PASSWORD"),
            dbname=maintenance_db,
            port=int(os.environ.get("DB_PORT", "5432")),
            connect_timeout=5,
        )
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            exists = cursor.fetchone() is not None
            if not exists:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
        connection.close()
        return True, None
    except Exception as error:
        return False, str(error)


def initialize_database() -> tuple[bool, str | None]:
    database_url = get_database_url()
    if not database_url:
        return False, "database_not_configured"

    database_exists, ensure_error = ensure_database_exists()
    if not database_exists:
        return False, ensure_error

    global db_engine, DbSessionLocal
    try:
        db_engine = create_engine(database_url, pool_pre_ping=True)
        DbSessionLocal = sessionmaker(bind=db_engine)
        Base.metadata.create_all(bind=db_engine)
        return True, None
    except Exception as error:
        db_engine = None
        DbSessionLocal = None
        return False, str(error)


def check_database_connection() -> tuple[bool, str | None]:
    if db_engine is None:
        return False, "database_not_initialized"

    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, None
    except Exception as error:
        return False, str(error)


def get_or_create_user(session, google_sub: str) -> User:
    existing_user = session.query(User).filter(User.google_sub == google_sub).first()
    if existing_user:
        return existing_user

    new_user = User(google_sub=google_sub)
    session.add(new_user)
    session.flush()
    return new_user


def save_meal_analysis(google_sub: str, language: str, payload: dict) -> None:
    if DbSessionLocal is None:
        return

    with DbSessionLocal() as session:
        user = get_or_create_user(session, google_sub)
        record = MealAnalysis(user_id=user.id, language=language, payload=payload)
        session.add(record)
        session.commit()


def save_recommendation(google_sub: str, payload: dict) -> None:
    if DbSessionLocal is None:
        return

    with DbSessionLocal() as session:
        user = get_or_create_user(session, google_sub)
        record = RecommendationRecord(user_id=user.id, payload=payload)
        session.add(record)
        session.commit()


def get_ai_provider() -> dict | None:
    if os.environ.get("MISTRAL_API_KEY"):
        return {
            "provider": "mistral",
            "api_key": os.environ.get("MISTRAL_API_KEY"),
            "model": os.environ.get("MISTRAL_MODEL", "mistral-small-latest"),
            "base_url": "https://api.mistral.ai/v1/chat/completions",
        }

    if os.environ.get("OPENAI_API_KEY"):
        return {
            "provider": "openai",
            "api_key": os.environ.get("OPENAI_API_KEY"),
            "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
            "base_url": "https://api.openai.com/v1/chat/completions",
        }

    return None


def call_ai_provider(provider: dict, messages: list[dict], temperature: float) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider['api_key']}",
    }
    payload = {
        "model": provider["model"],
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }

    response = requests.post(provider["base_url"], headers=headers, json=payload, timeout=90)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"] or ""


def invoke_ai(messages: list[dict], temperature: float) -> str:
    provider = get_ai_provider()
    if provider is None:
        raise RuntimeError("no_ai_provider_configured")

    if provider["provider"] == "mistral":
        try:
            return call_ai_provider(provider, messages, temperature)
        except Exception:
            fallback_provider = None
            if os.environ.get("OPENAI_API_KEY"):
                fallback_provider = {
                    "provider": "openai",
                    "api_key": os.environ.get("OPENAI_API_KEY"),
                    "model": os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
                    "base_url": "https://api.openai.com/v1/chat/completions",
                }
            if fallback_provider is None:
                raise
            return call_ai_provider(fallback_provider, messages, temperature)

    return call_ai_provider(provider, messages, temperature)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_bearer_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def verify_google_id_token(token: str) -> dict:
    """Validate Google ID token and return its payload (includes sub, email, name...)."""
    google_client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not google_client_id:
        raise RuntimeError("missing_google_client_id")

    payload = id_token.verify_oauth2_token(
        token,
        grequests.Request(),
        google_client_id,
    )
    return payload


def verify_access_token(token: str) -> dict:
    """Accept a Google ID token or a token issued by the companion mobile app.

    Mobile tokens are verified server-to-server by the URL configured in
    MOBILE_AUTH_VERIFY_URL. The verifier must return JSON containing an active
    user identifier in ``sub`` (or ``user_id``).
    """
    try:
        payload = verify_google_id_token(token)
        return {**payload, "auth_provider": "google"}
    except Exception as google_error:
        verify_url = os.environ.get("MOBILE_AUTH_VERIFY_URL")
        if not verify_url:
            raise google_error

    response = requests.post(
        verify_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    subject = payload.get("sub") or payload.get("user_id")
    if payload.get("active", True) is not True or not subject:
        raise ValueError("inactive_or_invalid_mobile_token")
    return {
        **payload,
        "sub": str(subject),
        "auth_provider": payload.get("auth_provider", "mobile"),
    }


def authenticate_request() -> tuple[dict | None, tuple | None]:
    token = get_bearer_token()
    if not token:
        return None, (jsonify({"error": "missing_bearer_token"}), 401)
    try:
        return verify_access_token(token), None
    except Exception:
        return None, (jsonify({"error": "invalid_access_token"}), 401)


def safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None


def mobile_api_request(method: str, path: str, **kwargs):
    return requests.request(
        method,
        f"{MOBILE_API_BASE_URL}/{path.lstrip('/')}",
        timeout=15,
        **kwargs,
    )


def browser_session_available() -> bool:
    return bool(app.config.get("SECRET_KEY"))


def establish_mobile_session(identity: dict, auth_method: str) -> dict:
    user_id = str(identity.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("mobile_identity_missing_user_id")
    session.clear()
    session.permanent = True
    session["mobile_user_id"] = user_id
    session["mobile_profile"] = {
        "id": user_id,
        "email": identity.get("email"),
        "name": identity.get("name") or identity.get("display_name"),
        "picture": identity.get("picture") or identity.get("picture_url"),
        "auth_provider": auth_method,
    }
    return session["mobile_profile"]


def require_mobile_session() -> tuple[str | None, tuple | None]:
    user_id = session.get("mobile_user_id") if browser_session_available() else None
    if not user_id:
        return None, (jsonify({"error": "account_session_required"}), 401)
    return str(user_id), None


initialize_database()


SYSTEM_PROMPT_ANALYZE = """
Você é um analisador nutricional.
Responda APENAS em JSON válido (sem markdown, sem texto fora do JSON).
Objetivo: estimar calorias, carboidratos (carbs_g) e proteínas (protein_g).
Se faltar informação, estime por porções médias e reduza confidence.
Não faça aconselhamento médico.

Schema obrigatório (JSON):
{
  "schema_version": "1.0",
  "meal": {
    "language": "<lang>",
    "items": [
      {
        "name": "string",
        "quantity": number,
        "unit": "string",
        "estimated_grams": number,
        "macros": { "calories": number, "carbs_g": number, "protein_g": number },
        "confidence": number
      }
    ],
    "totals": { "calories": number, "carbs_g": number, "protein_g": number },
    "notes": "string",
    "overall_confidence": number
  }
}
""".strip()


def build_user_prompt(text: str, lang: str) -> str:
    return f"""
Idioma de saída: {lang}
Descrição do usuário: {text}

Regras:
- Use itens separados (items[]) quando houver múltiplos alimentos.
- Preencha totals somando items.
- confidence e overall_confidence devem ser de 0 a 1.
- Se houver bebida zero/sem calorias, estime adequadamente.
""".strip()


SYSTEM_PROMPT_RECO = """
Você é um coach nutricional (não médico).
Responda APENAS em JSON válido. Sem diagnóstico. Sem alarmismo.
Considere que dados são estimativas.

Schema obrigatório:
{
  "schema_version": "1.0",
  "recommendations": [
    {
      "title": "string",
      "why": "string",
      "actions": ["string", "string"]
    }
  ],
  "insights": {
    "avg_calories": number,
    "avg_carbs_g": number,
    "avg_protein_g": number
  },
  "warnings": ["string"]
}
""".strip()


@app.get("/")
def index():
    return render_template("index.html", google_client_id=os.environ.get("GOOGLE_CLIENT_ID"))


@app.post("/api/account/google")
def browser_google_login():
    if not browser_session_available():
        return jsonify({"error": "browser_session_not_configured"}), 503
    token = str((request.get_json(silent=True) or {}).get("id_token") or "").strip()
    if not token:
        return jsonify({"error": "missing_google_id_token"}), 400
    try:
        google_profile = verify_google_id_token(token)
        response = mobile_api_request("POST", "/auth/google", json={"id_token": token})
        response.raise_for_status()
        mobile_identity = response.json()
        mobile_identity.update({
            "name": google_profile.get("name"),
            "picture": google_profile.get("picture"),
            "email": google_profile.get("email") or mobile_identity.get("email"),
        })
        profile = establish_mobile_session(mobile_identity, "google")
        return jsonify({"user": profile})
    except Exception:
        return jsonify({"error": "google_account_connection_failed"}), 401


@app.post("/api/account/email/login")
def browser_email_login():
    if not browser_session_available():
        return jsonify({"error": "browser_session_not_configured"}), 503
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    if not email or not password:
        return jsonify({"error": "email_and_password_required"}), 400
    try:
        response = mobile_api_request(
            "POST", "/auth/email/login", json={"email": email, "password": password}
        )
        if response.status_code in (401, 403, 404):
            return jsonify({"error": "invalid_email_credentials"}), 401
        response.raise_for_status()
        profile = establish_mobile_session(response.json(), "email")
        return jsonify({"user": profile})
    except Exception:
        return jsonify({"error": "mobile_account_service_unavailable"}), 502


@app.post("/api/account/logout")
def browser_account_logout():
    if not browser_session_available():
        return jsonify({"error": "browser_session_not_configured"}), 503
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/account/me")
def browser_account_profile():
    _, error = require_mobile_session()
    if error:
        return error
    return jsonify({"user": session.get("mobile_profile") or {}})


@app.get("/api/account/history")
def browser_account_history():
    user_id, error = require_mobile_session()
    if error:
        return error
    days = min(max(request.args.get("days", default=90, type=int) or 90, 1), 365)
    today = date.today()
    try:
        response = mobile_api_request(
            "GET",
            "/meals",
            headers={"X-User-Id": user_id},
            params={
                "from": (today - timedelta(days=days)).isoformat(),
                "to": today.isoformat(),
                "includePhoto": "true",
            },
        )
        response.raise_for_status()
        meals = response.json()
        return jsonify({"meals": meals if isinstance(meals, list) else [], "count": len(meals) if isinstance(meals, list) else 0})
    except Exception:
        return jsonify({"error": "mobile_history_unavailable"}), 502


@app.get("/app-ads.txt")
def app_ads_txt():
    return send_from_directory(app.root_path, "app-ads.txt", mimetype="text/plain")


@app.get("/privacy")
def privacy_page():
    return render_template("legal.html", page_type="privacy")


@app.get("/rgpd")
def rgpd_page():
    return render_template("legal.html", page_type="gdpr")


@app.get("/cookies")
def cookies_page():
    return render_template("legal.html", page_type="cookies")


@app.get("/terms")
def terms_page():
    return render_template("legal.html", page_type="terms")


@app.get("/impact")
def impact_page():
    return render_template("legal.html", page_type="impact")


@app.get("/api/openapi.json")
def openapi_spec():
    return jsonify({
        "openapi": "3.0.3",
        "info": {
            "title": "NutriTracker API",
            "version": "1.0.0",
            "description": "Nutrition analysis and recommendation API",
        },
        "servers": [{"url": "/", "description": "Current server"}],
        "tags": [
            {"name": "System"},
            {"name": "Account"},
            {"name": "Nutrition"},
        ],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "Google ID token or registered-mobile access token: Bearer <token>",
                }
            }
        },
        "paths": {
            "/api/health": {
                "get": {
                    "tags": ["System"],
                    "summary": "Health check",
                    "responses": {
                        "200": {
                            "description": "Service health",
                            "content": {
                                "application/json": {
                                    "example": {"ok": True}
                                }
                            },
                        }
                    },
                }
            },
            "/api/health/db": {
                "get": {
                    "tags": ["System"],
                    "summary": "Database connectivity health check",
                    "responses": {
                        "200": {
                            "description": "Database reachable",
                            "content": {
                                "application/json": {
                                    "example": {"ok": True, "database": "connected"}
                                }
                            },
                        },
                        "503": {
                            "description": "Database not configured or not reachable",
                        },
                    },
                }
            },
            "/api/me": {
                "get": {
                    "tags": ["Account"],
                    "summary": "Get the verified current account",
                    "security": [{"bearerAuth": []}],
                    "responses": {
                        "200": {"description": "Minimal verified profile"},
                        "401": {"description": "Missing or invalid access token"},
                    },
                }
            },
            "/api/account/email/login": {
                "post": {
                    "tags": ["Account"],
                    "summary": "Sign in with the same email account as the mobile app",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"example": {"email": "user@example.com", "password": "••••••••"}}},
                    },
                    "responses": {
                        "200": {"description": "Secure browser session established"},
                        "401": {"description": "Invalid mobile account credentials"},
                    },
                }
            },
            "/api/account/history": {
                "get": {
                    "tags": ["Account"],
                    "summary": "Retrieve meals from the connected mobile account",
                    "responses": {
                        "200": {"description": "Mobile meal history"},
                        "401": {"description": "Browser account session required"},
                    },
                }
            },
            "/api/me/history": {
                "get": {
                    "tags": ["Account"],
                    "summary": "Retrieve saved meals and recommendations",
                    "security": [{"bearerAuth": []}],
                    "parameters": [{
                        "name": "limit", "in": "query", "required": False,
                        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    }],
                    "responses": {
                        "200": {"description": "Account history, newest first"},
                        "401": {"description": "Missing or invalid access token"},
                        "503": {"description": "Database not configured"},
                    },
                }
            },
            "/api/analyze-meal": {
                "post": {
                    "tags": ["Nutrition"],
                    "summary": "Analyze meal from text or image",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "example": {
                                    "lang": "fr",
                                    "text": "2 oeufs + salade verte + 1 pomme"
                                },
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "lang": {"type": "string", "example": "fr"},
                                        "text": {"type": "string", "example": "2 oeufs + salade"},
                                    }
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Analysis result",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "schema_version": "1.0",
                                        "meal": {
                                            "language": "fr",
                                            "items": [
                                                {
                                                    "name": "oeufs",
                                                    "quantity": 2,
                                                    "unit": "unit",
                                                    "estimated_grams": 100,
                                                    "macros": {
                                                        "calories": 156,
                                                        "carbs_g": 1.1,
                                                        "protein_g": 13.0
                                                    },
                                                    "confidence": 0.86
                                                }
                                            ],
                                            "totals": {
                                                "calories": 251,
                                                "carbs_g": 18.5,
                                                "protein_g": 14.2
                                            },
                                            "notes": "Estimation automatique",
                                            "overall_confidence": 0.8
                                        },
                                        "user_id": "google_sub",
                                        "datetime_utc": "2026-02-28T12:00:00Z"
                                    }
                                }
                            }
                        },
                        "400": {"description": "Bad request"},
                        "401": {"description": "Missing or invalid bearer token"},
                        "503": {"description": "Server not configured"},
                    },
                }
            },
            "/api/recommendations": {
                "post": {
                    "tags": ["Nutrition"],
                    "summary": "Generate personalized recommendations",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "example": {
                                    "history": [
                                        {"date": "2026-02-26", "calories": 2100, "carbs_g": 180, "protein_g": 105},
                                        {"date": "2026-02-27", "calories": 1950, "carbs_g": 170, "protein_g": 110}
                                    ],
                                    "goal": "weight_loss"
                                },
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": True,
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Recommendations result",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "schema_version": "1.0",
                                        "recommendations": [
                                            {
                                                "title": "Increase protein at breakfast",
                                                "why": "Helps satiety and muscle maintenance",
                                                "actions": [
                                                    "Add 1 egg or greek yogurt",
                                                    "Target 20-30g protein at breakfast"
                                                ]
                                            }
                                        ],
                                        "insights": {
                                            "avg_calories": 2025,
                                            "avg_carbs_g": 175,
                                            "avg_protein_g": 108
                                        },
                                        "warnings": [
                                            "Estimates are not medical advice"
                                        ],
                                        "user_id": "google_sub",
                                        "datetime_utc": "2026-02-28T12:00:00Z"
                                    }
                                }
                            }
                        },
                        "401": {"description": "Missing or invalid bearer token"},
                        "503": {"description": "Server not configured"},
                    },
                }
            },
        },
    })


@app.get("/api/docs")
def api_docs_page():
    return render_template("api_docs.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/health/db")
def health_db():
    missing_db_env_vars = get_missing_db_env_vars()
    if missing_db_env_vars:
        return jsonify({"ok": False, "error": "database_not_configured", "missing_env_vars": missing_db_env_vars}), 503

    is_connected, db_error = check_database_connection()
    if not is_connected:
        return jsonify({"ok": False, "error": "database_connection_failed", "details": db_error}), 503

    return jsonify({"ok": True, "database": "connected"})


@app.get("/api/me")
def current_user():
    identity, error = authenticate_request()
    if error:
        return error
    return jsonify({
        "user": {
            "id": identity["sub"],
            "name": identity.get("name"),
            "email": identity.get("email"),
            "picture": identity.get("picture"),
            "auth_provider": identity.get("auth_provider"),
        }
    })


@app.get("/api/me/history")
def current_user_history():
    identity, error = authenticate_request()
    if error:
        return error
    if DbSessionLocal is None:
        return jsonify({"error": "database_not_configured"}), 503

    limit = min(max(request.args.get("limit", default=20, type=int) or 20, 1), 100)
    with DbSessionLocal() as session:
        user = session.query(User).filter(User.google_sub == identity["sub"]).first()
        if not user:
            return jsonify({"meals": [], "recommendations": [], "count": 0})
        meals = (
            session.query(MealAnalysis)
            .filter(MealAnalysis.user_id == user.id)
            .order_by(MealAnalysis.created_at.desc())
            .limit(limit)
            .all()
        )
        recommendations = (
            session.query(RecommendationRecord)
            .filter(RecommendationRecord.user_id == user.id)
            .order_by(RecommendationRecord.created_at.desc())
            .limit(limit)
            .all()
        )
        return jsonify({
            "meals": [
                {"id": item.id, "language": item.language, "created_at": item.created_at.isoformat(), "data": item.payload}
                for item in meals
            ],
            "recommendations": [
                {"id": item.id, "created_at": item.created_at.isoformat(), "data": item.payload}
                for item in recommendations
            ],
            "count": len(meals) + len(recommendations),
        })


@app.post("/api/analyze-meal")
def analyze_meal():
    missing_env_vars = get_missing_env_vars()
    if missing_env_vars:
        return jsonify({"error": "server_not_configured", "missing_env_vars": missing_env_vars}), 503

    identity, error = authenticate_request()
    if error:
        return error
    user_id = identity["sub"]

    # Accept JSON (text only) or multipart (text + optional image)
    if request.is_json:
        body = request.get_json(silent=True) or {}
        lang = (body.get("lang") or "pt").lower()
        text = body.get("text") or ""
        image_file = None
    else:
        lang = (request.form.get("lang") or "pt").lower()
        text = request.form.get("text") or ""
        image_file = request.files.get("image")

    if not text and not image_file:
        return jsonify({"error": "missing_text_or_image"}), 400

    user_content = [{"type": "text", "text": build_user_prompt(text, lang)}]

    if image_file:
        img_bytes = image_file.read()
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        mime = image_file.mimetype or "image/jpeg"
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{img_b64}"}
        })

    try:
        raw = invoke_ai(
            [
                {"role": "system", "content": SYSTEM_PROMPT_ANALYZE},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
        )
    except Exception:
        # Provider names and transport details are intentionally kept internal.
        return jsonify({"error": "model_request_failed"}), 502
    parsed = safe_json_loads(raw)
    if parsed is None:
        return jsonify({"error": "model_returned_invalid_json", "raw": raw}), 502

    parsed.setdefault("schema_version", "1.0")
    parsed["user_id"] = user_id
    parsed["datetime_utc"] = utc_now_iso()

    try:
        save_meal_analysis(user_id, lang, parsed)
    except SQLAlchemyError:
        pass

    return jsonify(parsed)


@app.post("/api/recommendations")
def recommendations():
    missing_env_vars = get_missing_env_vars()
    if missing_env_vars:
        return jsonify({"error": "server_not_configured", "missing_env_vars": missing_env_vars}), 503

    identity, error = authenticate_request()
    if error:
        return error
    user_id = identity["sub"]

    payload = request.get_json(silent=True) or {}

    try:
        raw = invoke_ai(
            [
                {"role": "system", "content": SYSTEM_PROMPT_RECO},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.4,
        )
    except Exception:
        # Keep the client-facing failure contract independent of the provider.
        return jsonify({"error": "model_request_failed"}), 502
    parsed = safe_json_loads(raw)
    if parsed is None:
        return jsonify({"error": "model_returned_invalid_json", "raw": raw}), 502

    parsed.setdefault("schema_version", "1.0")
    parsed["user_id"] = user_id
    parsed["datetime_utc"] = utc_now_iso()

    try:
        save_recommendation(user_id, parsed)
    except SQLAlchemyError:
        pass

    return jsonify(parsed)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8086")), debug=True)
