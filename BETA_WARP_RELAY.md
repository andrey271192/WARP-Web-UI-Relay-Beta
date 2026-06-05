# WARP Relay beta note

## What `openwarpkit/warp-relay` adds

`warp-relay` configures Linux firewall NAT/forwarding for UDP WireGuard/WARP traffic:

- relay server listens on public/local IP and UDP port;
- firewall DNAT sends traffic to chosen WireGuard/WARP endpoint IP/port;
- MASQUERADE hides client source behind relay server;
- optional multiport mode forwards known Cloudflare WARP UDP ports to same ports on target endpoint.

It does not change Cloudflare WARP exit IP directly. It changes path to WARP/WireGuard endpoint. Desired "connection point" in beta means selecting target endpoint hostname/IP and port from Web UI, then applying relay firewall rules.

## Beta implementation

Added built-in relay manager to `app.py`:

- `GET /relay` shows firewall type, saved relay state, active managed rules;
- `POST /relay-apply` applies relay rules for `single` or `multiport` mode;
- `POST /relay-remove` removes only rules tagged `WR_WEBUI_RELAY`;
- UI card "WARP Relay (beta)" controls endpoint, source IP, mode, relay port, target port.

State file: `/etc/warp-webui/relay-state.json`

Rule tag: `WR_WEBUI_RELAY`

Firewall support:

- preferred: `nftables`;
- fallback: `iptables`;
- no automatic package install from UI in beta.

## Safety choices

- Does not run `wr.sh` directly.
- Does not delete upstream `WR_RULE` rules.
- Does not disable `net.ipv4.ip_forward` on remove, because other VPN/NAT services can depend on it.
- Cleans only own tag before applying new rules.
- Saves nftables to `/etc/nftables.conf`; for iptables persists via `netfilter-persistent` if installed.

## Beta testing path

1. Install beta on clean VPS or non-critical host.
2. Open WARP Web UI.
3. Use relay endpoint `engage.cloudflareclient.com`, mode `single`, port `4500`.
4. Test from client WireGuard/WARP config pointing to `SERVER_IP:4500`.
5. Try multiport only after single-port test works.
6. For custom "point", enter target IPv4 or hostname manually.

## Known limits

- Cloudflare WARP public exit IP/region is still controlled by Cloudflare.
- Endpoint DNS can resolve to different IPs over time; re-apply rules after changing target.
- Multiport mode maps relay port X to target port X only.
- UI does not yet benchmark/list candidate WARP endpoints by latency/country.

## Next beta features

- Endpoint library with labels such as "Cloudflare default", "custom IP", "last known fast IP".
- Latency probe for candidate endpoints.
- Client config generator replacing endpoint with relay host/port.
- Read-only dry-run preview before apply.
- Separate beta GitHub repo or `beta/warp-relay` branch once GitHub auth/token is available.
