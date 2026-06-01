#!/usr/bin/env bash
# Install PyDistance-Service as a systemd unit with boot autostart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
SERVICE_NAME="pydistance"
UNIT_TEMPLATE="${SCRIPT_DIR}/pydistance.service"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "This script must be run as root (e.g. sudo $0)." >&2
  exit 1
fi

SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-${USER}}}"
if ! id "${SERVICE_USER}" &>/dev/null; then
  echo "User does not exist: ${SERVICE_USER}" >&2
  exit 1
fi
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"

echo "==> PyDistance-Service autostart install"
echo "    Install directory: ${INSTALL_DIR}"
echo "    Service user:      ${SERVICE_USER}"

for cmd in python3 systemctl; do
  if ! command -v "${cmd}" &>/dev/null; then
    echo "Required command not found: ${cmd}" >&2
    exit 1
  fi
done

if command -v i2cdetect &>/dev/null; then
  echo "    Tip: verify I2C with: i2cdetect -y 1"
else
  echo "    Warning: i2c-tools not installed (optional: apt install i2c-tools)"
fi

if [[ ! -f "${INSTALL_DIR}/requirements.txt" ]]; then
  echo "requirements.txt not found under ${INSTALL_DIR}" >&2
  exit 1
fi

run_as_user() {
  sudo -u "${SERVICE_USER}" -H bash -c "cd '${INSTALL_DIR}' && $*"
}

echo "==> Preparing Python virtual environment and dependencies"
if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
  run_as_user "python3 -m venv .venv"
fi
run_as_user ".venv/bin/pip install -r requirements.txt"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  if [[ -f "${INSTALL_DIR}/.env.example" ]]; then
    cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}/.env"
    echo "    Created .env from .env.example — review hardware settings before production use."
  else
    echo "Warning: no .env or .env.example found; service may use defaults only." >&2
  fi
fi

run_as_user "mkdir -p logs"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${INSTALL_DIR}/.venv" "${INSTALL_DIR}/logs" 2>/dev/null || true

if getent group i2c &>/dev/null; then
  if id -nG "${SERVICE_USER}" | tr ' ' '\n' | grep -qx i2c; then
    echo "    User ${SERVICE_USER} is already in group i2c"
  else
    usermod -aG i2c "${SERVICE_USER}"
    echo "    Added ${SERVICE_USER} to group i2c (log out/in for interactive shells; systemd uses SupplementaryGroups)"
  fi
else
  echo "    Warning: group 'i2c' not found; enable I2C on your platform first"
fi

if [[ ! -f "${UNIT_TEMPLATE}" ]]; then
  echo "Unit template not found: ${UNIT_TEMPLATE}" >&2
  exit 1
fi

echo "==> Installing systemd unit to ${UNIT_DEST}"
sed \
  -e "s|@INSTALL_DIR@|${INSTALL_DIR}|g" \
  -e "s|@SERVICE_USER@|${SERVICE_USER}|g" \
  -e "s|@SERVICE_GROUP@|${SERVICE_GROUP}|g" \
  "${UNIT_TEMPLATE}" > "${UNIT_DEST}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo ""
echo "==> Installation complete"
systemctl --no-pager status "${SERVICE_NAME}" || true
echo ""
echo "Health check: curl -fsS http://127.0.0.1:8000/health"
echo "View logs:    sudo journalctl -u ${SERVICE_NAME} -f"
echo "After .env changes: sudo systemctl restart ${SERVICE_NAME}"
