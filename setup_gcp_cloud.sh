#!/bin/bash
# ==============================================================================
# APEX-3 One-Click GCP Cloud VM Setup Script (Ubuntu / Debian)
# Sets timezone to IST (Asia/Kolkata), creates .venv, installs dependencies & tmux
# ==============================================================================
set -e

echo "=================================================================="
echo "      APEX-3 GOOGLE CLOUD VM PERMANENT SETUP INSTALLER            "
echo "=================================================================="

# 1. Configure system timezone to Asia/Kolkata (IST)
echo "[1/4] Setting server timezone to Asia/Kolkata (IST)..."
sudo timedatectl set-timezone Asia/Kolkata
echo "Current server time: $(date)"

# 2. Update packages and install python3-venv, git, tmux
echo "[2/4] Installing Python 3 virtual environment tools and tmux..."
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv tmux git curl

# 3. Create virtual environment if not already existing
echo "[3/4] Initializing Python virtual environment (.venv)..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

# 4. Install python dependencies inside .venv
echo "[4/4] Installing APEX-3 dependencies..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pandas logzero websocket-client

# Ensure logs directory exists
mkdir -p logs

echo ""
echo "=================================================================="
echo "  SUCCESS: APEX-3 ENVIRONMENT IS READY ON YOUR GOOGLE CLOUD VM!  "
echo "=================================================================="
echo ""
echo "To run live trading in a 24/7 background session (that never closes):"
echo ""
echo "  1. Start a persistent tmux session:"
echo "       tmux new -s algo"
echo ""
echo "  2. Launch the live engine:"
echo "       .venv/bin/python connect_live.py"
echo ""
echo "  3. To detach and leave it running in background:"
echo "       Press Ctrl+B, then press D"
echo ""
echo "  4. To re-attach and check live logs anytime you SSH back in:"
echo "       tmux attach -t algo"
echo "=================================================================="

