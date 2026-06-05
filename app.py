#!/usr/bin/env python3
import base64
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import logging
from logging.handlers import RotatingFileHandler

USER = os.environ.get("WARP_WEBUI_USER", "admin")
PASS = os.environ.get("WARP_WEBUI_PASS", "")
if not PASS:
    raise SystemExit("WARP_WEBUI_PASS is not set. Configure /etc/default/warp-webui or run install.sh.")
HOST = os.environ.get("WARP_WEBUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("WARP_WEBUI_PORT", "3030"))

LOG_DIR = os.environ.get("WARP_WEBUI_LOG_DIR", "/var/log/warp-webui")
LOG_FILE = os.environ.get("WARP_WEBUI_LOG_FILE", os.path.join(LOG_DIR, "warp-webui.log"))
LOG_MAX_BYTES = int(os.environ.get("WARP_WEBUI_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUPS = int(os.environ.get("WARP_WEBUI_LOG_BACKUPS", "3"))

BACKUP_DIR = os.environ.get("WARP_WEBUI_BACKUP_DIR", "/var/backups/warp-webui")
INSTALL_SCRIPT = os.environ.get("WARP_INSTALL_SCRIPT", "/opt/warp-webui/scripts/warp-install-cf.sh")
UNINSTALL_SCRIPT = os.environ.get("WARP_UNINSTALL_SCRIPT", "/opt/warp-webui/scripts/warp-uninstall-cf.sh")
XUI_CONFIG = os.environ.get("XUI_CONFIG", "/usr/local/x-ui/bin/config.json")
AMNEZIA_CONTAINER = os.environ.get("AMNEZIA_XRAY_CONTAINER", "amnezia-xray")
AMNEZIA_CONFIG = os.environ.get("AMNEZIA_XRAY_CONFIG", "/opt/amnezia/xray/server.json")
CLIENT_ALIASES_PATH = os.environ.get("WARP_CLIENT_ALIASES", "/etc/warp-webui/client-aliases.json")
BRIDGE_HOST = os.environ.get("WARP_SOCKS_BRIDGE_HOST", "172.17.0.1")
BRIDGE_PORT = int(os.environ.get("WARP_SOCKS_BRIDGE_PORT", "11025"))
SOCKS_OUTBOUND_TAG = "warp-socks"
RELAY_RULE_TAG = os.environ.get("WARP_RELAY_RULE_TAG", "WR_WEBUI_RELAY")
RELAY_STATE_PATH = os.environ.get("WARP_RELAY_STATE", "/etc/warp-webui/relay-state.json")
RELAY_DEFAULT_ENDPOINT = os.environ.get("WARP_RELAY_DEFAULT_ENDPOINT", "engage.cloudflareclient.com")
NFT_CONF = os.environ.get("WARP_RELAY_NFT_CONF", "/etc/nftables.conf")
RELAY_MULTI_PORTS = [
    500, 854, 859, 864, 878, 880, 890, 891, 894, 903, 908, 928, 934, 939, 942,
    943, 945, 946, 955, 968, 987, 988, 1002, 1010, 1014, 1018, 1070, 1074,
    1180, 1387, 1701, 1843, 2371, 2408, 2506, 3138, 3476, 3581, 3854, 4177,
    4198, 4233, 4500, 5279, 5956, 7103, 7152, 7156, 7281, 7559, 8319, 8742,
    8854, 8886,
]

LOG_BUFFER = deque(maxlen=250)


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger("warp-webui")
    logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s"))
        logger.addHandler(fh)
    return logger


LOGGER = setup_logging()


def log_event(level: str, message: str, **fields):
    entry = {"ts": _utc_now_iso(), "level": level, "message": message, **fields}
    LOG_BUFFER.append(entry)
    try:
        LOGGER.info(json.dumps(entry, ensure_ascii=True))
    except Exception:
        pass


def run_cmd(cmd, timeout=120):
    start = time.time()
    try:
        proc = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        dur_ms = int((time.time() - start) * 1000)
        log_event("error", "command_timeout", cmd=cmd, duration_ms=dur_ms)
        return 124, "", "timeout"
    dur_ms = int((time.time() - start) * 1000)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    log_event(
        "info",
        "command_executed",
        cmd=cmd,
        returncode=proc.returncode,
        duration_ms=dur_ms,
        stdout_tail=out[-2000:],
        stderr_tail=err[-2000:],
    )
    return proc.returncode, out, err


def backup_file(path: str, label: str):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    base = os.path.basename(path)
    dest = os.path.join(BACKUP_DIR, f"{label}-{base}.{ts}")
    shutil.copy2(path, dest)
    return dest


def parse_warp_status_text(stdout: str, stderr: str, code: int):
    connected = None
    health = None
    account = None
    device = None
    text = stdout or ""
    low = text.lower()
    if "connected" in low and "disconnected" not in low:
        connected = True
    if "disconnected" in low:
        connected = False
    for line in text.splitlines():
        l = line.strip()
        ll = l.lower()
        if ll.startswith("status") and ":" in l:
            val = l.split(":", 1)[1].strip().lower()
            if "connected" in val:
                connected = True
            if "disconnected" in val:
                connected = False
        if ll.startswith("health") and ":" in l:
            health = l.split(":", 1)[1].strip()
        if ll.startswith("account") and ":" in l:
            account = l.split(":", 1)[1].strip()
        if ll.startswith("device") and ":" in l:
            device = l.split(":", 1)[1].strip()
    return {
        "connected": connected,
        "health": health,
        "account": account,
        "device": device,
        "code": code,
        "stdout": stdout,
        "stderr": stderr,
    }


def warp_status():
    code, out, err = run_cmd(["warp-cli", "--accept-tos", "status"])
    return parse_warp_status_text(out, err, code)


def parse_registration(stdout: str):
    info = {"raw": stdout, "account_type": None, "account_id": None, "device_id": None, "license_masked": None}
    for line in (stdout or "").splitlines():
        l = line.strip()
        if l.lower().startswith("account type:"):
            info["account_type"] = l.split(":", 1)[1].strip()
        elif l.lower().startswith("account id:"):
            info["account_id"] = l.split(":", 1)[1].strip()
        elif l.lower().startswith("device id:"):
            info["device_id"] = l.split(":", 1)[1].strip()
        elif l.lower().startswith("license:"):
            lic = l.split(":", 1)[1].strip()
            if lic:
                info["license_masked"] = lic[:4] + "…" + lic[-4:] if len(lic) > 10 else lic
    return info


def warp_registration():
    code, out, err = run_cmd(["warp-cli", "--accept-tos", "registration", "show"])
    data = parse_registration(out)
    data.update({"code": code, "stderr": err})
    return data


def get_proxy_port():
    code, out, err = run_cmd(["warp-cli", "--accept-tos", "settings"])
    m = re.search(r"WarpProxy on port (\d+)", out or "")
    if m:
        return {"port": int(m.group(1)), "source": "settings", "code": code}
    code2, out2, err2 = run_cmd(["ss", "-lnt"])
    for line in (out2 or "").splitlines():
        if "127.0.0.1:" in line:
            mm = re.search(r"127\.0\.0\.1:(\d+).*warp-svc", line)
            if mm:
                return {"port": int(mm.group(1)), "source": "ss", "code": code2}
    return {"port": None, "source": "unknown", "code": code, "stderr": err}


def set_proxy_port(port: int):
    if port < 1 or port > 65535:
        return 400, {"error": "invalid port"}
    c1, o1, e1 = run_cmd(["warp-cli", "--accept-tos", "mode", "proxy"])
    c2, o2, e2 = run_cmd(["warp-cli", "--accept-tos", "proxy", "port", str(port)])
    ok = c1 == 0 and c2 == 0
    return (200 if ok else 500), {
        "port": port,
        "mode_code": c1,
        "port_code": c2,
        "stdout": "\n".join(filter(None, [o1, o2])),
        "stderr": "\n".join(filter(None, [e1, e2])),
        "proxy": get_proxy_port(),
    }


def apply_license_key(key: str):
    key = (key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", key):
        return 400, {"error": "invalid license key format"}
    code, out, err = run_cmd(["warp-cli", "--accept-tos", "registration", "license", key])
    return (200 if code == 0 else 500), {
        "result_code": code,
        "stdout": out,
        "stderr": err,
        "registration": warp_registration(),
    }


def run_script(path: str, extra_env=None):
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return 500, {"error": "script missing", "path": path}
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    start = time.time()
    proc = subprocess.run([path], shell=False, capture_output=True, text=True, timeout=600, env=env)
    dur_ms = int((time.time() - start) * 1000)
    log_event(
        "info",
        "script_executed",
        path=path,
        returncode=proc.returncode,
        duration_ms=dur_ms,
        stdout_tail=(proc.stdout or "")[-2000:],
        stderr_tail=(proc.stderr or "")[-2000:],
    )
    return (
        200 if proc.returncode == 0 else 500,
        {
            "path": path,
            "result_code": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": (proc.stderr or "").strip(),
            "duration_ms": dur_ms,
        },
    )


def ensure_socks_bridge(target_port: int):
    unit_path = "/etc/systemd/system/warp-socks-bridge.service"
    unit = f"""[Unit]
Description=WARP SOCKS bridge for Docker ({BRIDGE_HOST}:{BRIDGE_PORT})
After=network-online.target warp-svc.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP-LISTEN:{BRIDGE_PORT},bind={BRIDGE_HOST},reuseaddr,fork TCP:127.0.0.1:{target_port}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
"""
    with open(unit_path, "w", encoding="utf-8") as f:
        f.write(unit)
    run_cmd(["systemctl", "daemon-reload"])
    c1, _, _ = run_cmd(["systemctl", "enable", "--now", "warp-socks-bridge.service"])
    c2, out, err = run_cmd(["systemctl", "is-active", "warp-socks-bridge.service"])
    return {
        "unit": unit_path,
        "bridge": f"{BRIDGE_HOST}:{BRIDGE_PORT}",
        "target": f"127.0.0.1:{target_port}",
        "enable_code": c1,
        "active": out.strip() if c2 == 0 else "unknown",
        "stderr": err,
    }


def _merge_xui_config(cfg: dict, socks_port: int):
    outbounds = cfg.setdefault("outbounds", [])
    socks = {
        "tag": SOCKS_OUTBOUND_TAG,
        "protocol": "socks",
        "settings": {
            "servers": [
                {
                    "address": "127.0.0.1",
                    "port": socks_port,
                    "users": [],
                }
            ]
        },
    }
    replaced = False
    for i, ob in enumerate(outbounds):
        if ob.get("tag") == SOCKS_OUTBOUND_TAG:
            outbounds[i] = socks
            replaced = True
            break
    if not replaced:
        outbounds.append(socks)
    routing = cfg.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    rule = {
        "type": "field",
        "domain": ["geosite:google"],
        "outboundTag": SOCKS_OUTBOUND_TAG,
    }
    if not any(r.get("outboundTag") == SOCKS_OUTBOUND_TAG and "geosite:google" in str(r.get("domain")) for r in rules):
        rules.append(rule)
    return cfg


def apply_xui_preset(socks_port: int):
    if not os.path.isfile(XUI_CONFIG):
        return 500, {"error": "x-ui config not found", "path": XUI_CONFIG}
    backup = backup_file(XUI_CONFIG, "x-ui")
    with open(XUI_CONFIG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg = _merge_xui_config(cfg, socks_port)
    with open(XUI_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    rc, out, err = run_cmd(["systemctl", "restart", "x-ui"])
    return (200 if rc == 0 else 500), {
        "backup": backup,
        "config": XUI_CONFIG,
        "socks": f"127.0.0.1:{socks_port}",
        "restart_code": rc,
        "stderr": err,
    }


def _read_amnezia_config():
    code, out, err = run_cmd(["docker", "exec", AMNEZIA_CONTAINER, "cat", AMNEZIA_CONFIG])
    if code != 0:
        return None, err or out
    return json.loads(out), ""


def _write_amnezia_config(cfg: dict):
    tmp = os.path.join(BACKUP_DIR, "amnezia-server.json.tmp")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    dest = f"{AMNEZIA_CONTAINER}:{AMNEZIA_CONFIG}"
    rc, out, err = run_cmd(["docker", "cp", tmp, dest])
    if rc != 0:
        return rc, err or out
    rc2, out2, err2 = run_cmd(["docker", "restart", AMNEZIA_CONTAINER])
    return rc2, err2 or out2


def _merge_amnezia_config(cfg: dict, bridge_port: int):
    outbounds = cfg.setdefault("outbounds", [])
    socks = {
        "tag": SOCKS_OUTBOUND_TAG,
        "protocol": "socks",
        "settings": {
            "servers": [
                {
                    "address": BRIDGE_HOST,
                    "port": bridge_port,
                }
            ]
        },
    }
    replaced = False
    for i, ob in enumerate(outbounds):
        if ob.get("tag") == SOCKS_OUTBOUND_TAG:
            outbounds[i] = socks
            replaced = True
            break
    if not replaced:
        outbounds.insert(0, socks)
    routing = cfg.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    rule = {
        "type": "field",
        "domain": ["geosite:google"],
        "outboundTag": SOCKS_OUTBOUND_TAG,
    }
    if not any(r.get("outboundTag") == SOCKS_OUTBOUND_TAG and "geosite:google" in str(r.get("domain")) for r in rules):
        rules.append(rule)
    return cfg


def apply_amnezia_preset(socks_port: int):
    bridge_info = ensure_socks_bridge(socks_port)
    cfg, err = _read_amnezia_config()
    if cfg is None:
        return 500, {"error": "read amnezia config failed", "detail": err, "bridge": bridge_info}
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    snap = os.path.join(BACKUP_DIR, f"amnezia-server.json.{ts}")
    with open(snap, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    cfg = _merge_amnezia_config(cfg, BRIDGE_PORT)
    rc, detail = _write_amnezia_config(cfg)
    client_rows = list_amnezia_clients_from_cfg(cfg)
    log_event(
        "info",
        "amnezia_preset_applied",
        clients=format_clients_for_log(client_rows),
    )
    return (200 if rc == 0 else 500), {
        "backup": snap,
        "bridge": bridge_info,
        "socks_via": f"{BRIDGE_HOST}:{BRIDGE_PORT} -> 127.0.0.1:{socks_port}",
        "restart_code": rc,
        "detail": detail,
        "clients": client_rows,
        "clients_display": format_clients_for_log(client_rows),
        "ok": rc == 0,
    }





_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _short_uuid(uid: str) -> str:
    uid = (uid or "").strip()
    if len(uid) > 20:
        return uid[:8] + "…" + uid[-8:]
    return uid


def _is_uuid_like(s: str) -> bool:
    return bool(_UUID_RE.fullmatch((s or "").strip()))


def load_client_aliases() -> dict:
    try:
        if os.path.isfile(CLIENT_ALIASES_PATH):
            with open(CLIENT_ALIASES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k).lower(): str(v).strip() for k, v in data.items() if str(v).strip()}
    except Exception as e:
        log_event("warning", "aliases_load_failed", path=CLIENT_ALIASES_PATH, error=str(e))
    return {}


def save_client_aliases(aliases: dict) -> dict:
    os.makedirs(os.path.dirname(CLIENT_ALIASES_PATH) or "/etc/warp-webui", exist_ok=True)
    normalized = {}
    for k, v in (aliases or {}).items():
        key = str(k).strip().lower()
        val = str(v).strip()
        if key and val:
            normalized[key] = val
    tmp = CLIENT_ALIASES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CLIENT_ALIASES_PATH)
    return normalized


def resolve_client_display_name(client_id, email=None, comment=None, aliases=None):
    cid = (client_id or "").strip()
    cid_lower = cid.lower()
    aliases = aliases if aliases is not None else load_client_aliases()
    if cid_lower in aliases:
        return aliases[cid_lower], "alias"
    comment = (comment or "").strip()
    if comment and not _is_uuid_like(comment):
        return comment, "comment"
    em = (email or "").strip()
    if em and not _is_uuid_like(em) and em.lower() != cid_lower:
        return em, "email"
    return cid, "uuid"


def enrich_amnezia_client_row(c: dict, aliases=None):
    cid = (c.get("id") or "").strip()
    email = (c.get("email") or cid).strip()
    comment = (c.get("comment") or "").strip()
    display, source = resolve_client_display_name(cid, email=email, comment=comment, aliases=aliases)
    routing_user = email or cid
    return {
        "uuid": cid,
        "id": cid,
        "email": email,
        "flow": c.get("flow", ""),
        "comment": comment,
        "displayName": display,
        "shortUuid": _short_uuid(cid),
        "source": source,
        "routingUser": routing_user,
    }


def format_clients_for_log(clients):
    labels = []
    for c in clients or []:
        if isinstance(c, dict):
            name = c.get("displayName") or c.get("email") or c.get("uuid") or c.get("id")
            uid = c.get("uuid") or c.get("id") or ""
            labels.append(f"{name} ({_short_uuid(uid)})")
        else:
            labels.append(str(c))
    return labels


def users_to_display_summary(users, cfg=None):
    if cfg is None:
        cfg, _ = _read_amnezia_config()
    by_routing = {}
    if cfg:
        for row in list_amnezia_clients_from_cfg(cfg):
            by_routing[row["routingUser"]] = row
    names = []
    for u in users or []:
        row = by_routing.get(u)
        if row:
            names.append(f"{row['displayName']} ({row['shortUuid']})")
        elif _is_uuid_like(u):
            names.append(_short_uuid(u))
        else:
            names.append(u)
    return names

def list_amnezia_clients_from_cfg(cfg: dict):
    aliases = load_client_aliases()
    clients = []
    for inbound in cfg.get("inbounds", []):
        if inbound.get("protocol") != "vless":
            continue
        for c in inbound.get("settings", {}).get("clients", []):
            clients.append(enrich_amnezia_client_row(c, aliases=aliases))
    return clients


def list_amnezia_clients():
    cfg, err = _read_amnezia_config()
    if cfg is None:
        return None, err
    return list_amnezia_clients_from_cfg(cfg), ""


def _strip_managed_warp_rules(rules):
    kept = []
    for r in rules:
        if r.get("outboundTag") != SOCKS_OUTBOUND_TAG:
            kept.append(r)
            continue
        if r.get("user"):
            continue
        if "geosite:google" in str(r.get("domain", "")):
            continue
        kept.append(r)
    return kept


def _append_warp_routing_rules(cfg: dict, users, domains):
    routing = cfg.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    rules = _strip_managed_warp_rules(rules)
    domains = [d for d in (domains or ["geosite:google"]) if d]
    users = [u for u in (users or []) if u]
    if users:
        for u in users:
            rules.append({
                "type": "field",
                "user": [u],
                "domain": domains,
                "outboundTag": SOCKS_OUTBOUND_TAG,
            })
    else:
        rules.append({
            "type": "field",
            "domain": domains,
            "outboundTag": SOCKS_OUTBOUND_TAG,
        })
    routing["rules"] = rules
    return cfg


def apply_amnezia_routing(socks_port: int, users=None, domains=None):
    bridge_info = ensure_socks_bridge(socks_port)
    cfg, err = _read_amnezia_config()
    if cfg is None:
        return 500, {"error": "read amnezia config failed", "detail": err, "bridge": bridge_info}
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    snap = os.path.join(BACKUP_DIR, f"amnezia-server.json.{ts}")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(snap, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    cfg = _merge_amnezia_config(cfg, BRIDGE_PORT)
    cfg = _append_warp_routing_rules(cfg, users, domains)
    rc, detail = _write_amnezia_config(cfg)
    client_rows = list_amnezia_clients_from_cfg(cfg)
    users_display = users_to_display_summary(users, cfg=cfg)
    log_event(
        "info",
        "amnezia_routing_applied",
        users=users or [],
        users_display=users_display,
        domains=domains or ["geosite:google"],
    )
    return (200 if rc == 0 else 500), {
        "backup": snap,
        "bridge": bridge_info,
        "users": users or [],
        "users_display": users_display,
        "domains": domains or ["geosite:google"],
        "clients": client_rows,
        "routing_rules": cfg.get("routing", {}).get("rules", []),
        "restart_code": rc,
        "detail": detail,
        "ok": rc == 0,
    }

def client_presets(socks_port: int):
    host_public = os.environ.get("WARP_PUBLIC_HOST", "")
    bridge = f"{BRIDGE_HOST}:{BRIDGE_PORT}"
    local = f"127.0.0.1:{socks_port}"
    return {
        "socks_port": socks_port,
        "local_socks": local,
        "docker_bridge_socks": bridge,
        "xray_outbound": {
            "tag": SOCKS_OUTBOUND_TAG,
            "protocol": "socks",
            "settings": {
                "servers": [{"address": "127.0.0.1", "port": socks_port}]
            },
        },
        "amnezia_outbound": {
            "tag": SOCKS_OUTBOUND_TAG,
            "protocol": "socks",
            "settings": {
                "servers": [{"address": BRIDGE_HOST, "port": BRIDGE_PORT}]
            },
        },
        "curl_example": f"curl -s --socks5-hostname {local} https://api.ipify.org",
        "v2rayN_socks": {"protocol": "socks", "server": "127.0.0.1", "port": socks_port},
        "note": f"On server use {local}; from Amnezia container use {bridge} (socat bridge).",
        "public_host_hint": host_public,
    }


def _valid_port(value) -> int:
    port = int(value)
    if port < 1 or port > 65535:
        raise ValueError("invalid port")
    return port


def _valid_ipv4(value: str) -> str:
    ip = ipaddress.ip_address((value or "").strip())
    if ip.version != 4:
        raise ValueError("IPv4 required")
    return str(ip)


def resolve_relay_endpoint(endpoint: str) -> dict:
    endpoint = (endpoint or RELAY_DEFAULT_ENDPOINT).strip()
    if not endpoint:
        raise ValueError("endpoint required")
    try:
        return {"endpoint": endpoint, "ip": _valid_ipv4(endpoint), "source": "ip"}
    except ValueError:
        pass
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", endpoint) or ".." in endpoint:
        raise ValueError("invalid endpoint")
    infos = socket.getaddrinfo(endpoint, None, socket.AF_INET, socket.SOCK_DGRAM)
    if not infos:
        raise ValueError("endpoint has no IPv4 address")
    return {"endpoint": endpoint, "ip": infos[0][4][0], "source": "dns"}


def get_public_ipv4() -> str:
    for url in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me"):
        code, out, _ = run_cmd(["curl", "-4fsS", "--max-time", "3", url], timeout=5)
        if code == 0:
            try:
                return _valid_ipv4((out or "").strip().splitlines()[0])
            except Exception:
                continue
    raise ValueError("cannot detect public IPv4; set sourceIp manually")


def detect_relay_firewall():
    if shutil.which("nft"):
        code, _, err = run_cmd(["nft", "list", "ruleset"], timeout=5)
        if code == 0 and "managed by iptables-nft" in (err or "") and shutil.which("iptables"):
            return "iptables"
        return "nftables"
    if shutil.which("iptables"):
        return "iptables"
    return None


def load_relay_state() -> dict:
    try:
        if os.path.isfile(RELAY_STATE_PATH):
            with open(RELAY_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        log_event("warning", "relay_state_load_failed", path=RELAY_STATE_PATH, error=str(e))
    return {
        "enabled": False,
        "mode": "single",
        "endpoint": RELAY_DEFAULT_ENDPOINT,
        "sourceIp": "",
        "relayPort": 4500,
        "targetPort": 4500,
    }


def save_relay_state(state: dict) -> dict:
    os.makedirs(os.path.dirname(RELAY_STATE_PATH) or "/etc/warp-webui", exist_ok=True)
    state = dict(state or {})
    state["updatedAt"] = _utc_now_iso()
    tmp = RELAY_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, RELAY_STATE_PATH)
    return state


def enable_relay_forwarding():
    with open("/etc/sysctl.d/ipv4-forwarding.conf", "w", encoding="utf-8") as f:
        f.write("net.ipv4.ip_forward=1\n")
    return run_cmd(["sysctl", "-w", "net.ipv4.ip_forward=1"])


def _relay_nft(args, timeout=30):
    return run_cmd(["nft", *args], timeout=timeout)


def _relay_clean_nft_rules():
    code, out, err = _relay_nft(["-a", "list", "ruleset"])
    deleted = []
    if code == 0:
        family = table = chain = None
        for line in (out or "").splitlines():
            stripped = line.strip()
            mt = re.match(r"table\s+(\S+)\s+(\S+)\s+\{", stripped)
            if mt:
                family, table, chain = mt.group(1), mt.group(2), None
                continue
            mc = re.match(r"chain\s+(\S+)\s+\{", stripped)
            if mc:
                chain = mc.group(1)
                continue
            mh = re.search(r"# handle (\d+)", stripped)
            if f'comment "{RELAY_RULE_TAG}"' in stripped and mh and family and table and chain:
                rc, _, detail = _relay_nft(["delete", "rule", family, table, chain, "handle", mh.group(1)])
                deleted.append({"table": f"{family} {table}", "chain": chain, "handle": mh.group(1), "code": rc, "detail": detail})
    for table in ("nat", "filter"):
        _relay_nft(["delete", "set", "ip", table, "wr_webui_relay_ports"])
    return {"deleted": deleted, "list_code": code, "list_error": err}


def _relay_clean_iptables_rules():
    deleted = []
    for table_args in (["-t", "nat"], []):
        code, out, err = run_cmd(["iptables", *table_args, "-S"])
        if code != 0:
            deleted.append({"table": table_args or ["filter"], "code": code, "detail": err})
            continue
        for line in (out or "").splitlines():
            if RELAY_RULE_TAG not in line or not line.startswith("-A "):
                continue
            delete_args = shlex.split(line.replace("-A", "-D", 1))
            rc, _, detail = run_cmd(["iptables", *table_args, *delete_args])
            deleted.append({"table": table_args or ["filter"], "rule": line, "code": rc, "detail": detail})
    return {"deleted": deleted}


def clean_relay_rules(firewall=None) -> dict:
    firewall = firewall or detect_relay_firewall()
    if firewall == "nftables":
        return _relay_clean_nft_rules()
    if firewall == "iptables":
        return _relay_clean_iptables_rules()
    return {"error": "nftables or iptables not found"}


def _save_relay_firewall(firewall: str):
    if firewall == "nftables":
        code, out, err = _relay_nft(["list", "ruleset"])
        if code == 0:
            with open(NFT_CONF, "w", encoding="utf-8") as f:
                f.write(out)
                f.write("\n")
        if shutil.which("systemctl"):
            run_cmd(["systemctl", "enable", "nftables"])
            run_cmd(["systemctl", "restart", "nftables"])
        return {"code": code, "path": NFT_CONF, "stderr": err}
    if shutil.which("netfilter-persistent"):
        code, out, err = run_cmd(["netfilter-persistent", "save"])
        return {"code": code, "stdout": out, "stderr": err}
    return {"code": 0, "note": "iptables rules active until reboot; install netfilter-persistent to persist"}


def _apply_relay_nft(src_ip: str, dst_ip: str, relay_port: int, target_port: int, mode: str):
    cmds = [
        ["add", "table", "ip", "nat"],
        ["add", "table", "ip", "filter"],
        ["add", "chain", "ip", "nat", "prerouting", "{", "type", "nat", "hook", "prerouting", "priority", "-100", ";", "}"],
        ["add", "chain", "ip", "nat", "postrouting", "{", "type", "nat", "hook", "postrouting", "priority", "100", ";", "}"],
        ["add", "chain", "ip", "filter", "forward", "{", "type", "filter", "hook", "forward", "priority", "filter", ";", "}"],
    ]
    results = []
    critical = []
    for args in cmds:
        rc, out, err = _relay_nft(args)
        results.append({"cmd": ["nft", *args], "code": rc, "stderr": err, "stdout": out})
    if mode == "multiport":
        for table in ("nat", "filter"):
            res = _nft_result(["add", "set", "ip", table, "wr_webui_relay_ports", "{", "type", "inet_service", ";", "flags", "interval", ";", "}"])
            results.append(res)
            critical.append(res)
            for port in RELAY_MULTI_PORTS:
                res = _nft_result(["add", "element", "ip", table, "wr_webui_relay_ports", "{", str(port), "}"])
                results.append(res)
                critical.append(res)
        rule_cmds = [
            ["add", "rule", "ip", "nat", "prerouting", "ip", "daddr", src_ip, "udp", "dport", "@wr_webui_relay_ports", "dnat", "to", dst_ip, "comment", RELAY_RULE_TAG],
            ["add", "rule", "ip", "nat", "postrouting", "ip", "daddr", dst_ip, "udp", "dport", "@wr_webui_relay_ports", "masquerade", "comment", RELAY_RULE_TAG],
            ["add", "rule", "ip", "filter", "forward", "ip", "daddr", dst_ip, "udp", "dport", "@wr_webui_relay_ports", "accept", "comment", RELAY_RULE_TAG],
            ["add", "rule", "ip", "filter", "forward", "ip", "saddr", dst_ip, "udp", "sport", "@wr_webui_relay_ports", "accept", "comment", RELAY_RULE_TAG],
        ]
    else:
        rule_cmds = [
            ["add", "rule", "ip", "nat", "prerouting", "ip", "daddr", src_ip, "udp", "dport", str(relay_port), "dnat", "to", f"{dst_ip}:{target_port}", "comment", RELAY_RULE_TAG],
            ["add", "rule", "ip", "nat", "postrouting", "ip", "daddr", dst_ip, "udp", "dport", str(target_port), "masquerade", "comment", RELAY_RULE_TAG],
            ["add", "rule", "ip", "filter", "forward", "ip", "daddr", dst_ip, "udp", "dport", str(target_port), "accept", "comment", RELAY_RULE_TAG],
            ["add", "rule", "ip", "filter", "forward", "ip", "saddr", dst_ip, "udp", "sport", str(target_port), "accept", "comment", RELAY_RULE_TAG],
        ]
    for args in rule_cmds:
        res = _nft_result(args)
        results.append(res)
        critical.append(res)
    failures = [r for r in critical if r.get("code") not in (0,)]
    return failures, results


def _nft_result(args):
    rc, out, err = _relay_nft(args)
    return {"cmd": ["nft", *args], "code": rc, "stdout": out, "stderr": err}


def _apply_relay_iptables(src_ip: str, dst_ip: str, relay_port: int, target_port: int, mode: str):
    results = []
    if mode == "multiport":
        for i in range(0, len(RELAY_MULTI_PORTS), 15):
            ports_csv = ",".join(str(p) for p in RELAY_MULTI_PORTS[i:i + 15])
            cmd_groups = [
                ["iptables", "-t", "nat", "-A", "PREROUTING", "-d", src_ip, "-p", "udp", "-m", "multiport", "--dports", ports_csv, "-j", "DNAT", "--to-destination", dst_ip, "-m", "comment", "--comment", RELAY_RULE_TAG],
                ["iptables", "-t", "nat", "-A", "POSTROUTING", "-p", "udp", "-d", dst_ip, "-m", "multiport", "--dports", ports_csv, "-j", "MASQUERADE", "-m", "comment", "--comment", RELAY_RULE_TAG],
                ["iptables", "-A", "FORWARD", "-p", "udp", "-d", dst_ip, "-m", "multiport", "--dports", ports_csv, "-j", "ACCEPT", "-m", "comment", "--comment", RELAY_RULE_TAG],
                ["iptables", "-A", "FORWARD", "-p", "udp", "-s", dst_ip, "-m", "multiport", "--sports", ports_csv, "-j", "ACCEPT", "-m", "comment", "--comment", RELAY_RULE_TAG],
            ]
            for cmd in cmd_groups:
                rc, out, err = run_cmd(cmd)
                results.append({"cmd": cmd, "code": rc, "stdout": out, "stderr": err})
    else:
        cmd_groups = [
            ["iptables", "-t", "nat", "-A", "PREROUTING", "-d", src_ip, "-p", "udp", "--dport", str(relay_port), "-j", "DNAT", "--to-destination", f"{dst_ip}:{target_port}", "-m", "comment", "--comment", RELAY_RULE_TAG],
            ["iptables", "-t", "nat", "-A", "POSTROUTING", "-p", "udp", "-d", dst_ip, "--dport", str(target_port), "-j", "MASQUERADE", "-m", "comment", "--comment", RELAY_RULE_TAG],
            ["iptables", "-A", "FORWARD", "-p", "udp", "-d", dst_ip, "--dport", str(target_port), "-j", "ACCEPT", "-m", "comment", "--comment", RELAY_RULE_TAG],
            ["iptables", "-A", "FORWARD", "-p", "udp", "-s", dst_ip, "--sport", str(target_port), "-j", "ACCEPT", "-m", "comment", "--comment", RELAY_RULE_TAG],
        ]
        for cmd in cmd_groups:
            rc, out, err = run_cmd(cmd)
            results.append({"cmd": cmd, "code": rc, "stdout": out, "stderr": err})
    failures = [r for r in results if r.get("code") != 0]
    return failures, results


def relay_rules_status() -> dict:
    firewall = detect_relay_firewall()
    state = load_relay_state()
    if firewall == "nftables":
        code, out, err = _relay_nft(["list", "ruleset"])
        lines = [l.strip() for l in (out or "").splitlines() if RELAY_RULE_TAG in l or "wr_webui_relay_ports" in l]
        return {"firewall": firewall, "tag": RELAY_RULE_TAG, "state": state, "enabled": bool(lines), "rules": lines, "code": code, "stderr": err}
    if firewall == "iptables":
        lines = []
        for table_args in (["-t", "nat"], []):
            code, out, err = run_cmd(["iptables", *table_args, "-S"])
            if code == 0:
                lines.extend([l for l in (out or "").splitlines() if RELAY_RULE_TAG in l])
        return {"firewall": firewall, "tag": RELAY_RULE_TAG, "state": state, "enabled": bool(lines), "rules": lines}
    return {"firewall": None, "tag": RELAY_RULE_TAG, "state": state, "enabled": False, "rules": [], "error": "nftables or iptables not found"}


def apply_relay_config(body: dict):
    try:
        mode = (body.get("mode") or "single").strip()
        if mode not in ("single", "multiport"):
            return 400, {"error": "mode must be single or multiport"}
        endpoint_info = resolve_relay_endpoint(body.get("endpoint") or RELAY_DEFAULT_ENDPOINT)
        src_ip = _valid_ipv4(body.get("sourceIp") or get_public_ipv4())
        relay_port = _valid_port(body.get("relayPort") or 4500)
        target_port = _valid_port(body.get("targetPort") or relay_port)
    except Exception as e:
        return 400, {"error": str(e)}

    firewall = detect_relay_firewall()
    if not firewall:
        return 500, {"error": "nftables or iptables not found"}

    clean = clean_relay_rules(firewall)
    fwd_code, fwd_out, fwd_err = enable_relay_forwarding()
    if firewall == "nftables":
        failures, results = _apply_relay_nft(src_ip, endpoint_info["ip"], relay_port, target_port, mode)
    else:
        failures, results = _apply_relay_iptables(src_ip, endpoint_info["ip"], relay_port, target_port, mode)
    save = _save_relay_firewall(firewall)
    state = save_relay_state({
        "enabled": not failures,
        "mode": mode,
        "endpoint": endpoint_info["endpoint"],
        "endpointIp": endpoint_info["ip"],
        "endpointSource": endpoint_info["source"],
        "sourceIp": src_ip,
        "relayPort": relay_port,
        "targetPort": target_port,
        "ports": RELAY_MULTI_PORTS if mode == "multiport" else [relay_port],
        "tag": RELAY_RULE_TAG,
    })
    payload = {
        "ok": not failures,
        "firewall": firewall,
        "clean": clean,
        "forwarding": {"code": fwd_code, "stdout": fwd_out, "stderr": fwd_err},
        "save": save,
        "state": state,
        "failures": failures[:8],
        "result_count": len(results),
        "status": relay_rules_status(),
    }
    log_event("info" if not failures else "error", "relay_apply", firewall=firewall, state=state, failures=len(failures))
    return (200 if not failures else 500), payload


def remove_relay_config():
    firewall = detect_relay_firewall()
    clean = clean_relay_rules(firewall)
    save = _save_relay_firewall(firewall) if firewall else {"code": 0}
    state = load_relay_state()
    state["enabled"] = False
    state = save_relay_state(state)
    log_event("info", "relay_removed", firewall=firewall)
    return 200, {"ok": True, "firewall": firewall, "clean": clean, "save": save, "state": state, "status": relay_rules_status()}


def read_json_body(handler, max_bytes=16384):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length > max_bytes:
        raise ValueError("body too large")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))



INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>WARP Web UI</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; background:#0b1020; color:#e7e9ee; }
    h2,h3 { margin: 0 0 10px 0; }
    .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    input, textarea, select { padding:8px 10px; border-radius:8px; border:1px solid #2a355a; background:#0b1020; color:#e7e9ee; min-width: 220px; }
    textarea { width: 100%; min-height: 110px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
    button { padding:10px 14px; border-radius:10px; border:1px solid #2a355a; background:#121a34; color:#e7e9ee; cursor:pointer; }
    button:hover { background:#172145; }
    button.secondary { background:#1a2448; }
    button.danger { border-color:#7f1d1d; background:#3f1212; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .card { border:1px solid #2a355a; background:#0f1630; border-radius:14px; padding:14px; margin-top:14px; }
    .kv { display:grid; grid-template-columns: 170px 1fr; gap:8px 12px; }
    .k { color:#9aa7c7; }
    pre { white-space:pre-wrap; word-break:break-word; margin:0; }
    .muted { color:#9aa7c7; font-size: 12px; }
    .ok { color:#6ee7b7; }
    .bad { color:#fca5a5; }
    .msg { margin-top:8px; font-size: 13px; }
  </style>
</head>
<body>
  <h2>WARP Web UI</h2>
  <div class="muted">Basic Auth. Auto-refresh every 4s.</div>

  <div class="card">
    <div class="row">
      <button id="btnConnect">Connect</button>
      <button id="btnDisconnect">Disconnect</button>
      <button id="btnRestart">Restart warp-svc</button>
      <button id="btnRefresh">Refresh</button>
      <span id="statusBadge" class="muted"></span>
    </div>
  </div>

  <div class="card">
    <h3>Account (WARP registration)</h3>
    <div class="kv">
      <div class="k">Account type</div><div id="rType">-</div>
      <div class="k">Account ID</div><div id="rAccountId">-</div>
      <div class="k">Device ID</div><div id="rDeviceId">-</div>
      <div class="k">License</div><div id="rLicense">-</div>
    </div>
    <div class="row" style="margin-top:12px">
      <input id="licenseKey" placeholder="WARP+ license key" autocomplete="off"/>
      <button id="btnLicense" class="secondary">Apply license</button>
    </div>
    <div id="accountMsg" class="msg muted"></div>
  </div>

  <div class="card">
    <h3>WARP package</h3>
    <div class="row">
      <button id="btnInstall">Install WARP</button>
      <button id="btnUninstall" class="danger">Uninstall WARP</button>
    </div>
    <div id="installMsg" class="msg muted"></div>
  </div>

  <div class="card">
    <h3>SOCKS proxy port (warp-cli)</h3>
    <div class="kv">
      <div class="k">Current port</div><div id="proxyPort">-</div>
      <div class="k">Endpoint</div><div id="proxyEndpoint">-</div>
    </div>
    <div class="row" style="margin-top:12px">
      <input id="proxyPortInput" type="number" min="1" max="65535" placeholder="e.g. 40000"/>
      <button id="btnSetPort" class="secondary">Set port</button>
      <button id="btnPort40000" class="secondary">Use 40000</button>
    </div>
    <div id="proxyMsg" class="msg muted"></div>
  </div>

  <div class="card">
    <h3>WARP Relay (beta)</h3>
    <div class="kv">
      <div class="k">Firewall</div><div id="relayFirewall">-</div>
      <div class="k">Active</div><div id="relayEnabled">-</div>
      <div class="k">Endpoint IP</div><div id="relayEndpointIp">-</div>
      <div class="k">Rules</div><div id="relayRuleCount">-</div>
    </div>
    <div class="row" style="margin-top:12px">
      <input id="relayEndpoint" placeholder="engage.cloudflareclient.com or IPv4"/>
      <input id="relaySourceIp" placeholder="public IPv4 (auto if empty)"/>
      <select id="relayMode">
        <option value="single">single UDP port</option>
        <option value="multiport">Cloudflare WARP multiport</option>
      </select>
      <input id="relayPort" type="number" min="1" max="65535" placeholder="relay port"/>
      <input id="relayTargetPort" type="number" min="1" max="65535" placeholder="target port"/>
    </div>
    <div class="row" style="margin-top:12px">
      <button id="btnRelayApply" class="secondary">Apply relay</button>
      <button id="btnRelayRemove" class="danger">Remove relay rules</button>
    </div>
    <div id="relayMsg" class="msg muted"></div>
    <pre id="relayRules" style="margin-top:10px"></pre>
  </div>

  <div class="card">
    <h3>3x-ui / Xray</h3>
    <p class="muted">Adds outbound tag <code>warp-socks</code> → 127.0.0.1:PORT and routing rule <code>geosite:google</code>. Backs up config, restarts x-ui.</p>
    <button id="btnXui" class="secondary">Apply preset to x-ui</button>
    <div id="xuiMsg" class="msg muted"></div>
  </div>

  <div class="card">
    <h3>Amnezia Xray (Docker)</h3>
    <p class="muted">Мост <code>172.17.0.1:11025</code> → SOCKS на хосте. В маршрутизации Xray по-прежнему UUID/email; в UI — понятные имена (алиасы: <code>/etc/warp-webui/client-aliases.json</code>).</p>
    <button id="btnAmnezia" class="secondary">Применить preset к Amnezia (все клиенты, geosite:google)</button>
    <div id="amneziaMsg" class="msg muted"></div>
    <h4 style="margin-top:16px">WARP только для выбранных клиентов</h4>
    <p class="muted">Отметьте клиентов по имени. Ничего не выбрано → общее правило geosite. Имя можно сохранить — переживёт перезапуск UI.</p>
    <div id="amneziaClientList" class="muted">Loading clients...</div>
    <div class="row" style="margin-top:8px">
      <input id="amneziaDomains" placeholder="domains, comma-separated (default geosite:google)" style="flex:1"/>
    </div>
    <button id="btnAmneziaRouting" class="secondary" style="margin-top:8px">Применить WARP-маршрутизацию для выбранных</button>
    <div id="amneziaRoutingMsg" class="msg muted"></div>
  </div>

  <div class="card">
    <h3>Client presets (JSON)</h3>
    <textarea id="presetsJson" readonly></textarea>
    <button id="btnCopyPresets" class="secondary" style="margin-top:8px">Copy JSON</button>
  </div>

  <div class="card">
    <h3>Status</h3>
    <div class="kv">
      <div class="k">Connected</div><div id="sConnected">-</div>
      <div class="k">Health</div><div id="sHealth">-</div>
      <div class="k">Account</div><div id="sAccount">-</div>
      <div class="k">Device</div><div id="sDevice">-</div>
      <div class="k">Last stderr</div><div><pre id="sStderr"></pre></div>
      <div class="k">Last stdout</div><div><pre id="sStdout"></pre></div>
    </div>
  </div>

  <div class="card">
    <h3>Logs (recent)</h3>
    <div class="muted" id="logMeta"></div>
    <pre id="logText" style="margin-top:10px"></pre>
  </div>

<script>
const el = (id) => document.getElementById(id);
function badge(text, ok=null) {
  const b = el('statusBadge');
  b.textContent = text;
  b.className = ok === true ? 'ok' : ok === false ? 'bad' : 'muted';
}
function setText(id, v) {
  el(id).textContent = (v === null || v === undefined || v === '') ? '-' : String(v);
}
function setMsg(id, text, ok=null) {
  const n = el(id);
  n.textContent = text || '';
  n.className = 'msg ' + (ok === true ? 'ok' : ok === false ? 'bad' : 'muted');
}
async function api(path, opts={}) {
  const url = new URL(path, window.location.origin);
  const res = await fetch(url, { cache: 'no-store', ...opts });
  const ct = res.headers.get('content-type') || '';
  const body = ct.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) throw new Error(typeof body === 'string' ? body : JSON.stringify(body));
  return body;
}
async function apiPost(path, payload) {
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) });
}
async function refreshStatus() {
  try {
    const data = await api('/status');
    setText('sConnected', data.connected);
    setText('sHealth', data.health);
    setText('sAccount', data.account);
    setText('sDevice', data.device);
    setText('sStdout', (data.stdout || '').slice(-4000));
    setText('sStderr', (data.stderr || '').slice(-4000));
    if (data.connected === true) badge('CONNECTED', true);
    else if (data.connected === false) badge('DISCONNECTED', false);
    else badge('UNKNOWN');
    el('btnConnect').disabled = data.connected === true;
    el('btnDisconnect').disabled = data.connected === false;
  } catch (e) { badge('ERROR /status', false); }
}
async function refreshRegistration() {
  try {
    const r = await api('/registration');
    setText('rType', r.account_type);
    setText('rAccountId', r.account_id);
    setText('rDeviceId', r.device_id);
    setText('rLicense', r.license_masked);
  } catch (e) { setMsg('accountMsg', 'Failed to load registration', false); }
}
async function refreshProxy() {
  try {
    const p = await api('/proxy');
    setText('proxyPort', p.port);
    setText('proxyEndpoint', p.port ? ('127.0.0.1:' + p.port) : '-');
    if (p.port && !el('proxyPortInput').value) el('proxyPortInput').value = p.port;
  } catch (e) { setMsg('proxyMsg', 'Failed to load proxy port', false); }
}
async function refreshPresets() {
  try {
    const p = await api('/presets');
    el('presetsJson').value = JSON.stringify(p, null, 2);
  } catch (e) { el('presetsJson').value = 'Failed to load presets'; }
}
async function refreshRelay() {
  try {
    const r = await api('/relay');
    const st = r.state || {};
    setText('relayFirewall', r.firewall);
    setText('relayEnabled', r.enabled);
    setText('relayEndpointIp', st.endpointIp);
    setText('relayRuleCount', (r.rules || []).length);
    if (!el('relayEndpoint').value) el('relayEndpoint').value = st.endpoint || 'engage.cloudflareclient.com';
    if (!el('relaySourceIp').value) el('relaySourceIp').value = st.sourceIp || '';
    if (!el('relayPort').value) el('relayPort').value = st.relayPort || 4500;
    if (!el('relayTargetPort').value) el('relayTargetPort').value = st.targetPort || 4500;
    el('relayMode').value = st.mode || 'single';
    el('relayRules').textContent = (r.rules || []).join('\n');
  } catch (e) { setMsg('relayMsg', 'Failed to load relay status: ' + (e.message || e), false); }
}
async function refreshLogs() {
  try {
    const data = await api('/logs');
    const lines = (data.entries || []).map(e => {
      const ts = e.ts || '';
      const lvl = e.level || '';
      const msg = e.message || '';
      const action = e.action ? ` action=${e.action}` : '';
      const rc = (e.returncode !== undefined) ? ` rc=${e.returncode}` : '';
      return `${ts} ${lvl} ${msg}${action}${rc}`;
    });
    el('logMeta').textContent = `entries: ${lines.length}`;
    el('logText').textContent = lines.join('\n');
  } catch (e) { el('logMeta').textContent = 'failed to load logs'; }
}
async function refreshAll() {
  await Promise.all([refreshStatus(), refreshRegistration(), refreshProxy(), refreshPresets(), refreshRelay(), refreshLogs(), refreshAmneziaClients()]);
}
async function doAction(path) {
  badge('Working...', null);
  try { await api(path, { method: 'POST' }); badge('OK ' + path, true); }
  catch (e) { badge('FAIL ' + path, false); }
  await refreshAll();
}
el('btnConnect').onclick = () => doAction('/connect');
el('btnDisconnect').onclick = () => doAction('/disconnect');
el('btnRestart').onclick = () => doAction('/restart');
el('btnRefresh').onclick = () => refreshAll();
el('btnLicense').onclick = async () => {
  const key = el('licenseKey').value.trim();
  setMsg('accountMsg', 'Applying...', null);
  try {
    const r = await apiPost('/license', { key });
    setMsg('accountMsg', (r.stderr || r.stdout || 'License applied').slice(0, 500), true);
    el('licenseKey').value = '';
    await refreshRegistration();
  } catch (e) { setMsg('accountMsg', String(e.message || e), false); }
};
el('btnInstall').onclick = async () => {
  if (!confirm('Install Cloudflare WARP package on this server?')) return;
  setMsg('installMsg', 'Running install script...', null);
  try {
    const r = await apiPost('/warp-install', {});
    setMsg('installMsg', (r.stdout || r.stderr || 'done').slice(0, 800), r.result_code === 0);
    await refreshAll();
  } catch (e) { setMsg('installMsg', String(e.message || e), false); }
};
el('btnUninstall').onclick = async () => {
  if (!confirm('Uninstall cloudflare-warp package?')) return;
  setMsg('installMsg', 'Running uninstall...', null);
  try {
    const r = await apiPost('/warp-uninstall', {});
    setMsg('installMsg', (r.stdout || r.stderr || 'done').slice(0, 800), r.result_code === 0);
  } catch (e) { setMsg('installMsg', String(e.message || e), false); }
};
async function setPort(port) {
  setMsg('proxyMsg', 'Setting port ' + port + '...', null);
  try {
    const r = await apiPost('/proxy-port', { port });
    setMsg('proxyMsg', 'Port updated to ' + (r.proxy && r.proxy.port), true);
    await refreshProxy();
    await refreshPresets();
  } catch (e) { setMsg('proxyMsg', String(e.message || e), false); }
}
el('btnSetPort').onclick = () => {
  const p = parseInt(el('proxyPortInput').value, 10);
  if (!p) return setMsg('proxyMsg', 'Enter a valid port', false);
  setPort(p);
};
el('btnPort40000').onclick = () => { el('proxyPortInput').value = 40000; setPort(40000); };
el('btnRelayApply').onclick = async () => {
  if (!confirm('Apply WARP Relay firewall rules on this server?')) return;
  const payload = {
    endpoint: el('relayEndpoint').value.trim() || 'engage.cloudflareclient.com',
    sourceIp: el('relaySourceIp').value.trim(),
    mode: el('relayMode').value,
    relayPort: parseInt(el('relayPort').value || '4500', 10),
    targetPort: parseInt(el('relayTargetPort').value || el('relayPort').value || '4500', 10),
  };
  setMsg('relayMsg', 'Applying relay rules...', null);
  try {
    const r = await apiPost('/relay-apply', payload);
    const st = r.state || {};
    setMsg('relayMsg', 'OK: ' + st.sourceIp + ':' + st.relayPort + ' -> ' + st.endpointIp + ':' + st.targetPort, true);
    await refreshRelay();
  } catch (e) { setMsg('relayMsg', String(e.message || e), false); }
};
el('btnRelayRemove').onclick = async () => {
  if (!confirm('Remove only WARP Web UI relay rules?')) return;
  setMsg('relayMsg', 'Removing relay rules...', null);
  try {
    await apiPost('/relay-remove', {});
    setMsg('relayMsg', 'Relay rules removed', true);
    await refreshRelay();
  } catch (e) { setMsg('relayMsg', String(e.message || e), false); }
};
el('btnXui').onclick = async () => {
  setMsg('xuiMsg', 'Applying x-ui preset...', null);
  try {
    const r = await apiPost('/xui-preset', {});
    setMsg('xuiMsg', 'Backup: ' + (r.backup || '-') + '; restart rc=' + r.restart_code, r.restart_code === 0);
  } catch (e) { setMsg('xuiMsg', String(e.message || e), false); }
};

async function refreshAmneziaClients() {
  const box = el('amneziaClientList');
  try {
    const data = await api('/amnezia-clients');
    const clients = data.clients || [];
    if (!clients.length) {
      box.innerHTML = '<span class="muted">Нет VLESS-клиентов в server.json</span>';
      return;
    }
    box.innerHTML = clients.map((c) => {
      const routing = c.routingUser || c.email || c.uuid || c.id;
      const name = c.displayName || routing;
      const short = c.shortUuid || routing;
      const src = c.source === 'alias' ? 'алиас' : (c.source === 'email' ? 'email' : (c.source === 'comment' ? 'comment' : 'uuid'));
      const escRoute = String(routing).replace(/"/g, '&quot;');
      const escUuid = String(c.uuid || c.id).replace(/"/g, '&quot;');
      const escName = String(name).replace(/"/g, '&quot;');
      return '<div style="margin:8px 0;padding:6px 0;border-bottom:1px solid #2a2a2a">' +
        '<label><input type="checkbox" class="amz-client" value="' + escRoute + '"/> ' +
        '<strong>' + escName + '</strong> <span class="muted">(' + short + ', ' + src + ')</span></label>' +
        '<div class="row" style="margin-top:4px;gap:6px">' +
        '<input class="amz-alias" data-uuid="' + escUuid + '" value="' + escName + '" style="flex:1" placeholder="Имя в интерфейсе"/>' +
        '<button type="button" class="secondary amz-save-alias" data-uuid="' + escUuid + '">Сохранить имя</button>' +
        '</div></div>';
    }).join('');
    box.querySelectorAll('.amz-save-alias').forEach((btn) => {
      btn.onclick = async () => {
        const uuid = btn.getAttribute('data-uuid');
        const inp = box.querySelector('.amz-alias[data-uuid="' + uuid + '"]');
        const displayName = inp ? inp.value.trim() : '';
        if (!displayName) return setMsg('amneziaRoutingMsg', 'Введите имя', false);
        try {
          await apiPost('/amnezia-client-alias', { uuid, displayName });
          setMsg('amneziaRoutingMsg', 'Имя сохранено', true);
          await refreshAmneziaClients();
        } catch (e) { setMsg('amneziaRoutingMsg', String(e.message || e), false); }
      };
    });
  } catch (e) {
    box.textContent = 'Не удалось загрузить клиентов: ' + (e.message || e);
  }
}
el('btnAmnezia').onclick = async () => {
  setMsg('amneziaMsg', 'Применяю preset Amnezia...', null);
  try {
    const r = await apiPost('/amnezia-preset', {});
    const names = (r.clients_display || []).join(', ');
    setMsg('amneziaMsg', (names ? ('Клиенты: ' + names + '. ') : '') + 'restart rc=' + r.restart_code, r.restart_code === 0);
  } catch (e) { setMsg('amneziaMsg', String(e.message || e), false); }
};

el('btnAmneziaRouting').onclick = async () => {
  const users = Array.from(document.querySelectorAll('.amz-client:checked')).map((n) => n.value);
  const domRaw = (el('amneziaDomains').value || '').trim();
  const domains = domRaw ? domRaw.split(',').map((s) => s.trim()).filter(Boolean) : ['geosite:google'];
  setMsg('amneziaRoutingMsg', users.length ? ('Применяю для ' + users.length + ' клиент(ов)...') : 'Применяю общее правило geosite...', null);
  try {
    const r = await apiPost('/amnezia-routing', { users, domains });
    const who = (r.users_display && r.users_display.length) ? r.users_display.join(', ') : 'все (geosite)';
    setMsg('amneziaRoutingMsg', 'OK: ' + who + '; rules=' + ((r.routing_rules && r.routing_rules.length) || 0), r.ok);
    await refreshAmneziaClients();
  } catch (e) { setMsg('amneziaRoutingMsg', String(e.message || e), false); }
};

el('btnCopyPresets').onclick = async () => {
  try { await navigator.clipboard.writeText(el('presetsJson').value); setMsg('accountMsg', 'Presets copied', true); }
  catch (e) { setMsg('accountMsg', 'Copy failed', false); }
};
refreshAll();
setInterval(refreshAll, 4000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        token = header.split(" ", 1)[1].strip()
        creds = USER + ":" + PASS
        expected = base64.b64encode(creds.encode()).decode()
        return token == expected

    def _unauthorized(self, content_type="application/json"):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="warp-webui"')
        self.send_header("Content-Type", content_type)
        self.end_headers()
        if content_type == "application/json":
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
        else:
            self.wfile.write(b"unauthorized")

    def _json(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=True).encode())

    def _html(self, code, html: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_GET(self):
        if not self._auth_ok():
            return self._unauthorized(content_type="text/plain")
        if self.path == "/":
            log_event("info", "ui_loaded", client=self.client_address[0])
            return self._html(200, INDEX_HTML)
        if self.path == "/status":
            return self._json(200, warp_status())
        if self.path == "/registration":
            return self._json(200, warp_registration())
        if self.path == "/proxy":
            return self._json(200, get_proxy_port())
        if self.path == "/relay":
            return self._json(200, relay_rules_status())
        if self.path == "/amnezia-clients":
            clients, err = list_amnezia_clients()
            if clients is None:
                return self._json(500, {"error": "read failed", "detail": err})
            warp_rules = []
            cfg, _ = _read_amnezia_config()
            if cfg:
                warp_rules = [r for r in cfg.get("routing", {}).get("rules", []) if r.get("outboundTag") == SOCKS_OUTBOUND_TAG]
            return self._json(200, {
                "clients": clients,
                "warp_rules": warp_rules,
                "aliases_path": CLIENT_ALIASES_PATH,
            })
        if self.path == "/presets":
            p = get_proxy_port().get("port") or 1024
            return self._json(200, client_presets(int(p)))
        if self.path == "/logs":
            return self._json(200, {"entries": list(LOG_BUFFER)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            return self._unauthorized()
        log_event("info", "action_requested", action=self.path, client=self.client_address[0])

        action_map = {
            "/connect": ["warp-cli", "--accept-tos", "connect"],
            "/disconnect": ["warp-cli", "--accept-tos", "disconnect"],
            "/restart": ["systemctl", "restart", "warp-svc"],
        }
        if self.path in action_map:
            code, out, err = run_cmd(action_map[self.path])
            if self.path == "/restart":
                time.sleep(1.0)
            payload = {
                "action": self.path,
                "result_code": code,
                "stdout": out,
                "stderr": err,
                "status": warp_status(),
            }
            return self._json(200 if code == 0 else 500, payload)

        try:
            body = read_json_body(self)
        except Exception as e:
            return self._json(400, {"error": str(e)})

        if self.path == "/license":
            code, payload = apply_license_key(body.get("key", ""))
            return self._json(code, payload)

        if self.path == "/proxy-port":
            try:
                port = int(body.get("port"))
            except (TypeError, ValueError):
                return self._json(400, {"error": "port required"})
            code, payload = set_proxy_port(port)
            return self._json(code, payload)

        if self.path == "/warp-install":
            port = int(os.environ.get("WARP_PROXY_PORT") or 0) or get_proxy_port().get("port") or 1024
            code, payload = run_script(INSTALL_SCRIPT, {"WARP_PROXY_PORT": str(port)})
            payload["status"] = warp_status()
            return self._json(code, payload)

        if self.path == "/warp-uninstall":
            code, payload = run_script(UNINSTALL_SCRIPT)
            return self._json(code, payload)

        if self.path == "/relay-apply":
            code, payload = apply_relay_config(body)
            return self._json(code, payload)

        if self.path == "/relay-remove":
            code, payload = remove_relay_config()
            return self._json(code, payload)

        if self.path == "/xui-preset":
            port = get_proxy_port().get("port") or 1024
            code, payload = apply_xui_preset(int(port))
            return self._json(code, payload)

        if self.path == "/amnezia-routing":
            port = get_proxy_port().get("port") or 1024
            users = body.get("users")
            if users is not None and not isinstance(users, list):
                users = [users]
            domains = body.get("domains")
            if domains is not None and not isinstance(domains, list):
                domains = [domains]
            code, payload = apply_amnezia_routing(int(port), users=users, domains=domains)
            return self._json(code, payload)

        if self.path == "/amnezia-preset":
            port = get_proxy_port().get("port") or 1024
            code, payload = apply_amnezia_preset(int(port))
            return self._json(code, payload)

        if self.path == "/amnezia-client-alias":
            uuid_val = (body.get("uuid") or body.get("id") or "").strip()
            display = (body.get("displayName") or body.get("name") or "").strip()
            if not _is_uuid_like(uuid_val):
                return self._json(400, {"error": "valid uuid required"})
            if not display:
                return self._json(400, {"error": "displayName required"})
            aliases = load_client_aliases()
            aliases[uuid_val.lower()] = display
            saved = save_client_aliases(aliases)
            log_event("info", "client_alias_saved", uuid=uuid_val, displayName=display)
            return self._json(200, {"uuid": uuid_val, "displayName": display, "aliases": saved})

        return self._json(404, {"error": "not found"})

    def log_message(self, _format, *args):
        return


if __name__ == "__main__":
    log_event("info", "service_start", host=HOST, port=PORT)
    HTTPServer((HOST, PORT), Handler).serve_forever()
