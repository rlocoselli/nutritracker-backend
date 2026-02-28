# Nutrition MVP API (Flask)

## Landing page
- `GET /` multi-language landing page: English, Français, Português, Italiano, Español
- Includes GDPR/RGPD sections: privacy policy, cookies policy, user rights, terms of use
- API docs links available from home page
- Swagger UI: `GET /api/docs`
- OpenAPI JSON: `GET /api/openapi.json`
- Dedicated legal pages:
  - `GET /privacy`
  - `GET /cookies`
  - `GET /terms`

## Endpoints
- `GET /api/health`
- `GET /api/health/db` (checks PostgreSQL connectivity)
- `POST /api/analyze-meal`
  - JSON: `{ "lang": "pt", "text": "comi 2 ovos" }`
  - or multipart form-data: fields `lang`, `text` and file `image`
  - Header: `Authorization: Bearer <google_id_token>`
- `POST /api/recommendations`
  - JSON: aggregated history + goals
  - Header: `Authorization: Bearer <google_id_token>`

## Environment variables
- `OPENAI_API_KEY` (required)
- `GOOGLE_CLIENT_ID` (required) Web OAuth client id
- `OPENAI_MODEL` (optional, default: `gpt-4.1-mini`)
- `DB_HOST` (required for DB connection)
- `DB_USER` (required for DB connection)
- `DB_PASSWORD` (required for DB connection)
- `DB_NAME` (required for DB connection)
- `DB_PORT` (optional, default: `5432`)
- `PORT` (optional, default: 8086)

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY="..."
export GOOGLE_CLIENT_ID="..."
python app.py
```

## Docker deploy
- Public port: `8086`
- Health check URL: `http://localhost:8086/api/health`

## Database (Code First)
- Models are defined in code and tables are auto-created at startup.
- If `DB_NAME` does not exist, the app attempts to create it automatically at startup.
- Required DB variables: `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_PORT`.
- DB health endpoint: `GET /api/health/db`.

## Deploy on Render
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app`
- Add env vars in Render dashboard.
