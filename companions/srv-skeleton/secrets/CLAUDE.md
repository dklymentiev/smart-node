# /srv/secrets — secrets convention

**Rule: nothing is hardcoded.** All credentials live here as `.env` files, or in
the secrets manager (Smart Node uses sops+age via `core-api`). Code reads them
through `/srv/scripts/lib/env.sh`.

- Only `*.example` templates may be committed. Real `*.env` files are gitignored.
- One `.env` per concern; never a single global secrets blob.
- Agents receive only the secrets their grant allows.

See `machine-identity.env.example` for the CI/agent-credential template.
