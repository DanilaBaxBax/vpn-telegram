#!/usr/bin/env bash
# WireGuard server bootstrap + user management (Debian/Ubuntu, systemd)
# Usage:
#   ./vpn_setup.sh install [--iface wg0] [--port 51820] [--subnet 10.8.0.0/24] [--dns 1.1.1.1,9.9.9.9]
#   ./vpn_setup.sh add <username> [--ip 10.8.0.X] [--qr] [--ipv6]
#   ./vpn_setup.sh revoke <username>
#   ./vpn_setup.sh list
#   ./vpn_setup.sh show <username> [--qr]
#   ./vpn_setup.sh export <username> [--path /root]
#   ./vpn_setup.sh status | restart

set -Eeuo pipefail
shopt -s nullglob

WG_DIR="/etc/wireguard"
KEYS_DIR="$WG_DIR/keys"
CLIENTS_DIR="$WG_DIR/clients"
DEFAULT_IFACE="wg0"
DEFAULT_PORT=51820
DEFAULT_SUBNET="10.8.0.0/24"
DEFAULT_DNS="1.1.1.1,9.9.9.9"

require_root(){ if [[ ${EUID:-$(id -u)} -ne 0 ]]; then echo "[ERR] Run as root"; exit 1; fi; }
cmd_exists(){ command -v "$1" >/dev/null 2>&1; }
log(){  echo -e "[\e[32mOK\e[0m] $*"; }
warn(){ echo -e "[\e[33mWARN\e[0m] $*"; }
err(){  echo -e "[\e[31mERR\e[0m] $*"; exit 1; }

# ---------- PARSERS ----------
parse_args_install(){ IFACE="$DEFAULT_IFACE"; PORT="$DEFAULT_PORT"; SUBNET="$DEFAULT_SUBNET"; DNS="$DEFAULT_DNS";
  while [[ $# -gt 0 ]]; do case "$1" in
    --iface) IFACE="$2"; shift 2;; --port) PORT="$2"; shift 2;;
    --subnet) SUBNET="$2"; shift 2;; --dns) DNS="$2"; shift 2;;
    *) err "Unknown arg for install: $1";; esac; done; }

parse_args_add(){
  [[ $# -lt 1 ]] && err "Usage: add <username> [--ip 10.8.0.X] [--qr] [--ipv6]"
  USERNAME="$1"; shift; WANT_IP=""; ADD_QR=0; WANT_IPV6=0
  while [[ $# -gt 0 ]]; do case "$1" in
    --ip)   WANT_IP="$2"; shift 2;;
    --qr)   ADD_QR=1; shift;;
    --ipv6) WANT_IPV6=1; shift;;
    *) err "Unknown arg for add: $1";; esac; done
}

