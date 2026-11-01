#!/usr/bin/env python3
"""
MCP Bridge — runs on the GCP VM.

Exposes two interfaces:
  1. REST API  — for the LangGraph workflow and direct administration
  2. MCP Protocol (JSON-RPC 2.0) at POST /mcp — for GitHub Copilot / IDE agent tool calls

Start:  MCP_API_KEY=<key> VM_PUBLIC_IP=<host> python3 mcp_bridge.py
"""

import os
import json
import socket
import random
import secrets
import shutil
import subprocess
import time
import threading
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Any

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

# ── Config ───────────────────────────────────────────────────────────────────

API_KEY    = os.getenv("MCP_API_KEY", "")
VM_IP      = os.getenv("VM_PUBLIC_IP", "127.0.0.1")
REGISTRY   = os.getenv("REGISTRY", "")
REPO_DIR    = os.getenv("REPO_DIR", os.path.abspath(os.path.dirname(__file__)))
SCHEMA_SQL  = os.getenv("SCHEMA_SQL", os.path.join(REPO_DIR, "db", "schema.sql"))
MCP_PORT   = int(os.getenv("MCP_PORT", "8888"))
PROD_DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
SHADOW_DB_PASSWORD = os.getenv("SHADOW_DB_PASSWORD") or secrets.token_urlsafe(24)

# GitHub / GitOps integration — used to post PR/MR comments automatically after deploy
GITHUB_TOKEN      = os.getenv("GITHUB_TOKEN", os.getenv("GH_TOKEN", os.getenv("GITLAB_TOKEN", "")))
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", os.getenv("GITLAB_PROJECT_ID", ""))
GITLAB_TOKEN      = GITHUB_TOKEN
GITLAB_PROJECT_ID = GITHUB_REPOSITORY

# Shadow clone lifecycle — clones older than this are auto-torn-down
CLONE_TTL_HOURS = int(os.getenv("CLONE_TTL_HOURS", "2"))

# Cloud Run overflow — used when VM headroom is insufficient
CLOUD_RUN_PROJECT = os.getenv("CLOUD_RUN_PROJECT", "")
CLOUD_RUN_REGION  = os.getenv("CLOUD_RUN_REGION", "us-central1")

RAM_THRESHOLD_MB  = 512
DISK_THRESHOLD_MB = 2048

# ── Clone registry ────────────────────────────────────────────────────────────
# Tracks active shadow clones: {mr_id: {"deployed_at": datetime, "frontend_url": str, ...}}
_clone_registry: dict = {}
_registry_lock = threading.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def auth(key: str):
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def free_port() -> int:
    for _ in range(200):
        port = random.randint(50000, 59999)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("localhost", port)) != 0:
                return port
    raise RuntimeError("Could not find a free port in range 50000-59999")


def system_headroom() -> dict:
    """Return Docker-host capacity on Linux and Docker Desktop hosts.

    Production Linux hosts expose ``free`` and ``/var/lib/docker``. macOS and
    Windows Docker Desktop do not, so use Docker's configured VM memory and
    the repository filesystem's free space as portable fallbacks.
    """
    available_ram_mb = 0
    if shutil.which("free"):
        rc, stdout, _ = run(["free", "-m"])
        if rc == 0:
            mem_line = next((line for line in stdout.splitlines() if line.startswith("Mem:")), "")
            parts = mem_line.split()
            if parts:
                available_ram_mb = int(parts[6]) if len(parts) > 6 else int(parts[3])
    if not available_ram_mb:
        rc, stdout, _ = run(["docker", "info", "--format", "{{.MemTotal}}"])
        if rc == 0 and stdout.strip().isdigit():
            available_ram_mb = int(stdout.strip()) // (1024 * 1024)

    docker_root = "/var/lib/docker" if os.path.isdir("/var/lib/docker") else REPO_DIR
    free_disk_mb = shutil.disk_usage(docker_root).free // (1024 * 1024)
    return {
        "sufficient": available_ram_mb >= RAM_THRESHOLD_MB and free_disk_mb >= DISK_THRESHOLD_MB,
        "available_ram_mb": available_ram_mb,
        "free_disk_mb": free_disk_mb,
        "thresholds": {"ram_mb": RAM_THRESHOLD_MB, "disk_mb": DISK_THRESHOLD_MB},
    }


def shadow_nginx_conf(backend_container: str) -> str:
    """Generate an nginx.conf that proxies /api/ to the shadow backend container."""
    return f"""server {{
    listen 80;

    location / {{
        root /usr/share/nginx/html;
        index index.html;
        try_files $uri $uri/ /index.html;
    }}

    location /api/ {{
        proxy_pass         http://{backend_container}:8000/;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 30s;
    }}
}}
"""


