# Nutrition MVP API (Flask)

## Endpoints
- `GET /health`
- `POST /analyze-meal`
  - JSON: `{ "lang": "pt", "text": "comi 2 ovos" }`
  - or multipart form-data: fields `lang`, `text` and file `image`
  - Header: `Authorization: Bearer <google_id_token>`
- `POST /recommendations`
  - JSON: aggregated history + goals
  - Header: `Authorization: Bearer <google_id_token>`

## Environment variables
- `OPENAI_API_KEY` (required)
- `GOOGLE_CLIENT_ID` (required) Web OAuth client id
- `OPENAI_MODEL` (optional, default: `gpt-4.1-mini`)
- `PORT` (optional, default: 8080)

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="..."
export GOOGLE_CLIENT_ID="..."
python app.py
```

## Deploy on Render
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Add env vars in Render dashboard.
