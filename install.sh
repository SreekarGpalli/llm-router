#!/usr/bin/env bash
# =============================================================================
# LLM Router — Bootstrap Installer
# Tested on: Ubuntu 22.04 LTS (GCP e2-micro, 1 GB RAM)
# Run as: curl -sSL https://your-repo/install.sh | bash
# Or:     bash install.sh
# =============================================================================
set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m'
CYAN='\033[0;36m' BOLD='\033[1m' NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERR]${NC}   $*" >&2; exit 1; }
banner()  { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}\n"; }

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Please run as root: sudo bash install.sh"

INSTALL_DIR="/opt/llm-router"
DATA_DIR="$INSTALL_DIR/data"
CF_DIR="$INSTALL_DIR/cloudflared"
VENV="$INSTALL_DIR/venv"
SERVICE_USER="llmrouter"

banner "LLM Router Installer"
echo "Install directory : $INSTALL_DIR"
echo "Running as        : $(whoami)"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
banner "Step 1/10 — System packages"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev python3-pip \
    git curl wget unzip sqlite3 \
    fail2ban unattended-upgrades apt-listchanges \
    ca-certificates gnupg lsb-release
success "Packages installed"

# ── 2. Swap file (512 MB) ─────────────────────────────────────────────────────
banner "Step 2/10 — Swap (512 MB)"

if swapon --show | grep -q /swapfile 2>/dev/null; then
    warn "Swap already configured — skipping"
else
    fallocate -l 512M /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=512 status=none
    chmod 600 /swapfile
    mkswap /swapfile -q
    swapon /swapfile
    # Make permanent
    if ! grep -q /swapfile /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    # Tune swappiness — only swap under real memory pressure
    sysctl -w vm.swappiness=10 > /dev/null
    if ! grep -q vm.swappiness /etc/sysctl.conf; then
        echo 'vm.swappiness=10' >> /etc/sysctl.conf
    fi
    success "512 MB swap created and persisted"
fi

# ── 3. Disable heavy services ─────────────────────────────────────────────────
banner "Step 3/10 — Disable snapd / ModemManager"

for svc in snapd ModemManager; do
    if systemctl is-enabled "$svc" &>/dev/null; then
        systemctl disable --now "$svc" 2>/dev/null || true
        success "Disabled $svc"
    else
        warn "$svc not active — skipping"
    fi
done

# ── 4. SSH hardening ──────────────────────────────────────────────────────────
banner "Step 4/10 — SSH hardening"

SSHD_CONF=/etc/ssh/sshd_config
HARDENING="
# === LLM Router hardening ===
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
MaxAuthTries 3
LoginGraceTime 30
"

if ! grep -q "LLM Router hardening" "$SSHD_CONF"; then
    echo "$HARDENING" >> "$SSHD_CONF"
    sshd -t && systemctl reload sshd
    success "SSH hardened"
else
    warn "SSH already hardened — skipping"
fi

# ── 5. Fail2Ban + unattended upgrades ────────────────────────────────────────
banner "Step 5/10 — Fail2Ban + auto-updates"

systemctl enable fail2ban --now
success "Fail2Ban enabled"

# Enable security-only unattended upgrades
cat > /etc/apt/apt.conf.d/50unattended-upgrades-llmrouter <<'EOF'
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
cat > /etc/apt/apt.conf.d/20auto-upgrades-llmrouter <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
success "Unattended security upgrades configured"

# ── 6. Application code ───────────────────────────────────────────────────────
banner "Step 6/10 — Application setup"

# Create service user
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    success "Service user '$SERVICE_USER' created"
fi

# Create directories
mkdir -p "$INSTALL_DIR" "$DATA_DIR" "$CF_DIR" "$INSTALL_DIR/static"

# Copy files from current directory or download from repo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
if [[ -f "$SCRIPT_DIR/main.py" ]]; then
    info "Copying files from $SCRIPT_DIR"
    cp -r "$SCRIPT_DIR"/{main.py,db.py,translator.py,router.py,crypto.py,auth.py,requirements.txt} "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/static" "$INSTALL_DIR/"
    [[ -f "$SCRIPT_DIR/llm-router.service" ]] && cp "$SCRIPT_DIR/llm-router.service" /etc/systemd/system/
    [[ -f "$SCRIPT_DIR/cloudflared.service" ]] && cp "$SCRIPT_DIR/cloudflared.service" /etc/systemd/system/
    success "Files copied from local directory"