def _post_pr_comment(pr_id: str, message: str) -> None:
    """Post a comment to a GitHub PR (or GitLab MR). Silently skips if GITHUB_TOKEN is not set."""
    if not GITHUB_TOKEN:
        return
    if not _REQUESTS_AVAILABLE:
        print("[mcp_bridge] requests library not available — skipping PR comment")
        return
    try:
        # If standard owner/repo format and not gitlab-namespaced
        if "/" in GITHUB_REPOSITORY and not GITHUB_REPOSITORY.startswith("gitlab"):
            url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/issues/{pr_id}/comments"
            resp = _requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                },
                json={"body": message},
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                print(f"[mcp_bridge] GitHub PR comment post returned {resp.status_code}: {resp.text[:200]}")
        else:
            encoded_project = quote(GITHUB_REPOSITORY, safe="")
            url = f"https://gitlab.com/api/v4/projects/{encoded_project}/merge_requests/{pr_id}/notes"
            resp = _requests.post(
                url,
                headers={"PRIVATE-TOKEN": GITHUB_TOKEN, "Content-Type": "application/json"},
                json={"body": message},
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                print(f"[mcp_bridge] MR comment post returned {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        print(f"[mcp_bridge] PR/MR comment post failed: {exc}")


_post_mr_comment = _post_pr_comment  # alias for backwards compatibility


def _measure_container_ram(container_name: str) -> int:
    """Return current RSS memory usage of a container in MB using docker stats. Returns 0 if unavailable."""
    rc, stdout, _ = run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container_name])
    if rc != 0 or not stdout.strip():
        return 0
    # MemUsage format: "123.4MiB / 1.5GiB" — take the left side
    try:
        used = stdout.strip().split("/")[0].strip()
        if "GiB" in used:
            return int(float(used.replace("GiB", "").strip()) * 1024)
        elif "MiB" in used:
            return int(float(used.replace("MiB", "").strip()))
        elif "kB" in used or "kiB" in used:
            return 1
    except (ValueError, IndexError):
        pass
    return 0


def _teardown(mr: str) -> dict:
    """Internal teardown — safe to call even if resources don't exist."""
    # Capture deploy time before removing from registry
    deployed_at: Optional[datetime] = None
    frontend_port: Optional[int] = None
    with _registry_lock:
        entry = _clone_registry.get(mr)
        if entry:
            deployed_at = entry.get("deployed_at")
            frontend_port = entry.get("frontend_port")

    containers = [f"frontend-mr{mr}", f"backend-mr{mr}", f"db-mr{mr}"]

    # Measure real RAM before removing containers
    ram_freed_mb = sum(_measure_container_ram(c) for c in containers)

    removed = []
    for c in containers:
        rc, _, _ = run(["docker", "rm", "-f", c])
        if rc == 0:
            removed.append(c)

    run(["docker", "network", "rm", f"net-mr{mr}"])
    run(["docker", "volume",  "rm", f"db-mr{mr}-data"])

    for path in [f"/tmp/shadow_seed_mr{mr}.sql", f"/tmp/nginx_mr{mr}.conf"]:
        if os.path.exists(path):
            os.remove(path)

    # Calculate duration
    duration_minutes = 0
    if deployed_at:
        now = datetime.now(timezone.utc)
        if deployed_at.tzinfo is None:
            deployed_at = deployed_at.replace(tzinfo=timezone.utc)
        duration_minutes = int((now - deployed_at).total_seconds() / 60)

    containers_removed = len(removed)
    # CO2: GCP e2-medium ~10W under load, 0.000233 kg CO2/kWh (Google CFE-adjusted)
    # duration_minutes of 3 containers = container_minutes; convert to kWh then to grams
    container_minutes = containers_removed * duration_minutes
    co2_saved_g = round(container_minutes * (10 / 60) / 1000 * 0.233, 4)

    # Remove from registry
    with _registry_lock:
        _clone_registry.pop(mr, None)

    return {
        "status":             "torn_down",
        "mr_id":              mr,
        "removed_containers": removed,
        "network_removed":    f"net-mr{mr}",
        "volume_removed":     f"db-mr{mr}-data",
        "green_report": {
            "ram_freed_mb":           ram_freed_mb,
            "ram_measurement":        "live docker stats",
            "containers_removed":     containers_removed,
            "duration_minutes":       duration_minutes,
            "co2_saved_g":            co2_saved_g,
            "co2_methodology":        "GCP e2-medium ~10W, Google CFE-adjusted 0.233 kg CO2/kWh",
        },
    }


def _auto_teardown_loop() -> None:
    """Background daemon thread — checks every 5 minutes and tears down expired clones."""
    while True:
        time.sleep(300)  # 5 minutes
        now = datetime.now(timezone.utc)
        expired = []
        with _registry_lock:
            for mr_id, entry in list(_clone_registry.items()):
                deployed_at = entry.get("deployed_at")
                if deployed_at is None:
                    continue
                if deployed_at.tzinfo is None:
                    deployed_at = deployed_at.replace(tzinfo=timezone.utc)
                age_hours = (now - deployed_at).total_seconds() / 3600
                if age_hours >= CLONE_TTL_HOURS:
                    expired.append(mr_id)

        for mr_id in expired:
            print(f"[auto-teardown] Clone for MR {mr_id} has expired (TTL={CLONE_TTL_HOURS}h). Tearing down.")
            try:
                _teardown(mr_id)
                _post_mr_comment(
                    mr_id,
                    f"Shadow Clone for MR !{mr_id} has been automatically torn down after {CLONE_TTL_HOURS}h TTL.\n\n"
                    f"Resources freed. Deploy a new clone anytime with the SRE Monitor.",
                )
            except Exception as exc:
                print(f"[auto-teardown] Teardown of MR {mr_id} failed: {exc}")


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    t = threading.Thread(target=_auto_teardown_loop, daemon=True, name="auto-teardown")
    t.start()
    print(f"[mcp_bridge] Auto-teardown thread started (TTL={CLONE_TTL_HOURS}h, check interval=5m)")
    yield


