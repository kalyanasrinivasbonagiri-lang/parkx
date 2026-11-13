# Deployment preparation

This project can be deployed as two services: a FastAPI API and a static Vite frontend. Choose a host that supports Python web services, static sites, HTTPS, and WebSockets. This document is provider-neutral; enter the commands and environment variables in the host's service settings.

## Backend service

- Service root: `backend`
- Install command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

Set these environment variables in the host dashboard. Do not commit real values:

```text
MONGODB_URI=<Atlas connection string>
MONGODB_DATABASE=smart_parking
JWT_SECRET_KEY=<long random secret>
CORS_ORIGINS=https://<your-frontend-domain>
```

Generate a secret locally with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. The Atlas cluster must support transactions and change streams. Allow the API host to reach Atlas, and give the database user access only to the application database.

## Frontend static service

- Service root: `frontend`
- Install/build command: `npm ci && npm run build`
- Publish directory: `dist`
- Build environment variable: `VITE_API_BASE_URL=https://<your-backend-domain>`
- Configure the host to serve `/index.html` for client-side routes.

The backend's `CORS_ORIGINS` must contain the exact HTTPS frontend origin, without a trailing slash. The API host must support WebSocket connections at `/ws/live`. The QR camera requires HTTPS in production; staff can still enter a booking code when camera scanning is unavailable.

## Before publishing

1. Rotate any MongoDB password that has been shared in chat, screenshots, terminals, or commits. Put the replacement only in the host's secret environment settings.
2. Confirm `.env` is ignored by Git and no real credentials are present in tracked files.
3. Deploy the backend, verify `/health`, then deploy the frontend with its backend URL and matching CORS origin.
4. Sign in with demo accounts only if the database contains seed data. Seeded passwords are public project demo credentials; do not use those accounts for real users.
5. Test driver booking/payment/QR, staff entry/exit, admin reports, and live updates over the deployed HTTPS site.

The repository does not select a hosting provider or contain provider-specific deployment automation yet. Add that once the hosting platform and service domains are known.
