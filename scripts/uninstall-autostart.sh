#!/usr/bin/env bash
# Remove PyDistance-Service systemd unit (keeps .venv, .env, and logs).
set -euo pipefail

SERVICE_NAME="pydistance"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "This script must be run as root (e.g. sudo $0)." >&2
  exit 1
fi

echo "==> Removing PyDistance-Service autostart (${SERVICE_NAME})"

if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
  systemctl stop "${SERVICE_NAME}"
fi

if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
  systemctl disable "${SERVICE_NAME}"
fi

if [[ -f "${UNIT_PATH}" ]]; then
  rm -f "${UNIT_PATH}"
  echo "    Removed ${UNIT_PATH}"
fi

systemctl daemon-reload
systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true

echo "==> Uninstall complete (project files under install dir were not removed)"
