#!/usr/bin/env bash
# Read-only health check for the AI Surveillance stack. Changes nothing.
# Usage (from the deploy/ directory):  ./doctor.sh
set -u

cd "$(dirname "$0")"
pass() { printf '  [ ok ] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAILED=1; }
info() { printf '         %s\n' "$1"; }
FAILED=0

echo "Host"
command -v docker >/dev/null && pass "docker: $(docker --version)" || fail "docker not installed (docs/SETUP.md section 1)"
docker compose version >/dev/null 2>&1 && pass "compose: $(docker compose version --short)" || fail "docker compose plugin missing"
docker info >/dev/null 2>&1 && pass "docker daemon reachable" || fail "cannot talk to the docker daemon (is your user in the docker group? try: sudo ./doctor.sh)"
[ -f .env ] && pass ".env present" || fail ".env missing: cp .env.example .env && chmod 600 .env"
if [ -f .env ] && grep -qE '^COMPOSE_PROFILES=.*test-stream' .env; then
  info "test-stream profile enabled (synthetic RTSP source)"
fi
if [ -f .env ]; then
  perms=$(stat -c '%a' .env)
  [ "$perms" = "600" ] && pass ".env permissions 600" || info ".env permissions are $perms; recommended: chmod 600 .env"
fi

echo "Containers"
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' 2>/dev/null || fail "docker compose ps failed (run from deploy/)"
for svc in frigate api web; do
  state=$(docker compose ps --format '{{.State}}' "$svc" 2>/dev/null)
  [ "$state" = "running" ] && pass "$svc running" || fail "$svc is '${state:-not created}': docker compose logs --tail 80 $svc"
done

echo "Engine (Frigate) as seen from the api container"
if docker compose exec -T api python -c "import urllib.request,sys; print(urllib.request.urlopen('http://frigate:5000/api/version', timeout=5).read().decode())" 2>/dev/null; then
  pass "api can reach frigate:5000"
else
  fail "api cannot reach frigate:5000"
  info "check: docker compose logs --tail 80 frigate   (config errors are printed at startup)"
fi
if docker compose exec -T api python -c "import urllib.request,json; s=json.load(urllib.request.urlopen('http://frigate:5000/api/stats', timeout=5)); [print('         camera', k, 'camera_fps=', v.get('camera_fps')) for k,v in s.get('cameras',{}).items()]" 2>/dev/null; then
  info "camera_fps 0 means Frigate receives no frames: check the test-stream container or the RTSP URL (SETUP.md section 9)"
fi

echo "Web entry point"
site=$(grep -E '^SITE_ADDRESS=' .env 2>/dev/null | cut -d= -f2-)
site=${site:-localhost}
code=$(curl -sk -o /dev/null -w '%{http_code}' --resolve "$site:443:127.0.0.1" "https://$site/api/health" || true)
[ "$code" = "200" ] && pass "https://$site/api/health answers 200" || fail "https://$site/api/health answered '${code}': docker compose logs --tail 80 web api"

echo "Recent errors (last 200 lines per service)"
for svc in frigate api web test-stream; do
  n=$(docker compose logs --tail 200 "$svc" 2>/dev/null | grep -ciE 'error|fatal|exception' || true)
  [ "${n:-0}" -gt 0 ] && info "$svc: $n error lines -> docker compose logs --tail 200 $svc"
done

exit $FAILED
