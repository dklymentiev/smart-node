# Working with the Planner source

Planner is open source, but the upstream is maintainer-published and does not
accept outside pull or merge requests. Forks are welcome. Version 0.1.0 has a
working pipeline state machine, REST/MCP/CLI/UI parity, dashboard, and test
suite. See [docs/01-vision.md](docs/01-vision.md) for scope and non-goals.

## Where to start

1. Read [docs/01-vision.md](docs/01-vision.md) to understand the product.
2. Fork the repository and describe the intended change in your fork.
3. Keep scope decisions aligned with the vision, especially its Non-Goals.

## Development setup

```bash
git clone <this repo>
cd planner-oss
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d db
pytest tests/ -q -k "not integration and not live"
```

For the full suite against a real PostgreSQL, see the **Tests** section of [README.md](README.md).

## Changes in a fork

- Keep one logical change per commit or branch.
- Include or update tests. The unit suite must stay green; integration changes need a `PLANNER_TEST_DSN` run.
- Update `CHANGELOG.md` under `[Unreleased]`.
- Keep interface parity: any operation added in one interface (CLI / REST / MCP / UI) must be available in all four.
- Record how the change aligns with the vision. Upstream does not accept
  outside pull or merge requests.

## Reporting bugs

Record non-security bugs in your fork. For security vulnerabilities, see [SECURITY.md](SECURITY.md).
