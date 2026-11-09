# gdrive-mcp

Google Drive MCP server for Smart Node. Version 0.1.0.

This package is independently versioned and installed by the Smart Node
integration provisioner (`integration provision gdrive`). It does NOT live
in the kernel companion directory.

## Purpose

Exposes Google Drive and Google Sheets to MCP clients (agents) over stdio.
Uses a service account whose credentials live in the node secret store,
never on disk or in environment variables.

## Tools

| Tool | Description |
|------|-------------|
| `gdrive_list_files` | List files visible to the service account |
| `gdrive_get_metadata` | Full metadata for one file or folder |
| `gdrive_download_file` | Download text content (Docs/Sheets exported as plain text/CSV) |
| `gdrive_upload_file` | Update an existing non-Google-native file |
| `gdrive_read_sheet` | Read a Google Sheet as a JSON array of row objects |
| `gdrive_sheet_list_tabs` | List all tabs in a spreadsheet |
| `gdrive_sheet_write_range` | Write values to a specific range |
| `gdrive_sheet_append_rows` | Append rows to a sheet tab |
| `gdrive_sheet_add_tab` | Add a new tab to a spreadsheet |

## Credentials

At startup the server calls core-api through its Unix socket:

    GET /api/secrets/resolve?ref=google-cloud-service-account

The kernel proves the agent OS user and core-api requires both the `secrets:
r--` tool grant and `google-cloud-service-account` (or its namespace) in that
agent's `secret_refs`. The ref can be overridden with `GDRIVE_SECRET_REF`; the
socket with `SN_CORE_API_SOCKET`. `SN_SECRET_STORE` remains only for isolated
test beds without the node socket and is not an authenticated production path.

## Installation

Managed by `integration provision gdrive`. The provisioner:
1. Creates /srv/integrations/gdrive/venv/
2. pip installs gdrive-mcp==0.1.1 from /srv/integrations/packages/
3. Updates sn_config.mcp_servers status to available

Entry point after install:

    /srv/integrations/gdrive/venv/bin/gdrive-mcp

## Building a wheel

    pip install build
    cd integrations/gdrive-mcp
    python -m build --wheel --outdir /srv/integrations/packages/

## Updating

1. Increment version in pyproject.toml.
2. Build a new wheel and place it in /srv/integrations/packages/ on the node.
3. Update version_pin in sn_config.mcp_servers config for the gdrive slug.
4. Run: integration provision gdrive

No kernel release is needed.
