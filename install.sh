#!/usr/bin/env bash
# install.sh — автоматическая установка WireGuard + Telegram ботов
# Под Debian/Ubuntu (apt + systemd). Запускать от root.

set -Eeuo pipefail
shopt -s nullglob

## -------- Log helpers --------
log()  { echo -e "[\e[32mOK\e[0m] $*"; }
warn() { echo -e "[\e[33mWARN\e[0m] $*"; }
err()  { echo -e "[\e[31mERR\e[0m] $*"; exit 1; }

## -------- Defaults --------
DO_SERVER=0
DO_ADMIN=0
DO_USER=0
START_SERVICES=1

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="/opt/vpn-bot/.venv"
VENV_PY=""

# Server params
IFACE="wg0"
PORT=51820
SUBNET="10.8.0.0/24"
DNS_SERVERS="1.1.1.1,9.9.9.9"

# Admin bot
ADMIN_BOT_TOKEN=""
ADMIN_IDS=""  # comma separated, optional

# User bot
USER_BOT_TOKEN=""
PAYMENT_PROVIDER_TOKEN=""
CURRENCY="RUB"
PAY_TEST_ZERO="1"

# Paths and files
SETUP_DST="/root/vpn_setup.sh"
ENV_DIR="/etc/vpn-telegram"
ADMIN_ENV_FILE="$ENV_DIR/admin-bot.env"
USER_ENV_FILE="$ENV_DIR/user-bot.env"
ADMIN_SERVICE="/etc/systemd/system/vpn-admin-bot.service"
USER_SERVICE="/etc/systemd/system/vpn-user-bot.service"
USER_DB_DIR="/var/lib/vpn-user-bot"

## -------- Usage --------
usage() {
  cat <<USAGE
Usage: sudo ./install.sh [OPTIONS]

Select components:
  --all                     Install server + admin-bot + user-bot
  --server                  Install WireGuard server via vpn_setup.sh
  --admin-bot               Install and configure admin Telegram bot
  --user-bot                Install and configure user Telegram bot

WireGuard options (when --server):
  --iface NAME              Interface name (default: wg0)
  --port N                  UDP port (default: 51820)
  --subnet CIDR             Clients subnet (default: 10.8.0.0/24)
  --dns LIST                DNS servers for clients, comma-separated

Admin bot options (when --admin-bot):
  --admin-bot-token TOKEN   Telegram bot token (required to start)
  --admin-ids CSV           Optional CSV of admin chat IDs

User bot options (when --user-bot):
  --user-bot-token TOKEN    Telegram bot token (required to start)
  --payment-provider-token TOKEN  Telegram Payments provider token (optional)
  --currency CODE           Payment currency (default: RUB)
  --pay-test-zero 0|1       Test invoices for 0 (default: 1)

Common options:
  --venv-dir PATH           Python venv path (default: /opt/vpn-bot/.venv)
  --repo-dir PATH           Path to this repo (default: autodetect)
  --no-start                Do not enable/start systemd services
  -h, --help                Show this help

Examples:
  # Full install with all components
  ./install.sh --all \
    --admin-bot-token AA:BB --admin-ids 123456789 \
    --user-bot-token CC:DD --pay-test-zero 1 \
    --port 51820 --subnet 10.8.0.0/24 --dns 1.1.1.1,9.9.9.9

  # Only server and admin bot
  ./install.sh --server --admin-bot --admin-bot-token AA:BB
USAGE
}

## -------- Arg parsing --------
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --all)        DO_SERVER=1; DO_ADMIN=1; DO_USER=1; shift;;
      --server)     DO_SERVER=1; shift;;
      --admin-bot)  DO_ADMIN=1; shift;;
      --user-bot)   DO_USER=1; shift;;

      --iface)      IFACE="${2:?}"; shift 2;;
      --port)       PORT="${2:?}"; shift 2;;
      --subnet)     SUBNET="${2:?}"; shift 2;;
      --dns)        DNS_SERVERS="${2:?}"; shift 2;;

      --admin-bot-token) ADMIN_BOT_TOKEN="${2:?}"; shift 2;;
      --admin-ids)       ADMIN_IDS="${2:?}"; shift 2;;

      --user-bot-token)       USER_BOT_TOKEN="${2:?}"; shift 2;;
      --payment-provider-token) PAYMENT_PROVIDER_TOKEN="${2:?}"; shift 2;;
      --currency)             CURRENCY="${2:?}"; shift 2;;
      --pay-test-zero)        PAY_TEST_ZERO="${2:?}"; shift 2;;

      --venv-dir)    VENV_DIR="${2:?}"; shift 2;;
      --repo-dir)    REPO_DIR="${2:?}"; shift 2;;
      --no-start)    START_SERVICES=0; shift;;
      -h|--help)     usage; exit 0;;
      *) err "Unknown option: $1";;
    esac
  done

  # If user didn't choose components, assume --all
  if [[ $DO_SERVER -eq 0 && $DO_ADMIN -eq 0 && $DO_USER -eq 0 ]]; then
    DO_SERVER=1; DO_ADMIN=1; DO_USER=1
  fi
}

## -------- Privileges --------
require_root() {
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    err "Run as root (sudo ./install.sh)"
  fi
}

## -------- System helpers --------
apt_install() {
  local pkg
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  for pkg in "$@"; do
    apt-get install -y "$pkg"
  done
}

ensure_wg_deps() {
  if ! command -v wg >/dev/null 2>&1; then
    log "Installing WireGuard dependencies"
    apt_install wireguard wireguard-tools iproute2 qrencode iptables iptables-persistent
  fi
}

