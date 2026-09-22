# Deploying NEURAVEX

## Local development (this laptop, no Docker)

1. `cp .env.example .env` and fill in `NEURAVEX_SECRET_KEY` (generate with
   `python -c "import secrets; print(secrets.token_hex(32))"`) and
   `POSTGRES_PASSWORD`.
2. Install PostgreSQL for Windows, create a `neuravex` user/database.
3. Install Memurai (`winget install Memurai.MemuraiDeveloper`) for Redis
   on Windows.
4. `scripts\start.bat` — starts the backend.
5. In two more terminals:
   ```
   celery -A backend.app.core.celery_app worker --loglevel=info --pool=solo
   celery -A backend.app.core.celery_app beat --loglevel=info
   ```
6. Find your laptop's local IP (`ipconfig`) and point the Android app's
   Backend URL at `http://<that-ip>:8000`.

## Production (VPS + domain, so the app works without your laptop)

1. Buy a domain, rent a small VPS (Hetzner/DigitalOcean), point two "A"
   DNS records (`api.yourdomain.com` and `yourdomain.com`) at its IP.
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Copy the project to the server, edit `docker/caddy/Caddyfile` to use
   your real domain, fill in `.env` (including `NEXT_PUBLIC_API_URL` and
   `CORS_ALLOWED_ORIGIN`).
4. `docker compose up --build -d`
5. `docker compose exec backend python -c "from backend.app.db.session import init_db; init_db()"`
6. Register an account via `POST /api/auth/register`, point the Android
   app at `https://api.yourdomain.com`.

`NEURAVEX_ALLOW_LIVE` should stay `false` until you've completed a full
backtest -> paper -> validation cycle — this doesn't change just because
the server is public.