else
    die "Cannot find application files. Run install.sh from the llm-router directory."
fi

# Python virtualenv
python3.11 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
success "Python virtualenv created and dependencies installed"

# ── 7. Interactive configuration ─────────────────────────────────────────────
banner "Step 7/10 — Configuration"
echo "Please answer 4 questions. Press Enter to accept defaults."
echo ""

# Secret key
DEFAULT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(40))")
read -rp "$(echo -e "${BOLD}Secret key${NC} [auto-generated, press Enter to accept]: ")" SECRET_KEY
SECRET_KEY="${SECRET_KEY:-$DEFAULT_SECRET}"

# UI password
while true; do
    read -rsp "$(echo -e "${BOLD}UI password${NC} (required, not shown): ")" UI_PASSWORD
    echo ""
    [[ -n "$UI_PASSWORD" ]] && break
    warn "UI_PASSWORD cannot be empty."
done

# Cloudflare domain
read -rp "$(echo -e "${BOLD}Cloudflare domain${NC} (e.g. router.yourdomain.com): ")" CF_DOMAIN
CF_DOMAIN="${CF_DOMAIN:-router.yourdomain.com}"

# Port
read -rp "$(echo -e "${BOLD}Port${NC} [8000]: ")" PORT
PORT="${PORT:-8000}"

# Generate virtual API key
VIRTUAL_KEY="sk-router-$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")"
VIRTUAL_KEY_HASH=$(python3 -c "import hashlib; print(hashlib.sha256('$VIRTUAL_KEY'.encode()).hexdigest())")

# Write .env
cat > "$INSTALL_DIR/.env" <<EOF
SECRET_KEY=$SECRET_KEY
UI_PASSWORD=$UI_PASSWORD
CLOUDFLARE_DOMAIN=$CF_DOMAIN
PORT=$PORT
DB_PATH=$DATA_DIR/router.db
INITIAL_VIRTUAL_KEY=$VIRTUAL_KEY
EOF
chmod 600 "$INSTALL_DIR/.env"
success ".env written to $INSTALL_DIR/.env"

# ── 8. Cloudflare Tunnel ──────────────────────────────────────────────────────
banner "Step 8/10 — Cloudflare Tunnel"

# Download cloudflared
CF_VERSION="2024.12.2"
CF_ARCH="linux-amd64"
CF_URL="https://github.com/cloudflare/cloudflared/releases/download/$CF_VERSION/cloudflared-$CF_ARCH"

if [[ ! -f /usr/local/bin/cloudflared ]]; then
    info "Downloading cloudflared $CF_VERSION..."
    curl -sSL "$CF_URL" -o /usr/local/bin/cloudflared
    chmod +x /usr/local/bin/cloudflared
    success "cloudflared installed"
else
    warn "cloudflared already installed — skipping download"
fi

# Authenticate
echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}Cloudflare authentication required.${NC}"
echo "A URL will be printed below. Open it in your browser to authorise."
echo "This will create ~/.cloudflared/cert.pem"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Run login as the service user so the cert ends up in their home area
# (we'll use --origincert flag to point at it explicitly)
CF_CERT_DIR="$CF_DIR"
mkdir -p "$CF_CERT_DIR"

sudo -u "$SERVICE_USER" bash -c \
    "HOME=$CF_CERT_DIR /usr/local/bin/cloudflared tunnel login --config $CF_DIR/config.yml" 2>&1 || true

# If cert ended up in /root/.cloudflared, copy it
if [[ -f "/root/.cloudflared/cert.pem" && ! -f "$CF_DIR/cert.pem" ]]; then
    cp /root/.cloudflared/cert.pem "$CF_DIR/cert.pem"
fi
if [[ ! -f "$CF_DIR/cert.pem" ]]; then
    warn "cert.pem not found in $CF_DIR — you may need to run:"
    warn "  cloudflared tunnel login"
    warn "  cp ~/.cloudflared/cert.pem $CF_DIR/"
fi

