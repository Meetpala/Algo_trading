#!/bin/bash
# ==============================================================================
# APEX-3 Production Cloud Deployment & Hardening Script (Ubuntu / Debian LTS)
# Configures IST NTP sync, UFW inbound/outbound rules, systemd/Docker & health
# ==============================================================================
set -e

echo "=================================================================="
echo "    APEX-3 PRODUCTION CLOUD DEPLOYMENT & SECURITY HARDENING       "
echo "=================================================================="

APP_USER="${SUDO_USER:-$USER}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Configure system timezone to Asia/Kolkata & enforce NTP synchronization
echo "[1/6] Configuring system timezone to Asia/Kolkata (IST) & NTP sync..."
sudo timedatectl set-timezone Asia/Kolkata
sudo timedatectl set-ntp true || true

# 2. Install Docker & Docker Compose if not already installed
if ! command -v docker &> /dev/null; then
    echo "[2/6] Installing Docker Engine..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg lsb-release ufw

    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo systemctl enable docker
    sudo systemctl start docker
    sudo usermod -aG docker "$APP_USER"
    echo "Docker installed successfully."
else
    echo "[2/6] Docker is already installed."
fi

# 3. Configure Firewall (SSH 22 + Kite OAuth/Dashboard 5000 + HTTPS/DNS/NTP outbound)
echo "[3/6] Configuring UFW firewall rules (SSH 22 + Dashboard/OAuth 5000)..."
if command -v ufw &> /dev/null; then
    sudo ufw default deny incoming || true
    sudo ufw default allow outgoing || true
    sudo ufw allow 22/tcp comment 'SSH access' || true
    sudo ufw allow 5000/tcp comment 'APEX-3 Kite OAuth Callback & Dashboard' || true
    sudo ufw allow out 443/tcp comment 'Broker HTTPS/WSS & Telegram API' || true
    sudo ufw allow out 53/udp comment 'DNS' || true
    sudo ufw allow out 53/tcp comment 'DNS TCP' || true
    sudo ufw allow out 123/udp comment 'NTP clock sync' || true
    echo "y" | sudo ufw enable || true
fi

# 4. Install parameterized systemd unit file (for bare-venv systemd mode)
echo "[4/6] Installing parameterized systemd unit (/etc/systemd/system/apex3.service)..."
if [ -f "$APP_DIR/apex3.service" ]; then
    sed -e "s|__APEX3_USER__|$APP_USER|g" \
        -e "s|__APEX3_WORKDIR__|$APP_DIR|g" \
        "$APP_DIR/apex3.service" | sudo tee /etc/systemd/system/apex3.service > /dev/null
    sudo systemctl daemon-reload
fi

# 5. Create database and log directories
echo "[5/6] Preparing persistent data volumes..."
mkdir -p "$APP_DIR/logs"
touch "$APP_DIR/apex3_journal.db" "$APP_DIR/info.log"

# 6. Build and launch APEX-3 container
echo "[6/6] Building and launching APEX-3 Docker container..."
sudo docker compose down 2>/dev/null || true
sudo docker compose build --no-cache
sudo docker compose up -d

PUBLIC_IP=$(curl -s https://api.ipify.org || hostname -I | awk '{print $1}')

echo ""
echo "=================================================================="
echo "  SUCCESS: APEX-3 IS NOW RUNNING ON THE CLOUD!                   "
echo "=================================================================="
echo "  Cloud Public IP: $PUBLIC_IP"
echo "  Operator Dashboard: http://$PUBLIC_IP:5000"
echo "  Kite 1-Click Login: http://$PUBLIC_IP:5000/login/kite"
echo "  Health Check:       http://$PUBLIC_IP:5000/health"
echo ""
echo "  Next Step (Zerodha Kite App Console):"
echo "  Set your Kite App Redirect URL to:"
echo "    http://$PUBLIC_IP:5000/auth/callback"
echo "=================================================================="
