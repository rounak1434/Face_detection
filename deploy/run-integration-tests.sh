#!/usr/bin/env bash
# Runs the real-stack integration tests (backend/tests/integration) inside a
# throwaway Python 3.12 container, so the host needs only Docker (Ubuntu
# 22.04 ships Python 3.10, which is too old for the backend).
#
# Usage (from deploy/, with the stack running):  bash run-integration-tests.sh
# Optional: IT_ADMIN_USER (default admin), IT_CAMERA (default entrance),
#           IT_FACE_IMAGE=/path/photo.jpg (a photo you may use, one visible face)
#           to also check real face detection end to end.
set -euo pipefail
cd "$(dirname "$0")"

site=$(grep -E '^SITE_ADDRESS=' .env 2>/dev/null | cut -d= -f2- || true)
site=${site:-localhost}
user=${IT_ADMIN_USER:-admin}

if [ -z "${IT_ADMIN_PASSWORD:-}" ]; then
  read -rsp "Password for web admin '$user': " IT_ADMIN_PASSWORD
  echo
fi
export IT_ADMIN_PASSWORD

face_args=()
if [ -n "${IT_FACE_IMAGE:-}" ]; then
  [ -f "$IT_FACE_IMAGE" ] || { echo "IT_FACE_IMAGE not found: $IT_FACE_IMAGE" >&2; exit 1; }
  ext="${IT_FACE_IMAGE##*.}"
  face_args=(-v "$(realpath "$IT_FACE_IMAGE"):/fixtures/face.$ext:ro" -e "IT_FACE_IMAGE=/fixtures/face.$ext")
fi

# The public-domain portrait used by the inference tests (see its README).
pd_dir="$(cd .. && pwd)/inference/tests/data/public_domain"
face_args+=(-v "$pd_dir:/fixtures/pd:ro" -e "IT_PD_FACE_IMAGE=/fixtures/pd/nasa_s69-31741_480x600.jpg")

echo "Testing https://$site/ (camera: ${IT_CAMERA:-entrance})"
docker run --rm --network host \
  -v "$(cd .. && pwd)/backend:/src:ro" "${face_args[@]}" \
  -e IT_BASE_URL="https://$site" -e IT_VERIFY_TLS=0 \
  -e IT_ADMIN_USER="$user" -e IT_ADMIN_PASSWORD -e IT_CAMERA="${IT_CAMERA:-entrance}" \
  python:3.12-slim sh -c '
    cp -r /src /tmp/app && cd /tmp/app &&
    pip install --quiet --disable-pip-version-check ".[dev]" &&
    python -m pytest -p no:cacheprovider -m integration -v tests/integration'
