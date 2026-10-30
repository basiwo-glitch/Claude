#!/usr/bin/env bash
# One-command setup on a fresh Ubuntu/Debian server (e.g. Hetzner CX22, ~4 €/Monat).
#   curl/scp this repository to the server, then:  sudo bash deploy/setup_vps.sh
# Installs Docker, writes .env with random passwords, starts the stack with HTTPS
# (Let's Encrypt via Caddy; without own domain <ip>.sslip.io is used) and runs the
# controlled integration self-test.
set -euo pipefail
cd "$(dirname "$0")/.."

# small servers (1 vCPU / little RAM): add 2 GB swap so builds and tests do not run out of memory
if [ "$(swapon --show | wc -l)" -eq 0 ]; then
  echo ">> 2 GB Swap anlegen"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if ! command -v docker >/dev/null 2>&1; then
  echo ">> Docker installieren"
  curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
  echo ">> .env erzeugen"
  cp .env.example .env
  DASH_PW=$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)
  PG_PW=$(openssl rand -hex 16)
  IP=$(curl -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
  DOMAIN_DEFAULT="$(echo "$IP" | tr '.' '-').sslip.io"
  DOMAIN="${DOMAIN:-$DOMAIN_DEFAULT}"
  sed -i "s|^DASHBOARD_PASSWORD=.*|DASHBOARD_PASSWORD=${DASH_PW}|" .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG_PW}|" .env
  sed -i "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" .env
  chmod 600 .env
fi

echo ">> Firewall (falls ufw vorhanden): 22, 80, 443"
if command -v ufw >/dev/null 2>&1; then ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp; yes | ufw enable || true; fi

echo ">> Stack bauen und starten"
docker compose up -d --build

echo ">> Warten bis die API antwortet"
for i in $(seq 1 60); do
  if docker compose exec -T api curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then break; fi
  sleep 2
done

echo ">> Echte Marktdaten prüfen"
docker compose exec -T api python -m app.datacheck || echo "!! Marktdaten-Check fehlgeschlagen"
echo ">> Unit- und Integrationstests im Container"
docker compose exec -T api python -m pytest -q -p no:cacheprovider tests || echo "!! Tests fehlgeschlagen – siehe Ausgabe"
echo ">> Kontrollierter Selbsttest (künstlicher Datensatz, Konto SELFTEST)"
docker compose exec -T api python -m app.selftest | tail -5 || true

set -a; . ./.env; set +a
echo
echo "============================================================"
echo " Dashboard:  https://${DOMAIN}"
echo " Benutzer:   admin"
echo " Passwort:   ${DASHBOARD_PASSWORD}"
echo " Logs:       docker compose logs -f worker"
echo "============================================================"
