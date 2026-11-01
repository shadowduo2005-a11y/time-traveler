# Time-Traveler

Time-Traveler is a Python and LangGraph system for analyzing database migrations,
creating isolated Docker shadow environments, and validating them before release.

The working graph is:

```text
Shadow Architect → Migration Auditor → Data Smith → SRE Monitor → Smoke Tester
                         │
                         └── unsafe migration → stop
```

## Quick start

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
docker compose up --build -d
```

Open the application at [http://localhost](http://localhost).

Run a safe, local LangGraph analysis without deploying a shadow clone:

```bash
.venv/bin/python flows/orchestrator.py 1
```

For bridge configuration, live shadow-clone execution, testing, architecture,
environment variables, and GitHub setup, read [PROJECT_GUIDE.md](PROJECT_GUIDE.md).

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
docker compose config --quiet
```

## Main components

- `flows/orchestrator.py` — executable LangGraph `StateGraph`
- `mcp_bridge.py` — authenticated Docker shadow-clone bridge
- `backend/main.py` — FastAPI application
- `frontend/index.html` — browser UI served by Nginx
- `db/schema.sql` and `migrations/` — PostgreSQL schema history
- `tests/test_orchestrator.py` — graph and safety-gate regression tests

## License

MIT. See [LICENSE](LICENSE).
