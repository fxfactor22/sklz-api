# SKLZ Labs API

FastAPI backend for sklzlabs.com: waitlist, accounts (Supabase Auth),
and bot telemetry for the live dashboard.

Flat layout on purpose — every file sits at the repo root so GitHub
drag-and-drop uploads keep the structure intact. No nixpacks.toml:
Railway auto-detects Python from requirements.txt and handles pip itself.

## Endpoints
- GET  /health                       — status + supabase_configured flag
- POST /api/waitlist                 — store an email
- POST /api/auth/signup|login|refresh|logout, GET /api/auth/me
- POST /api/bot/heartbeat|events|report   (bot key)  — VPS bot phones home
- GET  /api/bot/sessions[, /{id}/events]  (user JWT) — dashboard reads
- GET  /docs                         — Swagger UI

## Railway variables
SUPABASE_URL, SUPABASE_SERVICE_KEY,
ALLOWED_ORIGINS=https://www.sklzlabs.com,https://sklzlabs.com,
BOT_INGEST_KEY=<long random string; same value goes on the VPS as SKLZ_BOT_KEY>

## Database
Run schema.sql in Supabase → SQL Editor (idempotent: safe to run whole file).
