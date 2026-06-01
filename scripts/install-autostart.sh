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

echo "==> Installing system packages (I2C + libgpiod for adafruit-blinka)"
if command -v apt-get &>/dev/null; then
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    i2c-tools \
    libgpiod2 \
    python3-libgpiod \
    || {
      echo "Warning: some apt packages failed; blinka may need: apt install i2c-tools libgpiod2 python3-libgpiod" >&2
    }
else
  echo "    Warning: apt-get not found; install i2c-tools and python3-libgpiod manually" >&2
fi

if command -v i2cdetect &>/dev/null; then
  echo "    Tip: verify I2C with: i2cdetect -y 1"
fi

if [[ ! -f "${INSTALL_DIR}/requirements.txt" ]]; then
  echo "requirements.txt not found under ${INSTALL_DIR}" >&2
  exit 1
fi

run_as_user() {
  sudo -u "${SERVICE_USER}" -H bash -c "cd '${INSTALL_DIR}' && $*"
}

venv_python() {
  echo "${INSTALL_DIR}/.venv/bin/python"
}

venv_has_pip() {
  run_as_user "$(venv_python) -m pip --version" &>/dev/null
}

bootstrap_venv_pip() {
  echo "    Bootstrapping pip in virtual environment..."
  if ! run_as_user "$(venv_python) -m ensurepip --upgrade"; then
    echo "Failed to install pip into .venv." >&2
    echo "On Debian/Ubuntu/Orange Pi, install: sudo apt install python3-venv python3-pip" >&2
    exit 1
  fi
}

echo "==> Preparing Python virtual environment and dependencies"
# adafruit-blinka needs system gpiod bindings (python3-libgpiod) visible inside the venv.
VENV_FLAGS=(--system-site-packages)
if [[ -f "${INSTALL_DIR}/.venv/pyvenv.cfg" ]] \
  && ! grep -q '^include-system-site-packages = true' "${INSTALL_DIR}/.venv/pyvenv.cfg"; then
  echo "    Recreating .venv with --system-site-packages (required for libgpiod / blinka)"
  rm -rf "${INSTALL_DIR}/.venv"
fi

if [[ ! -x "$(venv_python)" ]]; then
  if ! run_as_user "python3 -m venv .venv ${VENV_FLAGS[*]}"; then
    echo "Failed to create virtual environment." >&2
    echo "On Debian/Ubuntu/Orange Pi, install: sudo apt install python3-venv" >&2
    exit 1
  fi
fi

if ! venv_has_pip; then
  bootstrap_venv_pip
fi

if ! venv_has_pip; then
  echo "pip is still unavailable in .venv after ensurepip." >&2
  echo "Try: rm -rf .venv && sudo apt install python3-venv python3-pip && re-run this script" >&2
  exit 1
fi

run_as_user "$(venv_python) -m pip install -r requirements.txt"

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