# Create tunnel
TUNNEL_NAME="llm-router"
info "Creating Cloudflare tunnel '$TUNNEL_NAME'..."
TUNNEL_OUTPUT=$(cloudflared tunnel --origincert "$CF_DIR/cert.pem" create "$TUNNEL_NAME" 2>&1) || true
echo "$TUNNEL_OUTPUT"
TUNNEL_ID=$(echo "$TUNNEL_OUTPUT" | grep -oP '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || echo "")

if [[ -z "$TUNNEL_ID" ]]; then
    # Tunnel may already exist — try to get its ID
    TUNNEL_ID=$(cloudflared tunnel --origincert "$CF_DIR/cert.pem" list 2>/dev/null \
        | grep "$TUNNEL_NAME" | awk '{print $1}' | head -1 || echo "")
fi

# Write cloudflared config
cat > "$CF_DIR/config.yml" <<EOF
tunnel: ${TUNNEL_ID:-YOUR_TUNNEL_ID}
credentials-file: $CF_DIR/${TUNNEL_ID:-TUNNEL_ID}.json
origincert: $CF_DIR/cert.pem

ingress:
  - hostname: $CF_DOMAIN
    service: http://127.0.0.1:$PORT
  - service: http_status:404
EOF
success "Cloudflare tunnel config written to $CF_DIR/config.yml"

# Route DNS (may require manual step if token lacks DNS perms)
info "Configuring DNS route for $CF_DOMAIN..."
cloudflared tunnel --origincert "$CF_DIR/cert.pem" route dns "$TUNNEL_NAME" "$CF_DOMAIN" 2>&1 || \
    warn "DNS route failed — add a CNAME manually: $CF_DOMAIN → ${TUNNEL_ID:-TUNNEL_ID}.cfargotunnel.com"

# ── 9. Permissions & systemd ──────────────────────────────────────────────────
banner "Step 9/10 — Systemd services"

chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# Update service files with resolved PORT if needed
sed -i "s/\${PORT:-8000}/$PORT/" /etc/systemd/system/llm-router.service 2>/dev/null || true

systemctl daemon-reload
systemctl enable llm-router cloudflared
systemctl restart llm-router
sleep 3  # brief wait for startup

# Check if it came up
if systemctl is-active --quiet llm-router; then
    success "llm-router service is running"
else
    warn "llm-router failed to start — check logs: journalctl -u llm-router -n 50"
fi

systemctl restart cloudflared
if systemctl is-active --quiet cloudflared; then
    success "cloudflared service is running"
else
    warn "cloudflared failed to start — check config at $CF_DIR/config.yml"
fi

# ── 10. Summary ───────────────────────────────────────────────────────────────
banner "Step 10/10 — Installation complete"

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║               LLM Router installed successfully!             ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Public URL:${NC}      https://$CF_DOMAIN"
echo -e "  ${BOLD}Health check:${NC}    curl https://$CF_DOMAIN/health"
echo ""
echo -e "${RED}${BOLD}  ┌─────────────────────────────────────────────────────────┐${NC}"
echo -e "${RED}${BOLD}  │  VIRTUAL API KEY (shown once — save it now!)            │${NC}"
echo -e "${RED}${BOLD}  │                                                         │${NC}"
echo -e "${RED}${BOLD}  │  $VIRTUAL_KEY  │${NC}"
echo -e "${RED}${BOLD}  │                                                         │${NC}"
echo -e "${RED}${BOLD}  └─────────────────────────────────────────────────────────┘${NC}"
echo ""
echo -e "  ${BOLD}Claude Code setup:${NC}"
echo -e "    export ANTHROPIC_BASE_URL=https://$CF_DOMAIN/v1"
echo -e "    export ANTHROPIC_API_KEY=$VIRTUAL_KEY"
echo ""
echo -e "  ${BOLD}UI access (via SSH tunnel for security):${NC}"
echo -e "    ssh -L 8080:localhost:$PORT user@<VM_IP>"
echo -e "    Then open: http://localhost:8080"
echo ""
echo -e "  ${BOLD}Logs:${NC}    journalctl -u llm-router -f"
echo -e "  ${BOLD}Memory:${NC}  ps aux | grep uvicorn"
echo ""

exit 0
