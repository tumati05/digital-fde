#!/usr/bin/env sh
# macOS/Linux: ./run.sh
# First run: creates .venv, installs requirements, copies .env.example to .env (then stops so you can fill it in).
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env. Set SUPERVITY_TOKEN and WORKFLOW_ID in it, then run ./run.sh again."
  exit 0
fi
set -a; . ./.env; set +a

HOST=127.0.0.1
WORKERS=1
EXTRA=
case "$(printf '%s' "${APP_ENV:-development}" | tr '[:upper:]' '[:lower:]')" in
  production) HOST=0.0.0.0; WORKERS=1 ;;
esac
case "$(printf '%s' "${TRUST_PROXY:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) EXTRA=--proxy-headers ;;
esac
exec python -m uvicorn backend.main:app --host "$HOST" --port 8000 --workers "$WORKERS" $EXTRA
