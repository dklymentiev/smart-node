# hitl-upstream -- HITL decision server and caller

A pinned copy of the HITL decision service that the `hitl` companion installs.
A caller (here, the node) creates a request; the person it is addressed to
opens a private link, reviews it in the browser and answers with a passkey
(WebAuthn). The answer comes back signed, and the caller verifies it before
acting on it.

Licence: Apache License 2.0, see `LICENSE` and `NOTICE.md`. The browser
libraries under `server/public/assets/vendor/` carry their own licence files
(`LICENSE-libsodium`, `pdfjs/LICENSE`). Smart Node adapter code is not here;
it lives in `companions/hitl/`.

## Layout

| Path | What it is |
|---|---|
| `server/hitl/app.py` | WSGI app (`hitl.app:application`): route table, static client files, HTML shells |
| `server/hitl/api.py` | JSON API: enrolment, recipient keys, inbox, blobs, requests and responses under `/api/v1/` |
| `server/hitl/lib.py` | Config, SQLite storage, encoding, WebAuthn assertion checks |
| `server/schema.sql` | SQLite schema (version 3), applied idempotently on each connection |
| `server/public/` | Browser client (`app.js`, `app.css`); request content is decrypted and rendered client-side |
| `server/bin/admin.py` | Operator commands: `add-caller`, `set-caller-theme`, `enrol-link`, `reset-recipient`, `list` |
| `caller/hitl.py` | Caller CLI: `keygen`, `pin`, `ask`, `refresh`, `check`, `consume`; prints one JSON object per command |
| `deploy/gunicorn.conf.py` | gunicorn settings (bind from `HITL_BIND` / `PORT`) |
| `requirements.txt` | Hash-pinned dependencies (cryptography, PyNaCl, gunicorn) |

## How it is installed on a node

`companions/hitl.sh` syncs this directory to `/opt/smart-node/hitl`, builds a
venv from `requirements.txt`, generates the node's caller key with
`caller/hitl.py keygen`, registers it with `server/bin/admin.py add-caller`,
and runs the server as `smart-node-hitl.service` under gunicorn. Its settings
come from `/etc/smart-node/hitl.env` (`HITL_ORIGIN`, `HITL_DB`, `HITL_STATE`,
`PORT`). To give a person a way to answer, an operator issues an enrolment
link with `admin.py enrol-link <recipient_id> <display_name>`.

Node-specific code belongs in `companions/hitl/`, not here (see `NOTICE.md`).

There are no tests in this directory. `companions/hitl/tests/` holds the
adapter tests and an end-to-end script that drives this server on an
installed node.
