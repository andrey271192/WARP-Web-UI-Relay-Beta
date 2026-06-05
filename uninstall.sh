#!/usr/bin/env bash
# WARP Web UI — удаление одной командой
set -euo pipefail

INSTALL_DIR="${WARP_WEBUI_INSTALL_DIR:-/opt/warp-webui}"
ENV_FILE="/etc/default/warp-webui"
SERVICE_NAME="warp-webui"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
BRIDGE_UNIT="/etc/systemd/system/warp-socks-bridge.service"
RELAY_TAG="${WARP_RELAY_RULE_TAG:-WR_WEBUI_RELAY}"

if [[ "${EUID:-0}" -ne 0 ]]; then
  echo "Запустите от root: sudo bash uninstall.sh"
  exit 1
fi

echo "=== Удаление WARP Web UI ==="

clean_relay_nftables() {
  command -v nft >/dev/null 2>&1 || return 0
  nft -a list ruleset 2>/dev/null | awk -v tag="comment \""${RELAY_TAG}"\"" '
    /^table / { family=$2; table=$3 }
    /^[[:space:]]*chain / { chain=$2 }
    index($0, tag) && match($0, /# handle [0-9]+/) {
      handle=substr($0, RSTART + 9, RLENGTH - 9)
      print family, table, chain, handle
    }
  ' | while read -r family table chain handle; do
    nft delete rule "${family}" "${table}" "${chain}" handle "${handle}" 2>/dev/null || true
  done
  nft delete set ip nat wr_webui_relay_ports 2>/dev/null || true
  nft delete set ip filter wr_webui_relay_ports 2>/dev/null || true
  nft list ruleset > /etc/nftables.conf 2>/dev/null || true
}

clean_relay_iptables() {
  command -v iptables >/dev/null 2>&1 || return 0
  iptables -t nat -S 2>/dev/null | grep "${RELAY_TAG}" | sed 's/^-A/-D/' | while read -r rule; do
    eval iptables -t nat "$rule" 2>/dev/null || true
  done
  iptables -S 2>/dev/null | grep "${RELAY_TAG}" | sed 's/^-A/-D/' | while read -r rule; do
    eval iptables "$rule" 2>/dev/null || true
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save 2>/dev/null || true
  fi
}

echo "Удаляем managed WARP Relay rules (${RELAY_TAG})..."
clean_relay_nftables
clean_relay_iptables

if systemctl is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
  systemctl stop "${SERVICE_NAME}.service"
fi
systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true

if [[ -f "${UNIT}" ]]; then
  rm -f "${UNIT}"
fi

if systemctl is-active --quiet warp-socks-bridge.service 2>/dev/null; then
  systemctl stop warp-socks-bridge.service 2>/dev/null || true
fi
systemctl disable warp-socks-bridge.service 2>/dev/null || true
[[ -f "${BRIDGE_UNIT}" ]] && rm -f "${BRIDGE_UNIT}"

systemctl daemon-reload

read -rp "Удалить файлы приложения в ${INSTALL_DIR}? [y/N]: " REMOVE_APP
if [[ "${REMOVE_APP,,}" == "y" || "${REMOVE_APP,,}" == "yes" || "${REMOVE_APP,,}" == "д" || "${REMOVE_APP,,}" == "да" ]]; then
  rm -rf "${INSTALL_DIR}"
fi

read -rp "Удалить конфиг ${ENV_FILE} и каталог /etc/warp-webui/? [y/N]: " REMOVE_CFG
if [[ "${REMOVE_CFG,,}" == "y" || "${REMOVE_CFG,,}" == "yes" || "${REMOVE_CFG,,}" == "д" || "${REMOVE_CFG,,}" == "да" ]]; then
  rm -f "${ENV_FILE}"
  rm -rf /etc/warp-webui
fi

read -rp "Удалить пакет cloudflare-warp (apt remove)? [y/N]: " REMOVE_WARP
if [[ "${REMOVE_WARP,,}" == "y" || "${REMOVE_WARP,,}" == "yes" || "${REMOVE_WARP,,}" == "д" || "${REMOVE_WARP,,}" == "да" ]]; then
  if [[ -x "${INSTALL_DIR}/scripts/warp-uninstall-cf.sh" ]]; then
    WARP_PROXY_PORT=1024 bash "${INSTALL_DIR}/scripts/warp-uninstall-cf.sh"
  elif command -v warp-cli >/dev/null 2>&1; then
    warp-cli --accept-tos disconnect 2>/dev/null || true
    systemctl stop warp-svc 2>/dev/null || true
    apt-get remove -y -qq cloudflare-warp 2>/dev/null || apt-get purge -y -qq cloudflare-warp 2>/dev/null || true
  else
    echo "Пакет cloudflare-warp не найден; пропуск."
  fi
fi

echo "WARP Web UI удалён."
