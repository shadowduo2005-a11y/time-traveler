# Time-Traveler Project Guide

## 1. What this project does

Time-Traveler validates database changes in disposable Docker environments before
they reach production. A LangGraph workflow reads the PostgreSQL schema and
migrations, scores migration risk, generates dependency-safe seed data, deploys an
isolated clone, and runs live API smoke tests.

The default command is deliberately non-mutating. A real shadow environment is
created only when `--execute` is supplied.

## 2. Architecture

```text
Developer / CI
      │
      ▼
LangGraph StateGraph (flows/orchestrator.py)
      │
      ├── Shadow Architect
      │     Parses schema, migrations, services, columns, and foreign keys
      │
      ├── Migration Auditor
      │     Scores risk and generates rollback SQL
      │     DO NOT DEPLOY ──────────────────────────────────────► END
      │
      ├── Data Smith
      │     Generates FK-safe PostgreSQL seed data
      │     Dry run ─────────────────────────────────────────────► END
      │
      ├── SRE Monitor
      │     Calls the authenticated MCP bridge
      │
      └── Smoke Tester
            GET health → GET list → POST create → DELETE cleanup
```

The bridge starts three isolated containers for each shadow clone:

```text
net-mr<id>
├── db-mr<id>        PostgreSQL with a private named volume
├── backend-mr<id>   FastAPI on a random host port
└── frontend-mr<id>  Nginx on a random host port
```

## 3. Requirements

- Python 3.11–3.13
- Docker Desktop or Docker Engine with Compose v2
- At least 512 MB of Docker memory and 2 GB free disk
- macOS or Linux for local development

GitHub credentials, a cloud VM, and a container registry are optional. They are
needed only for PR comments, remote rebuilds, merging, or remote hosting.

## 4. Repository layout

```text
.
├── backend/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── db/schema.sql
├── flows/
│   ├── __init__.py
│   └── orchestrator.py
├── frontend/
│   ├── Dockerfile
│   ├── index.html
│   └── nginx.conf
├── migrations/
├── scripts/setup_mcp_service.sh
├── tests/test_orchestrator.py
├── .env.example
├── docker-compose.yml
├── mcp_bridge.py
└── requirements.txt
```

Application, orchestration, bridge, and test logic are Python. SQL files are kept
as SQL because PostgreSQL executes them directly; Docker Compose and Nginx retain
their native configuration formats.

## 5. Local application setup

Create the Python environment:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Build and start the main application:

```bash
docker compose up --build -d
docker compose ps
```

Local endpoints:

- Frontend: `http://127.0.0.1/`
- API through Nginx: `http://127.0.0.1/api/health`
- API documentation is available on a directly published shadow backend at `/docs`.

Stop the main stack without deleting its database:

```bash
docker compose down
```

To also remove the local database volume:

```bash
docker compose down -v
```

## 6. Configuration

Copy the example and edit local values:

```bash
cp .env.example .env
```

Minimum local configuration:

```env
MCP_API_KEY=replace-with-a-random-local-key
POSTGRES_PASSWORD=replace-with-a-strong-local-password
SHADOW_DB_PASSWORD=
VM_PUBLIC_IP=127.0.0.1
MCP_PORT=8888
BRIDGE_URL=http://127.0.0.1:8888
MCP_BRIDGE_URL=http://127.0.0.1:8888
REGISTRY=
GITHUB_TOKEN=
GITHUB_REPOSITORY=
CLONE_TTL_HOURS=2
REPO_DIR=/absolute/path/to/time-traveler
SCHEMA_SQL=/absolute/path/to/time-traveler/db/schema.sql
```

Never commit `.env`. It is excluded by `.gitignore`.

## 7. Start the MCP bridge

```bash
set -o allexport
source .env
set +o allexport
.venv/bin/python mcp_bridge.py
```

Verify it from another terminal:

```bash
curl http://127.0.0.1:8888/health
curl -H "X-API-Key: $MCP_API_KEY" \
  http://127.0.0.1:8888/check_vm_headroom
```

For a Linux VM, `scripts/setup_mcp_service.sh` installs the bridge as a systemd
service. Review its paths and environment before running it with `sudo`.

## 8. Run LangGraph

### Safe dry run

This analyzes every SQL migration, applies the safety gate, and generates seed SQL.
It does not call Docker or the bridge.

```bash
.venv/bin/python flows/orchestrator.py 42
```

Successful dry-run status:

```text
ready_for_deployment
```

### Live local shadow clone

If the directory has no Git metadata, use `--skip-rebuild`:

```bash
set -o allexport
source .env
set +o allexport

.venv/bin/python flows/orchestrator.py 42 \
  --bridge-url "$MCP_BRIDGE_URL" \
  --api-key "$MCP_API_KEY" \
  --skip-rebuild \
  --execute
```

For a real cloned repository, provide the PR branch and allow rebuilding:

```bash
.venv/bin/python flows/orchestrator.py 42 \
  --branch feature/my-change \
  --execute
```

Successful live status:

```text
pipeline_success
```

The JSON output includes the random frontend/backend URLs and all smoke-test results.

## 9. Safety behavior

Each migration begins at score 10. The auditor applies these deductions:

| Finding | Penalty |
|---|---:|
| `DROP TABLE` or `DROP COLUMN` | -3 |
| Added `NOT NULL` column without `DEFAULT` | -2 |
| Missing `IF NOT EXISTS` | -1 |
| Foreign key without an index | -1 |
| Missing `BEGIN`/`COMMIT` | -1 |

Scores map to:

- 8–10: `SAFE`
- 5–7: `CAUTION`
- 1–4: `DO NOT DEPLOY`

`DO NOT DEPLOY` ends the graph before seed generation or bridge calls. Deployment
also stops on schema errors, insufficient capacity, failed image builds, bridge
errors, or an unhealthy backend.

## 10. Tests

Run all graph regression tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The suite covers:

1. A successful dry run and FK-safe seed generation.
2. An unsafe migration stopping before deployment.
3. The complete bridge contract and four smoke-test stages.

Additional checks:

```bash
.venv/bin/python -m compileall -q flows tests mcp_bridge.py backend
docker compose config --quiet
docker compose build
```

## 11. GitHub preparation

Files intentionally excluded from Git include `.env`, virtual environments,
Python bytecode, and IDE metadata.

This downloaded directory may not contain `.git`. If necessary:

```bash
git init
git branch -M main
git add .
git commit -m "feat: LangGraph shadow-clone pipeline"
git remote add origin https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
git push -u origin main
```

The included GitHub Actions workflow runs Python tests and verifies both Docker
images. It does not deploy infrastructure or require repository secrets.

## 12. Operational notes

- Shadow clones auto-expire according to `CLONE_TTL_HOURS` while the bridge runs.
- Every clone has an isolated network, database volume, and random host ports.
- Production deployment is not automatic.
- GitHub comments and merging remain disabled unless valid GitHub credentials are
  explicitly configured.
- Treat `MCP_API_KEY` as a secret on shared or remotely reachable hosts.
