#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
#  Awesome Router 2 — installer
#  https://github.com/GonzFC/awesome-router
#
#  One-liner:
#    curl -fsSL https://raw.githubusercontent.com/GonzFC/awesome-router/main/install.sh | sudo bash
#
#  Idempotent. Safe to run on:
#    • Fresh Ubuntu 24.04   → installs everything + runs setup wizard
#    • Existing install     → updates code, shows menu (re-run wizard / update / reset)
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

REPO_URL="${AWESOME_ROUTER_REPO_URL:-https://github.com/GonzFC/awesome-router.git}"
REPO_BRANCH="${AWESOME_ROUTER_BRANCH:-main}"
INSTALL_DIR="/opt/awesome-router"

# ─── colors ──────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_GREEN=$'\033[1;32m'
  C_BLUE=$'\033[1;34m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'
  C_CYAN=$'\033[1;36m'; C_NC=$'\033[0m'
else
  C_BOLD=""; C_DIM=""; C_GREEN=""; C_BLUE=""; C_YELLOW=""; C_RED=""; C_CYAN=""; C_NC=""
fi

step() { echo "${C_CYAN}▶${C_NC} ${C_BOLD}$*${C_NC}"; }
ok()   { echo "  ${C_GREEN}✓${C_NC} $*"; }
warn() { echo "  ${C_YELLOW}⚠${C_NC} $*"; }
err()  { echo "  ${C_RED}✗${C_NC} $*" >&2; }
info() { echo "  ${C_BLUE}ℹ${C_NC} $*"; }

banner() {
  cat <<'EOF'

  ╭──────────────────────────────────────────────╮
  │      AWESOME ROUTER 2 — installer            │
  │      Multi-WAN router for Ubuntu 24.04       │
  ╰──────────────────────────────────────────────╯

EOF
}

# ─── pre-flight checks ───────────────────────────────────────────────────

check_root() {
  if [[ $EUID -ne 0 ]]; then
    err "Must be run as root. Try: ${C_BOLD}curl -fsSL <url> | sudo bash${C_NC}"
    exit 1
  fi
}

check_os() {
  if [[ ! -f /etc/os-release ]]; then
    err "Cannot detect OS. Aborting."
    exit 1
  fi
  . /etc/os-release
  if [[ "$ID" != "ubuntu" ]]; then
    warn "Detected $PRETTY_NAME (not Ubuntu). The installer is tested on Ubuntu 24.04 only."
    read -r -p "  Continue anyway? [y/N] " ans
    [[ "$ans" =~ ^[Yy] ]] || exit 1
  elif [[ "$VERSION_ID" != "24.04" ]]; then
    warn "Detected Ubuntu $VERSION_ID (not 24.04). The installer is tested on 24.04 only."
    read -r -p "  Continue anyway? [y/N] " ans
    [[ "$ans" =~ ^[Yy] ]] || exit 1
  else
    ok "Ubuntu 24.04 detected"
  fi
}

# ─── steps ───────────────────────────────────────────────────────────────

install_packages() {
  step "Installing system packages"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq \
    git curl ca-certificates \
    python3 python3-pip python3-yaml python3-psutil \
    python3-flask python3-jinja2 \
    nftables fping nmap iproute2 \
    >/dev/null
  ok "All apt packages installed"
}

clone_or_update() {
  step "Fetching code from $REPO_URL ($REPO_BRANCH)"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Existing install detected. Pulling latest..."
    git -C "$INSTALL_DIR" fetch --quiet origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" reset --hard --quiet "origin/$REPO_BRANCH"
    ok "Code updated"
  else
    if [[ -d "$INSTALL_DIR" ]]; then
      err "$INSTALL_DIR exists but isn't a git repo. Move it aside first."
      exit 1
    fi
    git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
  fi
}

install_systemd_units() {
  step "Installing systemd units"
  for unit in "$INSTALL_DIR"/installer/systemd/*.service; do
    name=$(basename "$unit")
    cp "$unit" "/etc/systemd/system/$name"
  done
  systemctl daemon-reload
  ok "Installed $(ls -1 "$INSTALL_DIR"/installer/systemd/*.service | wc -l) service unit(s)"
}

install_sampler() {
  step "Installing bandwidth sampler"
  cp "$INSTALL_DIR/installer/scripts/router-stats-sampler.py" /usr/local/bin/
  chmod +x /usr/local/bin/router-stats-sampler.py
  ok "Sampler installed at /usr/local/bin/router-stats-sampler.py"
}

create_runtime_dirs() {
  mkdir -p /var/lib/awesome-router/snapshots
  mkdir -p /run/awesome-router
}

run_wizard() {
  step "Launching setup wizard"
  echo
  cd "$INSTALL_DIR"
  python3 -m awesome_router.wizard
}

# ─── main ─────────────────────────────────────────────────────────────────

main() {
  banner
  check_root
  check_os
  install_packages
  clone_or_update
  install_systemd_units
  install_sampler
  create_runtime_dirs
  run_wizard
}

main "$@"
