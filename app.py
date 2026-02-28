import os
import json
import base64
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template
from google.oauth2 import id_token
from google.auth.transport import requests as grequests
from openai import OpenAI

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB upload limit

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

client = None


def get_missing_env_vars() -> list[str]:
    missing = []
    if not os.environ.get("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    if not os.environ.get("GOOGLE_CLIENT_ID"):
        missing.append("GOOGLE_CLIENT_ID")
    return missing


def get_openai_client() -> OpenAI | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    global client
    if client is None:
        client = OpenAI(api_key=api_key)
    return client

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


def safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None


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
    return render_template("index.html")


@app.get("/privacy")
def privacy_page():
    return render_template("legal.html", page_type="privacy")


@app.get("/cookies")
def cookies_page():
    return render_template("legal.html", page_type="cookies")


@app.get("/terms")
def terms_page():
    return render_template("legal.html", page_type="terms")


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
            {"name": "Nutrition"},
        ],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "Google ID token in Authorization header: Bearer <token>",
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


@app.post("/api/analyze-meal")
def analyze_meal():
    missing_env_vars = get_missing_env_vars()
    if missing_env_vars:
        return jsonify({"error": "server_not_configured", "missing_env_vars": missing_env_vars}), 503

    openai_client = get_openai_client()
    if openai_client is None:
        return jsonify({"error": "server_not_configured", "missing_env_vars": ["OPENAI_API_KEY"]}), 503

    token = get_bearer_token()
    if not token:
        return jsonify({"error": "missing_bearer_token"}), 401

    try:
        g = verify_google_id_token(token)
        user_id = g["sub"]
    except Exception:
        return jsonify({"error": "invalid_google_token"}), 401

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

    resp = openai_client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_ANALYZE},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
    )

    raw = resp.choices[0].message.content or ""
    parsed = safe_json_loads(raw)
    if parsed is None:
        return jsonify({"error": "model_returned_invalid_json", "raw": raw}), 502

    parsed.setdefault("schema_version", "1.0")
    parsed["user_id"] = user_id
    parsed["datetime_utc"] = utc_now_iso()
    return jsonify(parsed)


@app.post("/api/recommendations")
def recommendations():
    missing_env_vars = get_missing_env_vars()
    if missing_env_vars:
        return jsonify({"error": "server_not_configured", "missing_env_vars": missing_env_vars}), 503

    openai_client = get_openai_client()
    if openai_client is None:
        return jsonify({"error": "server_not_configured", "missing_env_vars": ["OPENAI_API_KEY"]}), 503

    token = get_bearer_token()
    if not token:
        return jsonify({"error": "missing_bearer_token"}), 401

    try:
        g = verify_google_id_token(token)
        user_id = g["sub"]
    except Exception:
        return jsonify({"error": "invalid_google_token"}), 401

    payload = request.get_json(silent=True) or {}

    resp = openai_client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_RECO},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.4,
    )

    raw = resp.choices[0].message.content or ""
    parsed = safe_json_loads(raw)
    if parsed is None:
        return jsonify({"error": "model_returned_invalid_json", "raw": raw}), 502

    parsed.setdefault("schema_version", "1.0")
    parsed["user_id"] = user_id
    parsed["datetime_utc"] = utc_now_iso()
    return jsonify(parsed)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8086")), debug=True)
