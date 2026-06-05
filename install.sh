#!/usr/bin/env bash
# WARP Web UI — установка одной командой (Debian/Ubuntu, от root)
set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
REPO_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
INSTALL_DIR="${WARP_WEBUI_INSTALL_DIR:-/opt/warp-webui}"
ENV_FILE="/etc/default/warp-webui"
SERVICE_NAME="warp-webui"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
ALIASES_DIR="/etc/warp-webui"
LOG_DIR="/var/log/warp-webui"
BACKUP_DIR="/var/backups/warp-webui"
RAW_BASE="${WARP_WEBUI_REPO_RAW:-https://raw.githubusercontent.com/andrey271192/WARP-Web-UI-Relay-Beta/main}"

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Запустите от root: sudo bash install.sh"
  exit 1
fi

echo "=== Установка WARP Web UI ==="
echo

fetch_if_missing() {
  local rel_path="$1"
  local dest="${REPO_DIR}/${rel_path}"
  if [[ -f "${dest}" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "${dest}")"
  echo "Скачиваем ${rel_path} ..."
  curl -fsSL "${RAW_BASE}/${rel_path}" -o "${dest}"
}

if [[ ! -f "${REPO_DIR}/app.py" || ! -f "${REPO_DIR}/systemd/warp-webui.service" ]]; then
  TMP_REPO="$(mktemp -d)"
  trap 'rm -rf "${TMP_REPO:-}"' EXIT
  REPO_DIR="${TMP_REPO}"
  fetch_if_missing "app.py"
  fetch_if_missing "scripts/warp-install-cf.sh"
  fetch_if_missing "scripts/warp-uninstall-cf.sh"
  fetch_if_missing "systemd/warp-webui.service"
fi

prompt() {
  local var_name="$1" prompt_text="$2" default_val="$3"
  local input
  if [[ -n "${!var_name:-}" ]]; then
    return 0
  fi
  read -rp "${prompt_text} [${default_val}]: " input
  if [[ -z "${input}" ]]; then
    printf -v "${var_name}" '%s' "${default_val}"
  else
    printf -v "${var_name}" '%s' "${input}"
  fi
}

echo "Порт SOCKS: локальный порт режима proxy у warp-cli (для пресетов x-ui / Amnezia)."
echo "Частые значения: 40000 (как у warp-offline) или 1024 (по умолчанию у cloudflare-warp)."
prompt WARP_PROXY_PORT "Порт SOCKS-прокси" "40000"
if ! [[ "${WARP_PROXY_PORT}" =~ ^[0-9]+$ ]] || (( WARP_PROXY_PORT < 1 || WARP_PROXY_PORT > 65535 )); then
  echo "Некорректный порт SOCKS: ${WARP_PROXY_PORT}"
  exit 1
fi

prompt WARP_WEBUI_PORT "HTTP-порт веб-панели" "3030"
if ! [[ "${WARP_WEBUI_PORT}" =~ ^[0-9]+$ ]] || (( WARP_WEBUI_PORT < 1 || WARP_WEBUI_PORT > 65535 )); then
  echo "Некорректный порт веб-панели: ${WARP_WEBUI_PORT}"
  exit 1
fi

prompt WARP_WEBUI_USER "Логин администратора веб-панели" "warpadmin"

if [[ -z "${WARP_WEBUI_PASS:-}" ]]; then
  while true; do
    read -rsp "Пароль администратора (минимум 8 символов): " WARP_WEBUI_PASS
    echo
    if [[ "${#WARP_WEBUI_PASS}" -ge 8 ]]; then
      break
    fi
    echo "Пароль слишком короткий. Нужно не менее 8 символов."
  done
elif [[ "${#WARP_WEBUI_PASS}" -lt 8 ]]; then
  echo "WARP_WEBUI_PASS слишком короткий. Нужно не менее 8 символов."
  exit 1
fi

# Публичный адрес для подсказок в пресетах клиентов (необязательно)
WARP_PUBLIC_HOST="${WARP_PUBLIC_HOST:-}"
if [[ -z "${WARP_PUBLIC_HOST}" ]]; then
  WARP_PUBLIC_HOST="$(curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || true)"
fi
if [[ -z "${WARP_PUBLIC_HOST}" ]]; then
  WARP_PUBLIC_HOST="$(hostname -f 2>/dev/null || hostname)"
fi

echo
echo "Установка в ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}/scripts" "${ALIASES_DIR}" "${LOG_DIR}" "${BACKUP_DIR}" /etc/sysctl.d /etc/iptables
touch /etc/nftables.conf
install -m 0755 "${REPO_DIR}/app.py" "${INSTALL_DIR}/app.py"
install -m 0755 "${REPO_DIR}/scripts/warp-install-cf.sh" "${INSTALL_DIR}/scripts/warp-install-cf.sh"
install -m 0755 "${REPO_DIR}/scripts/warp-uninstall-cf.sh" "${INSTALL_DIR}/scripts/warp-uninstall-cf.sh"

umask 077
cat > "${ENV_FILE}" <<EOF
WARP_WEBUI_USER=${WARP_WEBUI_USER}
WARP_WEBUI_PASS=${WARP_WEBUI_PASS}
WARP_WEBUI_HOST=0.0.0.0
WARP_WEBUI_PORT=${WARP_WEBUI_PORT}
WARP_PROXY_PORT=${WARP_PROXY_PORT}
WARP_PUBLIC_HOST=${WARP_PUBLIC_HOST}
WARP_INSTALL_SCRIPT=${INSTALL_DIR}/scripts/warp-install-cf.sh
WARP_UNINSTALL_SCRIPT=${INSTALL_DIR}/scripts/warp-uninstall-cf.sh
WARP_CLIENT_ALIASES=${ALIASES_DIR}/client-aliases.json
EOF
chmod 600 "${ENV_FILE}"

if [[ ! -f "${ALIASES_DIR}/client-aliases.json" ]]; then
  echo '{}' > "${ALIASES_DIR}/client-aliases.json"
  chmod 600 "${ALIASES_DIR}/client-aliases.json"
fi

sed \
  -e "s|@INSTALL_DIR@|${INSTALL_DIR}|g" \
  -e "s|@ENV_FILE@|${ENV_FILE}|g" \
  "${REPO_DIR}/systemd/warp-webui.service" > "${UNIT_DST}"

if command -v warp-cli >/dev/null 2>&1; then
  warp-cli --accept-tos mode proxy 2>/dev/null || true
  warp-cli --accept-tos proxy port "${WARP_PROXY_PORT}" 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

# По желанию: правило firewall для порта панели
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi active; then
  ufw allow "${WARP_WEBUI_PORT}/tcp" comment 'warp-webui' || true
fi

echo
echo "=== Установка завершена ==="
echo "Веб-панель:  http://${WARP_PUBLIC_HOST}:${WARP_WEBUI_PORT}/"
echo "Вход:        ${WARP_WEBUI_USER} / (пароль, который вы ввели)"
echo "SOCKS:       127.0.0.1:${WARP_PROXY_PORT} (после установки WARP и включения режима proxy)"
echo "Конфиг:      ${ENV_FILE}"
echo
echo "Далее: откройте веб-панель и нажмите «Install WARP», если пакет cloudflare-warp ещё не установлен."
echo "Порт proxy можно сменить в UI или в ${ENV_FILE} (переменная WARP_PROXY_PORT) и перезапустить сервис."
