# ROCKS HOSTING BOT — Full-featured starter

## Included
- Responsive neon-blue landing page using the provided logo
- Optional Telegram channel link: https://t.me/rocks_update
- Register/login/logout with password hashing
- SQLite user and bot metadata
- Per-user bot project upload (`.py` + exact `requirements.txt`)
- Start/Stop/Restart controls
- Update Python and dependency files
- Bot logs page, delete project, health endpoint
- Docker runner mode with container resource limits for compatible Docker hosts
- Render blueprint configured in `RUNNER_MODE=demo` for safe UI testing

## Important deployment reality
Render is configured in `demo` mode because a normal Render web service does not provide a Docker daemon for launching arbitrary user containers. In this mode, dashboard actions are previews; uploaded Python is not executed.

For real bot execution, deploy the app on a Docker-capable host that you administer, with Docker installed and accessible to the app, and set `RUNNER_MODE=docker`. The included Docker runner builds one image per bot and runs it with memory/CPU/PID limits, dropped Linux capabilities, a read-only root filesystem, and no-new-privileges. It still needs security review before public multi-user use; arbitrary code execution has residual risk. Do not expose Docker access to untrusted users or deploy this configuration as-is for a public service without hardening.

## Local preview
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
Open `http://127.0.0.1:10000`. Register an account and upload a test project.

## Render preview
1. Push this folder to GitHub.
2. Render → New → Blueprint, choose the repo.
3. Deploy. The Render blueprint uses `RUNNER_MODE=demo`.
4. Use the site for layout/account/upload workflow testing only. Render ephemeral disk means uploaded files and SQLite may not persist across deploys/restarts unless persistent storage/database is configured.

## Production checklist before real users
- Use managed PostgreSQL instead of SQLite and persistent private object storage for uploads.
- Use an isolated external runner service on a Docker-capable host; keep its API private and authenticated.
- Add CSRF protection, rate limiting, email/password reset, quotas, malware scanning, audit logs, backup/restore, and robust secret storage.
- Enforce per-user CPU/RAM/disk/time/network limits. Review whether user bots should be allowed arbitrary outbound network access.
- Never store Telegram bot tokens in source files when a secrets/environment-variable UI can be provided.