ensure_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    log "Installing Python3"
    apt_install python3
  fi
  if ! python3 -m venv -h >/dev/null 2>&1; then
    log "Installing python3-venv and pip"
    apt_install python3-venv python3-pip
  fi
}

ensure_venv() {
  ensure_python
  if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
  fi
  VENV_PY="$VENV_DIR/bin/python"
  log "Upgrading pip and installing python-telegram-bot==20.7"
  "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
  "$VENV_DIR/bin/pip" install "python-telegram-bot==20.7" >/dev/null
}

ensure_dirs() {
  mkdir -p "$ENV_DIR" "$USER_DB_DIR"
  chmod 700 "$ENV_DIR"
}

## -------- Server install --------
install_server() {
  ensure_wg_deps

  # Deploy setup script to /root/vpn_setup.sh
  if [[ -f "$SETUP_DST" ]]; then
    log "$SETUP_DST already exists — updating from repo"
  else
    log "Installing setup script to $SETUP_DST"
  fi
  install -m 0755 "$REPO_DIR/server-part/vpn_conf.sh" "$SETUP_DST"

  # Run installation
  log "Configuring WireGuard: iface=$IFACE port=$PORT subnet=$SUBNET dns=$DNS_SERVERS"
  "$SETUP_DST" install --iface "$IFACE" --port "$PORT" --subnet "$SUBNET" --dns "$DNS_SERVERS"
}

## -------- Admin bot --------
write_admin_env() {
  cat >"$ADMIN_ENV_FILE" <<ENV
BOT_TOKEN=$ADMIN_BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
BASH=/bin/bash
ENV
  chmod 600 "$ADMIN_ENV_FILE"
  log "Wrote $ADMIN_ENV_FILE"
}

install_admin_bot() {
  ensure_venv
  ensure_dirs
  write_admin_env

  cat >"$ADMIN_SERVICE" <<UNIT
[Unit]
Description=VPN Admin Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=$REPO_DIR
EnvironmentFile=$ADMIN_ENV_FILE
ExecStart=$VENV_DIR/bin/python $REPO_DIR/admin-bot/vpn_bot_admin.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  log "Wrote $ADMIN_SERVICE"
}

## -------- User bot --------
write_user_env() {
  cat >"$USER_ENV_FILE" <<ENV
BOT_TOKEN=$USER_BOT_TOKEN
PAYMENT_PROVIDER_TOKEN=$PAYMENT_PROVIDER_TOKEN
CURRENCY=$CURRENCY
PAY_TEST_ZERO=$PAY_TEST_ZERO
ENV
  chmod 600 "$USER_ENV_FILE"
  log "Wrote $USER_ENV_FILE"
}

install_user_bot() {
  ensure_venv
  ensure_dirs
  write_user_env

  # Ensure DB dir exists
  mkdir -p "$USER_DB_DIR"
  chmod 700 "$USER_DB_DIR"

  cat >"$USER_SERVICE" <<UNIT
[Unit]
Description=VPN User Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
User=root
WorkingDirectory=$REPO_DIR
EnvironmentFile=$USER_ENV_FILE
ExecStart=$VENV_DIR/bin/python $REPO_DIR/user-bot/vpn_bot_user.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  log "Wrote $USER_SERVICE"
}

## -------- Enable/start --------
reload_systemd() { systemctl daemon-reload; }

enable_start_unit() {
  local unit="$1"
  systemctl enable --now "$unit"
  log "Enabled and started: $unit"
}

## -------- Main --------
main() {
  parse_args "$@"
  require_root

  if [[ $DO_SERVER -eq 1 ]]; then
    install_server
  fi

  if [[ $DO_ADMIN -eq 1 ]]; then
    if [[ -z "$ADMIN_BOT_TOKEN" ]]; then
      warn "--admin-bot selected but --admin-bot-token is empty. Service will be installed but not started."
    fi
    install_admin_bot
  fi

  if [[ $DO_USER -eq 1 ]]; then
    if [[ -z "$USER_BOT_TOKEN" ]]; then
      warn "--user-bot selected but --user-bot-token is empty. Service will be installed but not started."
    fi
    install_user_bot
  fi

  reload_systemd

  if [[ $START_SERVICES -eq 1 ]]; then
    [[ $DO_ADMIN -eq 1 && -n "$ADMIN_BOT_TOKEN" ]] && enable_start_unit "$(basename "$ADMIN_SERVICE")" || true
    [[ $DO_USER  -eq 1 && -n "$USER_BOT_TOKEN"  ]] && enable_start_unit "$(basename "$USER_SERVICE")" || true
  else
    warn "--no-start specified — services were not enabled/started"
  fi

  echo
  log "Installation summary:"
  echo "  Repo dir:      $REPO_DIR"
  echo "  Venv:          $VENV_DIR"
  if [[ $DO_SERVER -eq 1 ]]; then
    echo "  Server:        iface=$IFACE port=$PORT subnet=$SUBNET dns=$DNS_SERVERS"
  fi
  if [[ $DO_ADMIN -eq 1 ]]; then
    echo "  Admin bot:     env=$ADMIN_ENV_FILE unit=$ADMIN_SERVICE"
  fi
  if [[ $DO_USER -eq 1 ]]; then
    echo "  User bot:      env=$USER_ENV_FILE unit=$USER_SERVICE"
  fi
  echo
  log "Done."
}

main "$@"

