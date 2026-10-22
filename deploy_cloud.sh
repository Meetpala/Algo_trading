#!/bin/bash
# ==============================================================================
# APEX-3 Zero-Dependency Cloud Setup Script (Ubuntu / Debian LTS)
# Sets up Docker, Docker Compose, IST timezone, UFW firewall, and boots APEX-3
# ==============================================================================
set -e

echo "=================================================================="
echo "    APEX-3 AUTOMATED CLOUD DEPLOYMENT & ZERO-DEPENDENCY SETUP     "
echo "=================================================================="

# 1. Update OS and set Timezone to Asia/Kolkata
echo "[1/5] Configuring system timezone to Asia/Kolkata (IST)..."
sudo timedatectl set-timezone Asia/Kolkata

# 2. Install Docker & Docker Compose if not already installed
if ! command -v docker &> /dev/null; then
    echo "[2/5] Installing Docker Engine..."
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
    sudo usermod -aG docker "$USER"
    echo "Docker installed successfully."
else
    echo "[2/5] Docker is already installed."
fi

# 3. Configure Firewall (Port 22 for SSH, Port 5000 for Dashboard)
echo "[3/5] Configuring firewall rules (SSH 22 + Dashboard 5000)..."
if command -v ufw &> /dev/null; then
    sudo ufw allow 22/tcp || true
    sudo ufw allow 5000/tcp || true
    echo "y" | sudo ufw enable || true
fi

# 4. Create database and log directories
echo "[4/5] Preparing persistent data volumes..."
mkdir -p logs
touch apex3_journal.db

# 5. Build and launch APEX-3 container
echo "[5/5] Building and launching APEX-3 Docker container..."
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
echo ""
echo "  Next Step (Zerodha Kite App Console):"
echo "  Set your Kite App Redirect URL to:"
echo "    http://$PUBLIC_IP:5000/auth/callback"
echo "=================================================================="