app = FastAPI(
    title="MCP Bridge",
    description="Shadow clone management for the Time-Traveler system",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok", "vm_ip": VM_IP}


# ── 1. Headroom check ─────────────────────────────────────────────────────────

@app.get("/check_vm_headroom")
def check_vm_headroom(x_api_key: str = Header(...)):
    auth(x_api_key)
    return system_headroom()


# ── 2. Deploy shadow clone ────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    mr_id:          Optional[str] = None  # Backwards compatibility
    pr_id:          Optional[str] = None  # Pull Request ID
    seed_sql:       str                   # Generated by Data Smith
    backend_image:  Optional[str] = None  # defaults to registry/backend:latest
    frontend_image: Optional[str] = None  # defaults to registry/frontend:latest


def _ensure_image(image: str, context: str, dockerfile_dir: str) -> str:
    """Pull image from registry; if not found, use locally built image (or build it)."""
    rc, _, _ = run(["docker", "pull", image])
    if rc == 0:
        return image
    # Registry pull failed — use local image if it exists, else build it
    local_tag = f"local-{context}:latest"
    rc, _, _ = run(["docker", "image", "inspect", local_tag])
    if rc == 0:
        return local_tag   # already built locally
    # Build for the first time
    rc, _, err = run(["docker", "build", "-t", local_tag, dockerfile_dir], timeout=300)
    if rc != 0:
        raise HTTPException(500, f"Registry pull and local build both failed for {context}: {err}")
    return local_tag


class RebuildRequest(BaseModel):
    branch: Optional[str] = None  # If set, checkout this branch before building


@app.post("/rebuild")
def rebuild(req: RebuildRequest = RebuildRequest(), x_api_key: str = Header(...)):
    """Pull latest code from git and rebuild backend + frontend images locally.
    Pass branch= to build a specific MR branch (shadow clone will run MR code, not main).
    """
    auth(x_api_key)

    results = {}
    branch = req.branch

    # Fetch all remote branches
    run(["git", "-C", REPO_DIR, "fetch", "--all"])

    if branch:
        # Checkout the MR branch
        rc, stdout, stderr = run(["git", "-C", REPO_DIR, "checkout", branch])
        if rc != 0:
            # Try remote branch
            rc, stdout, stderr = run(["git", "-C", REPO_DIR, "checkout", "-b", branch, f"origin/{branch}"])
        results["git_checkout"] = {"ok": rc == 0, "branch": branch, "output": stdout or stderr}
        rc, stdout, stderr = run(["git", "-C", REPO_DIR, "pull", "--ff-only"])
        results["git_pull"] = {"ok": rc == 0, "output": stdout or stderr}
    else:
        # Default: pull main
        rc, stdout, stderr = run(["git", "-C", REPO_DIR, "checkout", "main"])
        rc, stdout, stderr = run(["git", "-C", REPO_DIR, "pull", "--ff-only"])
        results["git_pull"] = {"ok": rc == 0, "output": stdout or stderr}

    # Rebuild backend
    rc, stdout, stderr = run(
        ["docker", "build", "-t", "local-backend:latest", f"{REPO_DIR}/backend"],
        timeout=300,
    )
    results["backend"] = {"ok": rc == 0, "output": stderr[-500:] if stderr else stdout[-200:]}

    # Rebuild frontend
    rc, stdout, stderr = run(
        ["docker", "build", "-t", "local-frontend:latest", f"{REPO_DIR}/frontend"],
        timeout=300,
    )
    results["frontend"] = {"ok": rc == 0, "output": stderr[-500:] if stderr else stdout[-200:]}

    all_ok = all(v["ok"] for v in results.values())
    return {
        "status":  "rebuilt" if all_ok else "partial_failure",
        "branch":  branch or "main",
        "results": results,
        "message": f"Images rebuilt from branch '{branch or 'main'}'. Ready to deploy shadow clones." if all_ok
                   else "Some steps failed — check results for details.",
    }


@app.post("/deploy_shadow_clone")
def deploy_shadow_clone(req: DeployRequest, x_api_key: str = Header(...)):
    auth(x_api_key)

    mr = (req.pr_id or req.mr_id or "").replace("/", "-")   # sanitise
    if not mr:
        raise HTTPException(400, "Either pr_id or mr_id must be provided")
    backend_image   = _ensure_image(
        req.backend_image  or f"{REGISTRY}/backend:latest",
        "backend", f"{REPO_DIR}/backend"
    )
    frontend_image  = _ensure_image(
        req.frontend_image or f"{REGISTRY}/frontend:latest",
        "frontend", f"{REPO_DIR}/frontend"
    )

    db_name       = f"db-mr{mr}"
    backend_name  = f"backend-mr{mr}"
    frontend_name = f"frontend-mr{mr}"
    network       = f"net-mr{mr}"
    volume        = f"db-mr{mr}-data"
    seed_file     = f"/tmp/shadow_seed_mr{mr}.sql"
    nginx_file    = f"/tmp/nginx_mr{mr}.conf"

    # Assign ports
    frontend_port = free_port()
    backend_port  = free_port()

    # Write seed SQL
    with open(seed_file, "w") as f:
        f.write(req.seed_sql)

    # Write custom nginx.conf so /api/ proxies to the shadow backend
    with open(nginx_file, "w") as f:
        f.write(shadow_nginx_conf(backend_name))

    try:
        # 0. Force-remove any stale volume so initdb always runs fresh (no production data bleed)
        run(["docker", "volume", "rm", "-f", volume])

        # 1. Network
        rc, _, err = run(["docker", "network", "create", network])
        if rc != 0 and "already exists" not in err:
            raise HTTPException(500, f"Network create failed: {err}")

        # 2. Database — schema first (01), seed second (02), runs in alphabetical order
        rc, _, err = run([
            "docker", "run", "-d",
            "--name",    db_name,
            "--network", network,
            "-e", "POSTGRES_USER=admin",
            "-e", f"POSTGRES_PASSWORD={SHADOW_DB_PASSWORD}",
            "-e", "POSTGRES_DB=patient_records",
            "-v", f"{SCHEMA_SQL}:/docker-entrypoint-initdb.d/01_schema.sql:ro",
            "-v", f"{seed_file}:/docker-entrypoint-initdb.d/02_seed.sql:ro",
            "-v", f"{volume}:/var/lib/postgresql/data",
            "postgres:15-alpine",
        ])
        if rc != 0:
            raise HTTPException(500, f"DB start failed: {err}")

        # 3. Wait for DB
        for attempt in range(15):
            rc, _, _ = run(["docker", "exec", db_name, "pg_isready", "-U", "admin"])
            if rc == 0:
                break
            time.sleep(2)
        else:
            raise HTTPException(500, "Database never became ready")

        # 4. Backend
        rc, _, err = run([
            "docker", "run", "-d",
            "--name",    backend_name,
            "--network", network,
            "-p", f"{backend_port}:8000",
            "-e", f"POSTGRES_HOST={db_name}",
            "-e", "POSTGRES_DB=patient_records",
            "-e", "POSTGRES_USER=admin",
            "-e", f"POSTGRES_PASSWORD={SHADOW_DB_PASSWORD}",
            backend_image,
        ])
        if rc != 0:
            raise HTTPException(500, f"Backend start failed: {err}")

        # The container can be running before Uvicorn has connected to Postgres.
        # Do not expose the clone to smoke tests until its health endpoint is ready.
        backend_health_url = f"http://127.0.0.1:{backend_port}/health"
        for _ in range(30):
            try:
                with urllib.request.urlopen(backend_health_url, timeout=2) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(1)
        else:
            _, backend_logs, backend_err = run(["docker", "logs", "--tail", "50", backend_name])
            raise HTTPException(
                500,
                f"Backend never became healthy: {backend_logs or backend_err}",
            )

        # 5. Frontend — mount custom nginx.conf so /api/ resolves correctly
        rc, _, err = run([
            "docker", "run", "-d",
            "--name",    frontend_name,
            "--network", network,
            "-p", f"{frontend_port}:80",
            "-v", f"{nginx_file}:/etc/nginx/conf.d/default.conf:ro",
            frontend_image,
        ])
        if rc != 0:
            raise HTTPException(500, f"Frontend start failed: {err}")

    except HTTPException:
        _teardown(mr)   # clean up whatever was created before the failure
        raise

    frontend_url = f"http://{VM_IP}:{frontend_port}"
    backend_url  = f"http://{VM_IP}:{backend_port}"
    deployed_at  = datetime.now(timezone.utc)

    # Register the clone for TTL tracking
    with _registry_lock:
        _clone_registry[mr] = {
            "deployed_at":    deployed_at,
            "frontend_url":   frontend_url,
            "backend_url":    backend_url,
            "frontend_port":  frontend_port,
            "backend_port":   backend_port,
        }

    # Post PR/MR comment (silently skipped if GITHUB_TOKEN is not set)
    _post_pr_comment(
        mr,
        f"🚀 **Shadow Clone for PR #{mr} is live!**\n\n"
        f"**Preview URL:** {frontend_url}\n"
        f"**Backend API:** {backend_url}\n\n"
        f"Auto-teardown in {CLONE_TTL_HOURS}h. Use the SRE Monitor or Preview Manager to tear down manually.",
    )

    return {
        "status":        "live",
        "pr_id":         mr,
        "mr_id":         mr,
        "frontend_url":  frontend_url,
        "backend_url":   backend_url,
        "frontend_port": frontend_port,
        "backend_port":  backend_port,
        "network":       network,
        "containers":    [db_name, backend_name, frontend_name],
        "message":       f"Shadow Clone PR #{mr} is live! Open: {frontend_url}",
    }


# ── 3. Teardown ───────────────────────────────────────────────────────────────

class TeardownRequest(BaseModel):
    mr_id: Optional[str] = None
    pr_id: Optional[str] = None


@app.post("/teardown_shadow_clone")
def teardown_shadow_clone(req: TeardownRequest, x_api_key: str = Header(...)):
    auth(x_api_key)
    target_id = req.pr_id or req.mr_id
    if not target_id:
        raise HTTPException(400, "Either pr_id or mr_id must be provided")
    return _teardown(target_id.replace("/", "-"))


# ── 4. Teardown all ───────────────────────────────────────────────────────────

@app.post("/teardown_all")
def teardown_all(x_api_key: str = Header(...)):
    auth(x_api_key)

    with _registry_lock:
        all_mr_ids = list(_clone_registry.keys())

    results = []
    total_ram_freed = 0
    total_containers = 0

    for mr_id in all_mr_ids:
        result = _teardown(mr_id)
        results.append(result)
        gr = result.get("green_report", {})
        total_ram_freed += gr.get("ram_freed_mb", 0)
        total_containers += gr.get("containers_removed", 0)

    return {
        "status":             "all_torn_down",
        "torn_down_mr_ids":   all_mr_ids,
        "clone_count":        len(all_mr_ids),
        "details":            results,
        "total_ram_freed_mb": total_ram_freed,
        "total_containers":   total_containers,
    }


# ── 5. List clones ────────────────────────────────────────────────────────────

@app.get("/list_clones")
def list_clones(x_api_key: str = Header(...)):
    auth(x_api_key)

    now = datetime.now(timezone.utc)
    clones = []

    with _registry_lock:
        for mr_id, entry in _clone_registry.items():
            deployed_at = entry.get("deployed_at")
            if deployed_at and deployed_at.tzinfo is None:
                deployed_at = deployed_at.replace(tzinfo=timezone.utc)

            age_minutes = int((now - deployed_at).total_seconds() / 60) if deployed_at else None
            ttl_minutes = CLONE_TTL_HOURS * 60
            auto_teardown_in = (ttl_minutes - age_minutes) if age_minutes is not None else None

            clones.append({
                "mr_id":                    mr_id,
                "frontend_url":             entry.get("frontend_url"),
                "backend_url":              entry.get("backend_url"),
                "deployed_at":              deployed_at.isoformat() if deployed_at else None,
                "age_minutes":              age_minutes,
                "auto_teardown_in_minutes": max(0, auto_teardown_in) if auto_teardown_in is not None else None,
            })

    return {
        "active_clones": clones,
        "clone_count":   len(clones),
        "ttl_hours":     CLONE_TTL_HOURS,
    }


# ── 6. Status ─────────────────────────────────────────────────────────────────

@app.get("/clone_status/{mr_id}")
def clone_status(mr_id: str, x_api_key: str = Header(...)):
    auth(x_api_key)

    mr         = mr_id.replace("/", "-")
    containers = [f"frontend-mr{mr}", f"backend-mr{mr}", f"db-mr{mr}"]
    statuses   = {}

    for c in containers:
        rc, stdout, _ = run(["docker", "inspect", "--format", "{{.State.Status}}", c])
        statuses[c] = stdout if rc == 0 else "not_found"

    # Get frontend port mapping
    rc, stdout, _ = run([
        "docker", "inspect",
        "--format", "{{range $p,$b := .NetworkSettings.Ports}}{{$p}}->{{(index $b 0).HostPort}} {{end}}",
        f"frontend-mr{mr}",
    ])
    ports = stdout if rc == 0 else "unknown"

    return {
        "mr_id":       mr,
        "containers":  statuses,
        "all_running": all(s == "running" for s in statuses.values()),
        "ports":       ports,
        "frontend_url": f"http://{VM_IP}:{ports.split('->')[1].split()[0]}" if "->" in ports else None,
    }


# ── 7. Cloud Run overflow ─────────────────────────────────────────────────────

class CloudRunRequest(BaseModel):
    mr_id: str


@app.post("/deploy_cloud_run")
def deploy_cloud_run(req: CloudRunRequest, x_api_key: str = Header(...)):
    """Deploy backend to Cloud Run when VM headroom is insufficient."""
    auth(x_api_key)

    if not CLOUD_RUN_PROJECT:
        raise HTTPException(
            400,
            "CLOUD_RUN_PROJECT env var is not set. "
            "Set it to your GCP project ID to enable Cloud Run overflow. "
            "See .env.example for details."
        )

    mr           = req.mr_id.replace("/", "-")
    service_name = f"shadow-backend-mr{mr}"
    image        = f"gcr.io/{CLOUD_RUN_PROJECT}/{service_name}:latest"
    local_tag    = "local-backend:latest"

    results = {}

    # Tag local image for GCR
    rc, stdout, stderr = run(["docker", "tag", local_tag, image])
    results["tag"] = {"ok": rc == 0, "output": stderr or stdout}
    if rc != 0:
        raise HTTPException(500, f"docker tag failed: {stderr}")

    # Push to GCR
    rc, stdout, stderr = run(["docker", "push", image], timeout=300)
    results["push"] = {"ok": rc == 0, "output": stderr[-300:] if stderr else stdout[-200:]}
    if rc != 0:
        raise HTTPException(500, f"docker push failed: {stderr[-300:]}")

    # Deploy to Cloud Run
    rc, stdout, stderr = run([
        "gcloud", "run", "deploy", service_name,
        "--image",   image,
        "--region",  CLOUD_RUN_REGION,
        "--project", CLOUD_RUN_PROJECT,
        "--allow-unauthenticated",
        "--set-env-vars", f"POSTGRES_HOST=localhost,POSTGRES_DB=patient_records,POSTGRES_USER=admin,POSTGRES_PASSWORD={SHADOW_DB_PASSWORD}",
        "--format", "value(status.url)",
    ], timeout=180)
    results["deploy"] = {"ok": rc == 0, "output": stderr[-300:] if stderr else stdout}
    if rc != 0:
        raise HTTPException(500, f"gcloud run deploy failed: {stderr[-300:]}")

    service_url = stdout.strip()
    return {
        "status":      "live_cloud_run",
        "mr_id":       mr,
        "service_url": service_url,
        "image":       image,
        "region":      CLOUD_RUN_REGION,
        "results":     results,
        "message":     f"Backend deployed to Cloud Run: {service_url}",
        "note":        "Cloud Run deployment does not include a DB or frontend. Use for stateless backends only.",
    }


# ── MCP Protocol (JSON-RPC 2.0) ───────────────────────────────────────────────

MCP_TOOLS = [
    {
        "name": "check_vm_headroom",
        "description": (
            "Check if the GCP VM has sufficient RAM (>=512MB) and disk (>=2GB) "
            "to deploy a new shadow clone. Call this first before deploying."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "deploy_shadow_clone",
        "description": (
            "Deploy a fully isolated shadow clone of the app on the GCP VM. "
            "Creates a private Docker network, starts postgres with schema + seed data, "
            "starts backend and frontend on random free ports, and returns the live URL. "
            "Also posts an MR comment with the preview URL if GITLAB_TOKEN is configured."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mr_id": {
                    "type": "string",
                    "description": "The Merge Request ID (e.g. '2'). Used to name all resources.",
                },
                "seed_sql": {
                    "type": "string",
                    "description": "Valid SQL INSERT statements to populate the shadow DB with test data.",
                },
                "backend_image": {
                    "type": "string",
                    "description": "Full Docker image:tag for the backend. Defaults to registry latest.",
                },
                "frontend_image": {
                    "type": "string",
                    "description": "Full Docker image:tag for the frontend. Defaults to registry latest.",
                },
            },
            "required": ["mr_id", "seed_sql"],
        },
    },
    {
        "name": "teardown_shadow_clone",
        "description": (
            "Destroy a shadow clone and reclaim all VM resources: "
            "stops and removes containers, deletes the Docker network and named volume, "
            "and removes temp files. Returns a green_report with RAM freed and CO2 saved."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mr_id": {
                    "type": "string",
                    "description": "The Merge Request ID of the clone to tear down.",
                },
            },
            "required": ["mr_id"],
        },
    },
    {
        "name": "teardown_all",
        "description": "Tear down all active shadow clones at once. Returns list of torn-down MR IDs and total resources freed.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_clones",
        "description": "List all active shadow clones with their URLs, age, and auto-teardown countdown.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "rebuild_images",
        "description": (
            "Pull the latest code from git on the GCP VM and rebuild the backend and frontend "
            "Docker images locally. Call this after pushing new changes so the next shadow clone "
            "deployment uses the latest code. Takes ~60-90 seconds."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "clone_status",
        "description": "Get the current running status and frontend URL of a shadow clone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mr_id": {
                    "type": "string",
                    "description": "The Merge Request ID to check.",
                },
            },
            "required": ["mr_id"],
        },
    },
    {
        "name": "deploy_cloud_run",
        "description": (
            "Deploy the backend to Google Cloud Run when VM headroom is insufficient. "
            "Requires CLOUD_RUN_PROJECT env var to be set on the bridge. "
            "Note: Cloud Run deployment does not include a DB or frontend."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mr_id": {
                    "type": "string",
                    "description": "The Merge Request ID — used to name the Cloud Run service.",
                },
            },
            "required": ["mr_id"],
        },
    },
]