parse_args_export(){ [[ $# -lt 1 ]] && err "Usage: export <username> [--path /root]";
  USERNAME="$1"; shift; OUTDIR="/root";
  while [[ $# -gt 0 ]]; do case "$1" in
    --path) OUTDIR="$2"; shift 2;;
    *) err "Unknown arg for export: $1";; esac; done; }

# ---------- HELPERS ----------
ensure_deps(){ if cmd_exists apt-get; then
  export DEBIAN_FRONTEND=noninteractive; apt-get update -y;
  apt-get install -y wireguard wireguard-tools iproute2 qrencode iptables iptables-persistent;
else err "Unsupported distro. Use Debian/Ubuntu with apt-get."; fi; }

wan_iface(){ ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}'; }
wan_ip(){    ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}'; }

ensure_sysctl(){ echo "net.ipv4.ip_forward=1" >/etc/sysctl.d/99-wireguard-forward.conf; sysctl --system >/dev/null; }
mk_dirs(){ mkdir -p "$WG_DIR" "$KEYS_DIR" "$CLIENTS_DIR"; chmod 700 "$WG_DIR" "$KEYS_DIR"; }
ensure_server_keys(){ local priv="$KEYS_DIR/server-$IFACE.key" pub="$KEYS_DIR/server-$IFACE.pub";
  [[ -f "$priv" ]] || { umask 077; wg genkey >"$priv"; }; chmod 600 "$priv"; wg pubkey <"$priv" >"$pub"; }
subnet_base(){ awk -F'[./]' '{printf "%s.%s.%s",$1,$2,$3}' <<<"$1"; }
next_client_ip(){ local base last ip; base=$(subnet_base "$SUBNET");
  for last in {2..254}; do ip="$base.$last";
    if ! grep -Rqs "AllowedIPs *= *$ip/32" "$WG_DIR/$IFACE.conf" "$CLIENTS_DIR"/*/peer.conf 2>/dev/null; then
      echo "$ip"; return; fi; done; err "No free IPs left in $SUBNET"; }
detect_iface(){ local f; f=$(ls -1 "$WG_DIR"/*.conf 2>/dev/null | head -n1 || true);
  [[ -n "$f" ]] && { f=$(basename "$f"); echo "${f%.conf}"; } || echo "$DEFAULT_IFACE"; }
detect_port(){   local i; i="$(detect_iface)"; grep -E '^ListenPort' -m1 "$WG_DIR/$i.conf" 2>/dev/null | awk -F'= *' '{print $2}' || echo "$DEFAULT_PORT"; }
detect_subnet(){ local i; i="$(detect_iface)"; awk -F'= *' '/^Address/{print $2}' "$WG_DIR/$i.conf" 2>/dev/null | cut -d/ -f1 | awk -F'.' '{printf "%s.%s.%s.0/24",$1,$2,$3}' || echo "$DEFAULT_SUBNET"; }
detect_dns(){    local f="$WG_DIR/$(detect_iface).dns"; [[ -f "$f" ]] && cat "$f" || echo "$DEFAULT_DNS"; }

preflight_server_ready(){
  local IFACE_LOCAL; IFACE_LOCAL="$(detect_iface)"; IFACE_LOCAL="${IFACE_LOCAL%.conf}"
  [[ -z "$IFACE_LOCAL" ]] && err "Не найден конфиг WireGuard. Сначала запусти: $0 install"
  IFACE="$IFACE_LOCAL"
  [[ -f "$KEYS_DIR/server-$IFACE.key" && -f "$KEYS_DIR/server-$IFACE.pub" ]] || { warn "Ключи сервера не найдены – создаю..."; ensure_server_keys; }
  if ! ip link show "$IFACE" >/dev/null 2>&1; then
    warn "Интерфейс $IFACE не запущен – пытаюсь запустить wg-quick@$IFACE"; systemctl start wg-quick@"$IFACE" || true; fi
  ip link show "$IFACE" >/dev/null 2>&1 || err "Интерфейс $IFACE не существует или не запустился. Проверь: systemctl status wg-quick@$IFACE"
}

sanitize_conf_file(){ # strip BOM/CR and weird spaces inplace
  local f="$1"
  # strip BOM
  perl -i -pe 'BEGIN{binmode(STDIN);binmode(STDOUT)} s/^\xEF\xBB\xBF//' "$f" 2>/dev/null || true
  # strip CR
  sed -i 's/\r$//' "$f" 2>/dev/null || true
  # normalize spaces around comma in AllowedIPs
  sed -i -E 's/^(AllowedIPs = 0\\.0\\.0\\.0\\/0),\\s*::\\/0/\\1,::\\/0/' "$f" 2>/dev/null || true
}

# ---------- COMMANDS ----------
install_server(){
  parse_args_install "$@"; require_root; ensure_deps; mk_dirs; ensure_sysctl
  local WANIF="$(wan_iface)"; [[ -z "$WANIF" ]] && err "Cannot detect WAN interface"
  local WANIP="$(wan_ip)";  [[ -z "$WANIP" ]] && WANIP="$(ip -4 addr show dev "$WANIF" | awk '/inet /{print $2}' | cut -d/ -f1 | head -n1)"
  ensure_server_keys; local PRIV="$KEYS_DIR/server-$IFACE.key"
  if [[ ! -f "$WG_DIR/$IFACE.conf" ]]; then
    cat >"$WG_DIR/$IFACE.conf" <<CFG
[Interface]
Address = $(subnet_base "$SUBNET").1/24
ListenPort = $PORT
PrivateKey = $(cat "$PRIV")
SaveConfig = true
PostUp = iptables -t nat -A POSTROUTING -o $WANIF -j MASQUERADE; iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o $WANIF -j MASQUERADE; iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT
CFG
  else warn "$WG_DIR/$IFACE.conf already exists. Skipping creation."; fi
  if cmd_exists ufw && ufw status >/dev/null 2>&1; then ufw allow "$PORT/udp" || true; fi
  systemctl enable --now wg-quick@"$IFACE"
  log "WireGuard up on $IFACE. Endpoint: ${WANIP:-<your-public-ip>}:$PORT"
  log "Subnet: $SUBNET | DNS for clients: $DNS"; echo "$DNS" >"$WG_DIR/$IFACE.dns"
}

add_client(){
  parse_args_add "$@"; require_root; preflight_server_ready
  # validator
  lint_conf(){ local f="$1"; wg-quick strip "$f" >/dev/null 2>&1 || err "Синтаксическая ошибка в сгенерированном конфиге ($f)."; }

  local IFACE="$(detect_iface)"; local SUBNET="$(detect_subnet)"; local DNS="$(detect_dns)"
  local peer_dir="$CLIENTS_DIR/$USERNAME"

  # idempotent reuse
  if [[ -d "$peer_dir" ]]; then
    warn "Client exists: $USERNAME — reusing existing config"
    if [[ -f "$peer_dir/$USERNAME.conf" ]]; then
      sanitize_conf_file "$peer_dir/$USERNAME.conf"
      if cmd_exists qrencode && [[ ! -f "$peer_dir/qr.png" ]]; then qrencode -t PNG -o "$peer_dir/qr.png" <"$peer_dir/$USERNAME.conf" || true; fi
      log "Config: $peer_dir/$USERNAME.conf"
      (( ADD_QR )) && cmd_exists qrencode && qrencode -t ANSIUTF8 <"$peer_dir/$USERNAME.conf"
      return 0
    else err "Client dir exists but config missing: $peer_dir/$USERNAME.conf"; fi
  fi

  mkdir -p "$peer_dir"; chmod 700 "$peer_dir"; umask 077
  local cpriv="$peer_dir/private.key" cpub="$peer_dir/public.key" psk="$peer_dir/psk.key"
  wg genkey >"$cpriv"; wg pubkey <"$cpriv" >"$cpub"; wg genpsk >"$psk"

  local ip; if [[ -n "${WANT_IP:-}" ]]; then ip="$WANT_IP"; else ip="$(next_client_ip)"; fi
  local sport endpoint; sport="$(detect_port)"; endpoint="$(wan_ip):$sport"
  local spub="$KEYS_DIR/server-$IFACE.pub"; [[ -f "$spub" ]] || err "Отсутствует публичный ключ сервера: $spub. Запусти: $0 install"

  local client_priv server_pub psk_val dns_first
  client_priv=$(tr -d '\n' < "$cpriv"); server_pub=$(tr -d '\n' < "$spub"); psk_val=$(tr -d '\n' < "$psk")
  dns_first=$(echo "$DNS" | awk -F',' '{gsub(/ /,"",$1); print $1}')

  local allowed="0.0.0.0/0"; (( WANT_IPV6 )) && allowed="0.0.0.0/0, ::/0"

  cat >"$peer_dir/$USERNAME.conf" <<CONF
[Interface]
PrivateKey = $client_priv
Address = $ip/32
DNS = $dns_first

[Peer]
PublicKey = $server_pub
PresharedKey = $psk_val
AllowedIPs = $allowed
Endpoint = $endpoint
PersistentKeepalive = 25
CONF

  sanitize_conf_file "$peer_dir/$USERNAME.conf"
  wg set "$IFACE" peer "$(cat "$cpub")" preshared-key "$psk" allowed-ips "$ip/32"
  wg-quick save "$IFACE" >/dev/null 2>&1
  lint_conf "$peer_dir/$USERNAME.conf"

  cat >"$peer_dir/peer.conf" <<PEER
# $USERNAME
[Peer]
PublicKey = $(cat "$cpub")
PresharedKey = $psk_val
AllowedIPs = $ip/32
PEER

  if cmd_exists qrencode; then
    qrencode -t PNG -o "$peer_dir/qr.png" <"$peer_dir/$USERNAME.conf" || true
    (( ADD_QR )) && qrencode -t ANSIUTF8 <"$peer_dir/$USERNAME.conf"
  fi

  log "Client $USERNAME added with IP $ip"
  log "Config: $peer_dir/$USERNAME.conf"
}

revoke_client(){
  require_root; [[ $# -ne 1 ]] && err "Usage: revoke <username>"; local USERNAME="$1"
  local IFACE="$(detect_iface)"; local peer_dir="$CLIENTS_DIR/$USERNAME"
  [[ ! -d "$peer_dir" ]] && err "No such client: $USERNAME"
  local cpub="$peer_dir/public.key"; [[ ! -f "$cpub" ]] && err "Missing $cpub"
  wg set "$IFACE" peer "$(cat "$cpub")" remove || warn "Peer already absent at runtime"
  local ip="$(awk -F'AllowedIPs *= *' '/AllowedIPs/{print $2; exit}' "$peer_dir/peer.conf" 2>/dev/null || true)"
  if [[ -n "$ip" ]]; then sed -i.bak "/$ip/d" "$WG_DIR/$IFACE.conf" || true; sed -i "/$(sed 's:[]\[^$.*/]:\\&:g' "$cpub")/d" "$WG_DIR/$IFACE.conf" || true; fi
  wg-quick save "$IFACE" >/dev/null 2>&1 || true
  log "Revoked $USERNAME. Files kept in $peer_dir (delete manually if desired)."
}

list_clients(){
  local IFACE="$(detect_iface)"
  echo "Username IP_Address PublicKey"
  for d in "$CLIENTS_DIR"/*; do
    [[ -d "$d" ]] || continue
    local name="$(basename "$d")"
    local ip="$(awk -F'AllowedIPs *= *' '/AllowedIPs/{print $2; exit}' "$d/peer.conf" 2>/dev/null)"
    local pub="$(cat "$d/public.key" 2>/dev/null | cut -c1-16)"
    printf "%s %s %s...\n" "$name" "${ip:-?}" "${pub:-}"
  done | column -t
  echo; echo "Active peers (wg show):"
  wg show "$IFACE" peers 2>/dev/null || true
}

show_client(){
  [[ $# -lt 1 ]] && err "Usage: show <username> [--qr]"; local USERNAME="$1"; shift; local SHOW_QR=0
  while [[ $# -gt 0 ]]; do case "$1" in --qr) SHOW_QR=1; shift;; *) err "Unknown arg: $1";; esac; done
  local conf="$CLIENTS_DIR/$USERNAME/$USERNAME.conf"; [[ -f "$conf" ]] || err "No such client or config: $USERNAME"
  sanitize_conf_file "$conf"
  echo "# --- $USERNAME.conf ---"
  sed -E 's/(PrivateKey = ).*/\1<hidden>/; s/(PresharedKey = ).*/\1<hidden>/' "$conf"
  echo
  if (( SHOW_QR )); then
    if cmd_exists qrencode; then qrencode -t ANSIUTF8 <"$conf"; else warn "qrencode is not installed"; fi
  fi
}

export_client(){ parse_args_export "$@"; local conf="$CLIENTS_DIR/$USERNAME/$USERNAME.conf"; [[ -f "$conf" ]] || err "No such client or config: $USERNAME"; sanitize_conf_file "$conf"; mkdir -p "$OUTDIR"; cp -f "$conf" "$OUTDIR/"; log "Exported to $OUTDIR/$(basename "$conf")"; }
status(){  local IFACE="$(detect_iface)"; systemctl status wg-quick@"$IFACE" --no-pager || true; wg show "$IFACE" || true; }
restart(){ local IFACE="$(detect_iface)"; systemctl restart wg-quick@"$IFACE"; log "Restarted $IFACE"; }

usage(){ cat <<USAGE
WireGuard server bootstrap + user management
Commands:
  install [--iface wg0] [--port 51820] [--subnet 10.8.0.0/24] [--dns 1.1.1.1,9.9.9.9]
  add <username> [--ip 10.8.0.X] [--qr] [--ipv6]
  revoke <username>
  list
  show <username> [--qr]
  export <username> [--path /root]
  status | restart
USAGE
}

main(){
  local cmd="${1:-}"; shift || true
  case "${cmd:-}" in
    install) install_server "$@" ;;
    add)     add_client "$@" ;;
    revoke)  revoke_client "$@" ;;
    list)    list_clients ;;
    show)    show_client "$@" ;;
    export)  export_client "$@" ;;
    status)  status ;;
    restart) restart ;;
    *) usage; exit 1;;
  esac
}
main "$@"
