# Working with the DOCI source

DOCI is shipped here as open source, but the upstream is maintainer-published
and does not accept outside pull or merge requests. Forks are welcome; the
workflow below is for developing and testing changes in your fork.

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Boot the dev stack: `docker compose -f docker-compose.dev.yml up -d`
   (UI on <http://localhost:8080>, embedded Postgres on host port 5433,
   migrations and seed indexing handled by the entrypoint).
4. Run the integration suite once to confirm the stack is healthy:
   `make api-test`.

If you prefer to develop outside Docker, see "Running outside Docker"
below — note that DOCI is an Apache + mod_rewrite app, so the built-in
PHP server (`php -S`) does NOT work as a substitute.

## Development Setup

### Requirements

- PHP 8.2+ (CI matrix runs 8.2 and 8.3)
- PostgreSQL 14+
- Docker + Compose v2 (the supported path)

### Running locally

```bash
# Dev stack -- embedded Postgres, auto-auth as `dev`
docker compose -f docker-compose.dev.yml up -d

# Verify
curl -fsS http://localhost:8080/api/health.php | jq .
make api-test
```

The dev compose file bind-mounts `./files` only; PHP source is baked
into the image. Iterate by either rebuilding (`docker compose build`)
or by `docker cp` of the changed file into the running container for
quick checks.

### Running outside Docker

You need Apache 2.4 + PHP 8.2 + `mod_rewrite` against a reachable
Postgres 14+. The repo's `apache-config.conf` and `.htaccess` are
mandatory — DOCI is a multi-route app and will not work behind the
built-in PHP server.

## Code Style

- Follow PSR-12 coding standards.
- Use meaningful variable and function names.
- Add PHPDoc comments for public functions in `lib/` and `api/`.
- Use type hints for parameters and return types.

## Maintaining changes in a fork

1. Create a feature branch from `main`.
2. Make your changes.
3. Add/update tests as needed; for any HTTP/MCP/subprocess change, the
   Live Benchmark Gate in [`docs/06-testing-strategy.md`](docs/06-testing-strategy.md)
   applies before the next release tag.
4. Run `composer test` (unit) and `make api-test` (integration).
5. Run `composer analyse` for static analysis.
6. Keep the change in your fork and record which checks you ran. Upstream does
   not accept outside pull or merge requests.

### Commit Messages

Use clear, descriptive commit messages:

- `feat: add document tagging support`
- `fix: resolve path traversal in file upload`
- `docs: update API documentation`
- `test: add unit tests for validation`

## Documenting issues in a fork

When recording bugs for your fork, include:

- PHP version
- PostgreSQL version
- Steps to reproduce
- Expected vs actual behavior
- Relevant log output

## Security

If you discover a security vulnerability, please email security@klymentiev.com instead of creating a public issue. See [SECURITY.md](SECURITY.md) for details.

## License

Changes you distribute as part of DOCI remain subject to the MIT License.
