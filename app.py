import os
import json
import base64
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from google.oauth2 import id_token
from google.auth.transport import requests as grequests
from openai import OpenAI

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB upload limit

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not GOOGLE_CLIENT_ID:
    raise RuntimeError("Missing env var GOOGLE_CLIENT_ID")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing env var OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_bearer_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def verify_google_id_token(token: str) -> dict:
    """Validate Google ID token and return its payload (includes sub, email, name...)."""
    payload = id_token.verify_oauth2_token(
        token,
        grequests.Request(),
        GOOGLE_CLIENT_ID,
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


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/analyze-meal")
def analyze_meal():
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

    resp = client.chat.completions.create(
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


@app.post("/recommendations")
def recommendations():
    token = get_bearer_token()
    if not token:
        return jsonify({"error": "missing_bearer_token"}), 401

    try:
        g = verify_google_id_token(token)
        user_id = g["sub"]
    except Exception:
        return jsonify({"error": "invalid_google_token"}), 401

    payload = request.get_json(silent=True) or {}

    resp = client.chat.completions.create(
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), debug=True)
