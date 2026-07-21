#!/usr/bin/env bash
# ===========================================================================
# One command to start the bot: ./start.sh
#
# What "one tap" honestly means here: this is a self-hosted system that runs
# on a server or your own machine via Docker — there is no mobile app and this
# script does not pretend there is one. What it actually does is collapse the
# real startup sequence (check secrets, validate config, run the safety test
# suite, bring up every service in the right order) into the one command you
# asked for. Double-click it in a file manager that runs .sh files, or run it
# from a terminal / SSH session / Termux on a phone — any of those is "one tap"
# for a system that lives on a server.
#
# What this does NOT do: flip system.mode to "live". That switch, and the
# 14-day shadow-trading record it requires, is described in RISKS.md and
# enforced in code by execution/base.py's LiveExecutorGuard — no startup
# script should be able to bypass it, including this one.
# ===========================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; exit 1; }
step() { echo -e "\n${BOLD}== $1 ==${NC}"; }

step "1/6  Docker"
command -v docker >/dev/null 2>&1 || fail "Docker not found. Install Docker first: https://docs.docker.com/get-docker/"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 not found (needs 'docker compose', not the old 'docker-compose')."
ok "Docker + Compose available"

step "2/6  Secrets (.env)"
if [ ! -f .env ]; then
  cp .env.example .env
  warn "Created .env from .env.example — it is empty. Fill in at least:"
  echo "      SOLANA_RPC_HTTP, SOLANA_RPC_WS, WALLET_PUBKEY, POSTGRES_PASSWORD, GRAFANA_PASSWORD"
  echo "      Then run ./start.sh again."
  exit 1
fi
# shellcheck disable=SC1091
set -a; source .env; set +a
missing=()
for var in POSTGRES_PASSWORD GRAFANA_PASSWORD SOLANA_RPC_HTTP SOLANA_RPC_WS; do
  [ -z "${!var:-}" ] && missing+=("$var")
done
if [ ${#missing[@]} -gt 0 ]; then
  fail ".env is missing: ${missing[*]} — fill these in before starting."
fi
ok ".env present and has the required core values"

step "3/6  Config (config/config.yaml)"
if [ ! -f config/config.yaml ]; then
  cp config/config.example.yaml config/config.yaml
  ok "Created config/config.yaml from the example (defaults are conservative — see README.md)"
else
  ok "config/config.yaml already exists, leaving it as-is"
fi

step "4/6  Wallet keystore"
if [ ! -f secrets/keystore.json ]; then
  mkdir -p secrets
  warn "No secrets/keystore.json found."
  echo "      Create one with:"
  echo "      python3 -c \"from mca.execution.wallet import WalletManager; import os"
  echo "      ks = WalletManager.create(os.urandom(32), input('Passphrase: '))"
  echo "      ks.save('secrets/keystore.json'); print('pubkey:', ks.pubkey)\""
  echo "      The bot will still start in paper mode without this — no keys are"
  echo "      needed until system.mode is switched to live, which is gated"
  echo "      separately (see RISKS.md)."
else
  ok "secrets/keystore.json present"
fi

step "5/6  Safety test suite (pre-flight)"
if command -v python3 >/dev/null 2>&1; then
  if python3 scripts/run_tests.py >/tmp/mca-test-output.log 2>&1; then
    ok "$(tail -3 /tmp/mca-test-output.log | head -1)"
  else
    fail "Safety tests failed — see /tmp/mca-test-output.log. Not starting; a bot that cannot prove its own risk logic must not trade."
  fi
else
  warn "python3 not found on this machine — skipping the local pre-flight check (the Docker build runs it again regardless, see Dockerfile)"
fi

step "6/6  Starting services"
docker compose up -d --build
echo
ok "Started. Default profile only — enrich/scorer/social ingest are not yet built (see README.md) and are excluded so nothing crash-loops; add --profile full once they exist."
echo
echo -e "  ${BOLD}Mode:${NC}        $(grep -A1 '^system:' config/config.yaml | grep mode | awk '{print $2}')"
echo -e "  ${BOLD}Dashboard:${NC}   http://localhost:3001  (Grafana)"
echo -e "  ${BOLD}API:${NC}         http://localhost:8000/health"
echo -e "  ${BOLD}Logs:${NC}        docker compose logs -f"
echo -e "  ${BOLD}Stop:${NC}        docker compose down"
echo
warn "Live trading needs 14 days of unbroken paper trading first — this is enforced in code (execution/base.py), not just documented. See RISKS.md before doing anything else."
