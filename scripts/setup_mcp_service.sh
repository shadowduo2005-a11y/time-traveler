#!/usr/bin/env bash
# setup_mcp_service.sh — Install mcp_bridge.py as a systemd service on the GCP VM
# Run once: bash scripts/setup_mcp_service.sh
#
# After running, check logs with:
#   sudo journalctl -u mcp-bridge -f

set -e

USER="$(whoami)"
HOME_DIR="${HOME}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PYTHON="${REPO_DIR}/.venv/bin/python3"
if [ ! -f "${VENV_PYTHON}" ]; then
  VENV_PYTHON="$(which python3)"
fi
SERVICE_NAME="mcp-bridge"

# ── Load settings from .env file (never commit .env to git) ───────────────────
ENV_FILE="${REPO_DIR}/.env"
if [ -f "${ENV_FILE}" ]; then
  echo "==> Loading config from ${ENV_FILE}..."
  set -o allexport
  source "${ENV_FILE}"
  set +o allexport
else
  echo "WARNING: No .env file found at ${ENV_FILE}"
  echo "Create one from .env.example: cp ${REPO_DIR}/.env.example ${ENV_FILE}"
  echo "Then edit it with your real values (especially GITHUB_TOKEN)."
  echo ""
fi

# ── Fallback defaults (used only if not set in .env) ──────────────────────────
MCP_API_KEY="${MCP_API_KEY:-}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-}"
SHADOW_DB_PASSWORD="${SHADOW_DB_PASSWORD:-}"

# Public IP of this VM — returned in health checks and shadow clone URLs
VM_PUBLIC_IP="${VM_PUBLIC_IP:-127.0.0.1}"

# Port the bridge listens on
MCP_PORT="${MCP_PORT:-8888}"

# ── GitHub integration (optional) ─────────────────────────────────────────────
# Set GITHUB_TOKEN to a PAT with repo scope to enable automatic PR comment posting.
# Leave blank ("") to disable — the bridge will still work, just no auto-comments.
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

# GitHub repository path — used to construct PR comments API URL
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-}"

# ── Shadow Clone lifecycle ────────────────────────────────────────────────────
# Hours before a shadow clone is automatically torn down by the background thread.
# Set to 0 to disable auto-teardown (not recommended for shared VMs).
CLONE_TTL_HOURS="${CLONE_TTL_HOURS:-2}"

# ── Cloud Run overflow (optional) ─────────────────────────────────────────────
# Set CLOUD_RUN_PROJECT to your GCP project ID to enable Cloud Run overflow
# when the VM doesn't have enough RAM/disk for another shadow clone.
# Leave blank ("") to disable — the bridge will return a 400 if /deploy_cloud_run is called.
CLOUD_RUN_PROJECT="${CLOUD_RUN_PROJECT:-}"

# Cloud Run region — only used when CLOUD_RUN_PROJECT is set
CLOUD_RUN_REGION="${CLOUD_RUN_REGION:-us-central1}"

# ── VM paths ──────────────────────────────────────────────────────────────────
# Absolute path to the repo on the VM — used for git pull and docker build
REPO_DIR_ENV="${REPO_DIR}"

# Absolute path to the base schema SQL — mounted into every shadow DB container
SCHEMA_SQL="${SCHEMA_SQL:-${REPO_DIR}/db/schema.sql}"

# ── Install ───────────────────────────────────────────────────────────────────

echo "==> Stopping existing mcp_bridge processes (if any)..."
pkill -f mcp_bridge.py 2>/dev/null || true

echo "==> Writing systemd service file..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
[Unit]
Description=Time-Traveler MCP Bridge
After=network.target

[Service]
User=${USER}
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV_PYTHON} mcp_bridge.py

# ── Core ──────────────────────────────────────────────────────────────────────
# Secret token — must match X-API-Key header used by agents
Environment=MCP_API_KEY=${MCP_API_KEY}
Environment=POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
Environment=SHADOW_DB_PASSWORD=${SHADOW_DB_PASSWORD}

# Public IP returned in shadow clone URLs
Environment=VM_PUBLIC_IP=${VM_PUBLIC_IP}

# Port the bridge listens on (default 8888)
Environment=MCP_PORT=${MCP_PORT}

# ── GitHub integration ────────────────────────────────────────────────────────
# PAT with repo scope — enables automatic PR comment posting after deploy
# Leave blank to disable
Environment=GITHUB_TOKEN=${GITHUB_TOKEN}

# GitHub repository path used to construct PR comments API URL
Environment=GITHUB_REPOSITORY=${GITHUB_REPOSITORY}

# ── Shadow Clone lifecycle ────────────────────────────────────────────────────
# Hours before a clone is auto-torn-down by the background thread
Environment=CLONE_TTL_HOURS=${CLONE_TTL_HOURS}

# ── Cloud Run overflow ────────────────────────────────────────────────────────
# GCP project ID — enables /deploy_cloud_run when VM headroom is insufficient
# Leave blank to disable Cloud Run overflow
Environment=CLOUD_RUN_PROJECT=${CLOUD_RUN_PROJECT}

# Cloud Run region — only used when CLOUD_RUN_PROJECT is set
Environment=CLOUD_RUN_REGION=${CLOUD_RUN_REGION}

# ── VM paths ──────────────────────────────────────────────────────────────────
# Absolute path to the cloned repo — used by /rebuild (git pull + docker build)
Environment=REPO_DIR=${REPO_DIR_ENV}

# Absolute path to base schema SQL — mounted into every shadow DB container
Environment=SCHEMA_SQL=${SCHEMA_SQL}

Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "==> Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "==> Enabling service (auto-start on boot)..."
sudo systemctl enable ${SERVICE_NAME}

echo "==> Starting service..."
sudo systemctl restart ${SERVICE_NAME}

echo "==> Waiting for bridge to come up..."
sleep 3

echo "==> Status:"
sudo systemctl status ${SERVICE_NAME} --no-pager

echo ""
echo "==> Health check:"
curl -s http://${VM_PUBLIC_IP}:${MCP_PORT}/health && echo ""

echo ""
echo "Done! Useful commands:"
echo "  sudo systemctl status ${SERVICE_NAME}     # check status"
echo "  sudo systemctl restart ${SERVICE_NAME}    # restart"
echo "  sudo journalctl -u ${SERVICE_NAME} -f     # live logs"
echo ""
echo "Configured features:"
echo "  GitHub PR comments: $([ -n '${GITHUB_TOKEN}' ] && echo 'enabled' || echo 'disabled (set GITHUB_TOKEN)')"
echo "  Cloud Run overflow: $([ -n '${CLOUD_RUN_PROJECT}' ] && echo 'enabled (project=${CLOUD_RUN_PROJECT})' || echo 'disabled (set CLOUD_RUN_PROJECT)')"
echo "  Clone TTL: ${CLONE_TTL_HOURS}h"