def mcp_result(id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def mcp_error(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


def mcp_text(id: Any, data: dict) -> dict:
    return mcp_result(id, {"content": [{"type": "text", "text": json.dumps(data, default=str)}]})


class DeployProductionRequest(BaseModel):
    pr_id:    Optional[str] = None   # If set, merges this PR before deploying
    mr_id:    Optional[str] = None   # Backwards compatibility alias for pr_id
    merge_pr: bool          = False  # Whether to merge the PR via GitHub API first
    merge_mr: bool          = False  # Backwards compatibility alias for merge_pr


@app.post("/deploy_production")
def deploy_production(req: DeployProductionRequest = DeployProductionRequest(), x_api_key: str = Header(...)):
    """
    Promote a shadow clone to production:
    1. Optionally merge the PR via GitHub API (or GitLab API)
    2. git pull main on the VM
    3. Run all migrations against prod-database
    4. Rebuild images from main
    5. Tag images with production names
    6. Recreate prod-frontend and prod-backend containers
    """
    auth(x_api_key)
    if not PROD_DB_PASSWORD:
        raise HTTPException(400, "POSTGRES_PASSWORD must be configured for production deployment")
    results = {}

    target_id = req.pr_id or req.mr_id
    should_merge = req.merge_pr or req.merge_mr

    # Step 1 — Merge PR via GitHub API (or MR via GitLab API)
    if should_merge and target_id and GITHUB_TOKEN and _REQUESTS_AVAILABLE:
        if "/" in GITHUB_REPOSITORY and not GITHUB_REPOSITORY.startswith("gitlab"):
            url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{target_id}/merge"
            resp = _requests.put(
                url,
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                },
                json={"merge_method": "squash"},
                timeout=30,
            )
            results["merge_pr"] = {"ok": resp.status_code in (200, 201), "status_code": resp.status_code}
            if resp.status_code not in (200, 201):
                results["merge_pr"]["error"] = resp.text[:300]
        else:
            encoded = quote(GITHUB_REPOSITORY, safe="")
            url = f"https://gitlab.com/api/v4/projects/{encoded}/merge_requests/{target_id}/merge"
            resp = _requests.put(
                url,
                headers={"PRIVATE-TOKEN": GITHUB_TOKEN},
                json={"should_remove_source_branch": False, "merge_when_pipeline_succeeds": False},
                timeout=30,
            )
            results["merge_pr"] = {"ok": resp.status_code in (200, 201), "status_code": resp.status_code}
            if resp.status_code not in (200, 201):
                results["merge_pr"]["error"] = resp.text[:300]
    elif should_merge:
        results["merge_pr"] = {"ok": False, "error": "GITHUB_TOKEN not configured or requests unavailable"}

    # Step 2 — Git pull main
    run(["git", "-C", REPO_DIR, "fetch", "--all"])
    run(["git", "-C", REPO_DIR, "checkout", "main"])
    rc, stdout, stderr = run(["git", "-C", REPO_DIR, "pull", "--ff-only"])
    results["git_pull"] = {"ok": rc == 0, "output": stdout or stderr}

    # Step 3 — Run all migrations against prod-database (all use IF NOT EXISTS — idempotent)
    import glob as _glob
    migration_dir = os.path.join(REPO_DIR, "migrations")
    migration_files = sorted(_glob.glob(os.path.join(migration_dir, "*.sql")))
    migration_files = [m for m in migration_files if "rollback" not in os.path.basename(m).lower()]
    migration_results = {}
    for mf in migration_files:
        name = os.path.basename(mf)
        with open(mf) as f:
            sql = f.read()
        rc, stdout, stderr = run([
            "docker", "exec", "prod-database",
            "psql", "-U", "admin", "-d", "patient_records", "-c", sql,
        ])
        migration_results[name] = {"ok": rc == 0, "output": (stdout or stderr)[:200]}
    results["migrations"] = migration_results

    # Step 4 — Rebuild images from main
    rc, _, stderr = run(["docker", "build", "-t", "local-backend:latest", f"{REPO_DIR}/backend"], timeout=300)
    results["build_backend"] = {"ok": rc == 0}
    rc, _, stderr = run(["docker", "build", "-t", "local-frontend:latest", f"{REPO_DIR}/frontend"], timeout=300)
    results["build_frontend"] = {"ok": rc == 0}

    # Step 5 — Tag with production image names
    run(["docker", "tag", "local-backend:latest",  "prod-backend:latest"])
    run(["docker", "tag", "local-frontend:latest", "prod-frontend:latest"])

    # Step 6 — Recreate production containers
    # Stop and remove existing containers
    run(["docker", "stop", "prod-frontend", "prod-backend"])
    run(["docker", "rm",   "prod-frontend", "prod-backend"])

    # Recreate backend
    rc, _, err = run([
        "docker", "run", "-d",
        "--name",    "prod-backend",
        "--network", "time-traveler_prod-net",
        "--network-alias", "backend",
        "-e", "POSTGRES_DB=patient_records",
        "-e", "POSTGRES_USER=admin",
        "-e", f"POSTGRES_PASSWORD={PROD_DB_PASSWORD}",
        "-e", "POSTGRES_HOST=database",
        "prod-backend:latest",
    ])
    results["restart_backend"] = {"ok": rc == 0, "error": err if rc != 0 else None}

    # Recreate frontend
    rc, _, err = run([
        "docker", "run", "-d",
        "--name",    "prod-frontend",
        "--network", "time-traveler_prod-net",
        "-p", "80:80",
        "prod-frontend:latest",
    ])
    results["restart_frontend"] = {"ok": rc == 0, "error": err if rc != 0 else None}

    def _is_ok(v):
        if isinstance(v, dict):
            if "ok" in v:
                return v["ok"]
            return all(_is_ok(x) for x in v.values())
        return True

    all_ok = all(_is_ok(v) for v in results.values() if v is not None)

    return {
        "status":  "deployed" if all_ok else "partial_failure",
        "results": results,
        "message": "Production updated successfully." if all_ok else "Some steps failed — check results.",
    }


def _mcp_auth(request: Request) -> bool:
    auth_header = request.headers.get("Authorization", "")
    api_key_header = request.headers.get("X-API-Key", "")
    bearer = auth_header.removeprefix("Bearer ").strip()
    return bearer == API_KEY or api_key_header == API_KEY


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    if not _mcp_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    body = await request.json()
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    # ── initialize ────────────────────────────────────────────────────────────
    if method == "initialize":
        return mcp_result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "time-traveler-mcp-bridge", "version": "2.0.0"},
        })

    # ── tools/list ────────────────────────────────────────────────────────────
    if method == "tools/list":
        return mcp_result(req_id, {"tools": MCP_TOOLS})

    # ── tools/call ────────────────────────────────────────────────────────────
    if method == "tools/call":
        tool = params.get("name")
        args = params.get("arguments", {})

        if tool == "check_vm_headroom":
            return mcp_text(req_id, system_headroom())

        if tool == "deploy_shadow_clone":
            if "mr_id" not in args or "seed_sql" not in args:
                return mcp_error(req_id, -32602, "mr_id and seed_sql are required")
            req_obj = DeployRequest(**args)
            try:
                result = deploy_shadow_clone(req_obj, x_api_key=API_KEY)
                return mcp_text(req_id, result)
            except HTTPException as e:
                return mcp_error(req_id, -32000, e.detail)

        if tool == "teardown_shadow_clone":
            if "mr_id" not in args:
                return mcp_error(req_id, -32602, "mr_id is required")
            result = _teardown(args["mr_id"].replace("/", "-"))
            return mcp_text(req_id, result)

        if tool == "teardown_all":
            with _registry_lock:
                all_mr_ids = list(_clone_registry.keys())
            results = []
            for mr_id in all_mr_ids:
                results.append(_teardown(mr_id))
            return mcp_text(req_id, {
                "status": "all_torn_down",
                "torn_down_mr_ids": all_mr_ids,
                "details": results,
            })

        if tool == "list_clones":
            now = datetime.now(timezone.utc)
            clones = []
            with _registry_lock:
                for mr_id, entry in _clone_registry.items():
                    deployed_at = entry.get("deployed_at")
                    if deployed_at and deployed_at.tzinfo is None:
                        deployed_at = deployed_at.replace(tzinfo=timezone.utc)
                    age_minutes = int((now - deployed_at).total_seconds() / 60) if deployed_at else None
                    ttl_minutes = CLONE_TTL_HOURS * 60
                    auto_teardown_in = (ttl_minutes - age_minutes) if age_minutes is not None else None
                    clones.append({
                        "mr_id": mr_id,
                        "frontend_url": entry.get("frontend_url"),
                        "age_minutes": age_minutes,
                        "auto_teardown_in_minutes": max(0, auto_teardown_in) if auto_teardown_in is not None else None,
                    })
            return mcp_text(req_id, {"active_clones": clones, "clone_count": len(clones)})

        if tool == "rebuild_images":
            result = rebuild(x_api_key=API_KEY)
            return mcp_text(req_id, result)

        if tool == "clone_status":
            if "mr_id" not in args:
                return mcp_error(req_id, -32602, "mr_id is required")
            mr = args["mr_id"].replace("/", "-")
            containers = [f"frontend-mr{mr}", f"backend-mr{mr}", f"db-mr{mr}"]
            statuses = {}
            for c in containers:
                rc, stdout, _ = run(["docker", "inspect", "--format", "{{.State.Status}}", c])
                statuses[c] = stdout if rc == 0 else "not_found"
            rc, stdout, _ = run([
                "docker", "inspect",
                "--format", "{{range $p,$b := .NetworkSettings.Ports}}{{$p}}->{{(index $b 0).HostPort}} {{end}}",
                f"frontend-mr{mr}",
            ])
            ports = stdout if rc == 0 else "unknown"
            return mcp_text(req_id, {
                "mr_id": mr,
                "containers": statuses,
                "all_running": all(s == "running" for s in statuses.values()),
                "frontend_url": f"http://{VM_IP}:{ports.split('->')[1].split()[0]}" if "->" in ports else None,
            })

        if tool == "deploy_cloud_run":
            if "mr_id" not in args:
                return mcp_error(req_id, -32602, "mr_id is required")
            req_obj = CloudRunRequest(mr_id=args["mr_id"])
            try:
                result = deploy_cloud_run(req_obj, x_api_key=API_KEY)
                return mcp_text(req_id, result)
            except HTTPException as e:
                return mcp_error(req_id, -32000, e.detail)

        return mcp_error(req_id, -32601, f"Unknown tool: {tool}")

    return mcp_error(req_id, -32601, f"Unknown method: {method}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print(f"MCP Bridge starting on 0.0.0.0:{MCP_PORT}")
    print(f"VM Public IP:     {VM_IP}")
    print(f"Registry:         {REGISTRY}")
    print(f"Clone TTL:        {CLONE_TTL_HOURS}h")
    print(f"GitHub PR comments: {'enabled' if GITHUB_TOKEN else 'disabled (set GITHUB_TOKEN)'}")
    print(f"Cloud Run:        {'enabled (project=' + CLOUD_RUN_PROJECT + ')' if CLOUD_RUN_PROJECT else 'disabled (set CLOUD_RUN_PROJECT)'}")
    print(f"MCP endpoint:     http://{VM_IP}:{MCP_PORT}/mcp")
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT)
