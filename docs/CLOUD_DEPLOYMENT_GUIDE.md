# APEX-3: Complete Turn-Key Cloud Deployment Guide

This guide details how to deploy APEX-3 to a cloud virtual machine (AWS EC2, Google Cloud, DigitalOcean, or Hetzner) with **Zero Dependency Issues** using Docker and Docker Compose.

---

## 1. Why Run APEX-3 on the Cloud?

1. **SEBI Static IP Compliance**: Indian regulations (effective April 2026) mandate that retail algorithmic trading APIs operate through registered static IPs. Cloud VMs come with a permanent static public IP.
2. **Zero Dependency / OS Conflicts**: By running in a Docker container, the entire Python environment, C-extensions, system libraries, and timezone (`Asia/Kolkata`) run identically on any Linux server without dependency errors.
3. **24/7 Availability**: No risk of laptop sleeping, battery draining, or home WiFi dropping during live market hours.
4. **"Login Once from Anywhere"**: Each morning at ~08:45 AM IST, open the dashboard on your phone or computer, tap "Login to Kite", approve 2FA, and the cloud server is armed and trading. You never need to touch an SSH terminal in the morning.

---

## 2. Choosing a Cloud VM (Recommended Options)

Any standard 64-bit Linux VM with **1–2 GB RAM** and **Ubuntu 22.04 or 24.04 LTS** is ideal:

| Provider | Instance Type | Static IP Included? | Approximate Cost |
| :--- | :--- | :--- | :--- |
| **AWS EC2** | `t3.small` (or `t3.micro`) | Yes (Allocate Elastic IP) | Free tier eligible / ~$8/month |
| **Google Cloud (GCP)** | `e2-micro` / `e2-small` | Yes (Reserve Static External IP) | Free tier / ~$7/month |
| **DigitalOcean** | Basic Droplet (1 GB RAM) | Yes (Static Public IP included) | ~$6/month |
| **Hetzner Cloud** | `CX22` (2 vCPU, 4 GB RAM) | Yes (Static IPv4 included) | ~€4/month |

---

## 3. Step-by-Step Turn-Key Deployment (Takes ~3 Minutes)

### Step 3.1: Connect to your Cloud VM via SSH
Open your terminal (PowerShell or Terminal on Mac/Linux):
```bash
ssh ubuntu@<YOUR_CLOUD_PUBLIC_IP>
```

### Step 3.2: Copy Project Files to Cloud VM
From your local computer, copy your project folder to the server using `scp` or `rsync`:
```powershell
# Run from your local machine:
scp -r "c:\Users\itsme\OneDrive\Desktop\Algo Trading" ubuntu@<YOUR_CLOUD_PUBLIC_IP>:~/Algo_trading
```
*(Or clone via git if your repo is on GitHub/GitLab, then copy your `.env` file).*

### Step 3.3: Run the Turn-Key Setup Script
On your cloud VM terminal:
```bash
cd ~/Algo_trading
chmod +x deploy_cloud.sh
./deploy_cloud.sh
```
This single script automatically:
1. Configures the system clock to **Asia/Kolkata (IST)**.
2. Installs Docker and Docker Compose.
3. Opens firewall ports (Port 22 for SSH and Port 5000 for your Operator Dashboard).
4. Builds the container with all locked Python dependencies (`requirements.txt`).
5. Starts APEX-3 in the background with automatic restart policies.

---

## 4. Configuring 1-Click Morning Authentication

In your **Zerodha Kite Developer App Console** (https://developers.kite.trade/apps):
1. Open your registered App details.
2. Set the **Redirect URL** to:
   ```
   http://<YOUR_CLOUD_PUBLIC_IP>:5000/auth/callback
   ```
3. Save changes.

### Daily Morning Workflow (08:30 – 09:10 AM IST):
1. On your phone or laptop browser, open:
   ```
   http://<YOUR_CLOUD_PUBLIC_IP>:5000/login/kite
   ```
2. Log in with your Zerodha User ID, password, and approve mobile TOTP.
3. Zerodha redirects back to your cloud dashboard.
4. The cloud dashboard automatically catches the `request_token`, exchanges it for the daily `access_token`, saves it into `.env`, updates the running engine in-memory, and begins autonomous monitoring.
5. **Done! Zero terminal commands or manual copy-pasting required.**

---

## 5. Maintenance Commands (Quick Reference)

From your cloud VM terminal:
- **Check live container logs**:
  ```bash
  sudo docker compose logs -f
  ```
- **Restart the trading engine**:
  ```bash
  sudo docker compose restart
  ```
- **Stop the trading engine**:
  ```bash
  sudo docker compose down
  ```
- **Update code changes**:
  ```bash
  sudo docker compose up -d --build
  ```
