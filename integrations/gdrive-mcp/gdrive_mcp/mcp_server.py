#!/usr/bin/env python3
"""
Google Drive MCP Server for Smart Node

Exposes Google Drive and Google Sheets as MCP tools over stdio.
Credentials come from the node secret store at startup -- never from disk
or environment variables.

Usage:
    gdrive-mcp               # via installed entry point (stdio)
    python -m gdrive_mcp.mcp_server

Environment (optional overrides):
    SN_CORE_API_SOCKET -- core-api unix socket (default /run/smart-node/api/core-api.sock)
    SN_AGENT_SLUG      -- runtime agent identity (the kernel remains authoritative)
    GDRIVE_SECRET_REF -- secret ref (default: google-cloud-service-account)

The service account sees only objects shared with its email address.
It does not use domain-wide delegation.
"""

import asyncio
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import httpx

# ──────────────────────────────────────────
# Secret store
# ──────────────────────────────────────────

SECRET_STORE = os.environ.get("SN_SECRET_STORE", "").rstrip("/")
SECRET_SOCKET = os.environ.get("SN_CORE_API_SOCKET", "/run/smart-node/api/core-api.sock")
SECRET_REF   = os.environ.get("GDRIVE_SECRET_REF", "google-cloud-service-account")


def _resolve_secret(ref: str) -> str:
    """Resolve a secret from the node secret store. Exits on failure."""
    # The store takes the ref as a QUERY PARAMETER, not a path segment.
    # WHY this comment exists: the path form answers 405, the module then
    # reads that as a resolution failure and exits 1, and the message on the
    # way out says "check that the ref exists" -- which sends the reader
    # hunting for a missing secret that is in fact present. (See the agent instructions.)
    url = f"http://localhost/api/secrets/resolve?ref={urllib.parse.quote(ref)}"
    try:
        if os.path.exists(SECRET_SOCKET):
            transport = httpx.HTTPTransport(uds=SECRET_SOCKET)
            with httpx.Client(transport=transport, timeout=10) as client:
                resp = client.get(
                    url,
                    headers={"Remote-User": os.environ.get("SN_AGENT_SLUG", "")},
                )
                resp.raise_for_status()
                return resp.json()["value"]
        # Test-bed compatibility only. Production uses the socket so the caller
        # cannot invent its identity.
        if not SECRET_STORE:
            raise RuntimeError(f"core-api socket absent: {SECRET_SOCKET}")
        req = urllib.request.Request(
            f"{SECRET_STORE}/api/secrets/resolve?ref={urllib.parse.quote(ref)}")
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read())["value"]
    except httpx.HTTPStatusError as exc:
        print(
            f"ERROR: secret store returned {exc.response.status_code} for ref '{ref}'. "
            "Check the agent's secrets grant and secret_refs allowlist.",
            file=sys.stderr,
        )
        sys.exit(1)
    except urllib.error.HTTPError as exc:
        print(
            f"ERROR: secret store returned {exc.code} for ref '{ref}'. "
            "Check that the ref exists and that this test-bed caller is authenticated.",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(
            f"ERROR: cannot reach secret store ({url}): {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


# ──────────────────────────────────────────
# Google API clients -- built at module load
# ──────────────────────────────────────────

def _build_clients():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build as gapi_build
    except ImportError as exc:
        print(
            f"ERROR: required library missing: {exc}. "
            "Run: pip install google-auth google-api-python-client",
            file=sys.stderr,
        )
        sys.exit(1)

    raw = _resolve_secret(SECRET_REF)
    try:
        key_data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: secret '{SECRET_REF}' is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds = service_account.Credentials.from_service_account_info(key_data, scopes=scopes)
    drive   = gapi_build("drive",  "v3", credentials=creds, cache_discovery=False)
    sheets  = gapi_build("sheets", "v4", credentials=creds, cache_discovery=False)
    sa_email = key_data.get("client_email", "unknown")
    print(f"INFO: authenticated as {sa_email}", file=sys.stderr)
    return drive, sheets


_drive, _sheets = _build_clients()

# ──────────────────────────────────────────
# MCP server
# ──────────────────────────────────────────

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Google Drive",
    instructions=(
        "Tools for reading and writing files in Google Drive and Google Sheets. "
        "The service account sees only files explicitly shared with it. "
        "Use gdrive_list_files to discover available files. "
        "For Google Sheets: use gdrive_read_sheet to read rows as JSON; "
        "gdrive_sheet_list_tabs to see all tabs; "
        "gdrive_sheet_write_range to write a specific range; "
        "gdrive_sheet_append_rows to add rows; "
        "gdrive_sheet_add_tab to create a new tab. "
        "Do NOT use gdrive_upload_file on a Google Sheet -- it will be refused. "
        "gdrive_upload_file is for non-Google-native files only (text, CSV, JSON, etc.)."
    ),
)

# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

_EXPORTABLE_MIME = {
    "application/vnd.google-apps.document":     "text/plain",
    "application/vnd.google-apps.spreadsheet":  "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.drawing":      "image/svg+xml",
}


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}TB"


# ──────────────────────────────────────────
# Tools
# ──────────────────────────────────────────

@mcp.tool()
async def gdrive_list_files(
    query: str = "",
    page_size: int = 50,
    order_by: str = "modifiedTime desc",
) -> str:
    """List files visible to the service account in Google Drive.

    Args:
        query: Optional Drive query string (e.g. "name contains 'report'" or
               "mimeType='application/vnd.google-apps.spreadsheet'").
               Leave blank to list all accessible files.
        page_size: Number of results (default 50, max 1000).
        order_by: Sort field (default 'modifiedTime desc').

    Returns a table of file id, name, type, size, and last modified time.
    """
    fields = "files(id,name,mimeType,size,modifiedTime,parents,webViewLink)"
    q = query.strip() or None

    def _call():
        return _drive.files().list(
            q=q,
            pageSize=min(page_size, 1000),
            orderBy=order_by,
            fields=fields,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

    try:
        resp = await asyncio.to_thread(_call)
    except Exception as exc:
        return f"ERROR: Drive API error: {exc}"

    files = resp.get("files", [])
    if not files:
        return "No files found. Check that objects are shared with the service account."

    lines = [f"{'ID':<33} {'Name':<40} {'Type':<15} {'Size':>8}  Modified"]
    lines.append("-" * 110)
    for f in files:
        mime  = f.get("mimeType", "")
        short = mime.split(".")[-1] if "google-apps" in mime else mime.split("/")[-1]
        size  = _fmt_size(int(f["size"]) if f.get("size") else None)
        mod   = (f.get("modifiedTime") or "")[:10]
        lines.append(f"{f['id']:<33} {f['name'][:40]:<40} {short[:15]:<15} {size:>8}  {mod}")

    return "\n".join(lines)


@mcp.tool()
async def gdrive_get_metadata(file_id: str) -> str:
    """Get full metadata for a Drive file or folder.

    Args:
        file_id: Google Drive file ID (from gdrive_list_files).
    """
    def _call():
        return _drive.files().get(
            fileId=file_id,
            fields="id,name,mimeType,size,createdTime,modifiedTime,parents,"
                   "webViewLink,sharingUser,owners,capabilities",
            supportsAllDrives=True,
        ).execute()

    try:
        meta = await asyncio.to_thread(_call)
    except Exception as exc:
        return f"ERROR: {exc}"

    lines = [
        f"id:           {meta.get('id')}",
        f"name:         {meta.get('name')}",
        f"type:         {meta.get('mimeType')}",
        f"size:         {_fmt_size(int(meta['size']) if meta.get('size') else None)}",
        f"created:      {(meta.get('createdTime') or '')[:19]}",
        f"modified:     {(meta.get('modifiedTime') or '')[:19]}",
        f"webViewLink:  {meta.get('webViewLink', 'n/a')}",
    ]
    owners = meta.get("owners", [])
    if owners:
        lines.append(f"owners:       {', '.join(o.get('emailAddress','?') for o in owners)}")
    caps = meta.get("capabilities", {})
    can_edit = caps.get("canEdit", False)
    lines.append(f"canEdit:      {can_edit}")
    return "\n".join(lines)


@mcp.tool()
async def gdrive_download_file(file_id: str, max_bytes: int = 1_048_576) -> str:
    """Download text content of a Drive file.

    For Google Docs/Sheets/Slides the content is exported as plain text or CSV.
    For binary files only the first max_bytes bytes are returned as UTF-8 (with
    replacement for non-decodable bytes). Increase max_bytes cautiously.

    Args:
        file_id:   Google Drive file ID.
        max_bytes: Byte limit on returned content (default 1 MB).
    """
    def _get_meta():
        return _drive.files().get(
            fileId=file_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()

    try:
        meta = await asyncio.to_thread(_get_meta)
    except Exception as exc:
        return f"ERROR getting metadata: {exc}"

    mime = meta.get("mimeType", "")
    name = meta.get("name", file_id)

    def _download():
        buf = io.BytesIO()
        if mime in _EXPORTABLE_MIME:
            export_mime = _EXPORTABLE_MIME[mime]
            req = _drive.files().export_media(fileId=file_id, mimeType=export_mime)
        else:
            req = _drive.files().get_media(fileId=file_id, supportsAllDrives=True)
        from googleapiclient.http import MediaIoBaseDownload
        dl = MediaIoBaseDownload(buf, req, chunksize=max_bytes)
        done = False
        while not done:
            _, done = dl.next_chunk()
            if buf.tell() >= max_bytes:
                break
        return buf.getvalue()[:max_bytes]

    try:
        raw = await asyncio.to_thread(_download)
    except Exception as exc:
        return f"ERROR downloading '{name}': {exc}"

    text = raw.decode("utf-8", errors="replace")
    truncated = len(raw) >= max_bytes
    header = f"File: {name} ({mime})\n{'(truncated at ' + str(max_bytes) + ' bytes)' if truncated else ''}\n\n"
    return header + text


@mcp.tool()
async def gdrive_upload_file(
    file_id: str,
    name: str = "",
    content: str = "",
    mime_type: str = "text/plain",
) -> str:
    """Update an existing non-Google-native file in Google Drive by replacing its content.

    This tool replaces the full content of an existing file with the supplied text.
    It only works for non-Google-native files (e.g. text/plain, text/csv, application/json).
    Calling it on a Google-native file (Sheets, Docs, Slides) would silently destroy
    all structure (tabs, formulas, formatting) -- this tool refuses such calls.
    Use gdrive_sheet_write_range or gdrive_sheet_append_rows to write to a Google Sheet.

    NOTE: creating new files is not supported on this service account (no storage quota)
    and will 403. This tool only updates existing files.

    Args:
        file_id:   Existing Drive file ID to update (required).
        name:      New file name (optional; leave blank to keep the existing name).
        content:   Text content to write (replaces the full file content).
        mime_type: MIME type of the content being written (default text/plain).
    """
    if not file_id:
        return "ERROR: file_id is required. This tool updates existing files only."
    if not content:
        return "ERROR: provide content to upload."

    # Fetch remote metadata to guard against clobbering Google-native files.
    _NATIVE_MIME = set(_EXPORTABLE_MIME.keys())

    def _get_meta():
        return _drive.files().get(
            fileId=file_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()

    try:
        meta = await asyncio.to_thread(_get_meta)
    except Exception as exc:
        return f"ERROR fetching metadata before upload: {exc}"

    remote_mime = meta.get("mimeType", "")
    if remote_mime in _NATIVE_MIME:
        if "spreadsheet" in remote_mime:
            suggestion = "gdrive_sheet_write_range or gdrive_sheet_append_rows"
        elif "document" in remote_mime:
            suggestion = "a Docs-aware tool (none yet on this server)"
        else:
            suggestion = "an appropriate native-file tool"
        return (
            f"REFUSED: '{meta.get('name')}' is a Google-native file (mimeType: {remote_mime}). "
            f"Uploading a text blob would silently destroy all structure (tabs, formulas, formatting). "
            f"Use {suggestion} instead."
        )

    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type, resumable=False)

    def _update():
        body = {}
        if name:
            body["name"] = name
        return _drive.files().update(
            fileId=file_id,
            body=body,
            media_body=media,
            fields="id,name,webViewLink",
            supportsAllDrives=True,
        ).execute()

    try:
        result = await asyncio.to_thread(_update)
    except Exception as exc:
        return f"ERROR: {exc}"

    return (
        f"File updated.\n"
        f"id:          {result.get('id')}\n"
        f"name:        {result.get('name')}\n"
        f"webViewLink: {result.get('webViewLink', 'n/a')}"
    )


@mcp.tool()
async def gdrive_sheet_list_tabs(spreadsheet_id: str) -> str:
    """List all tabs (sheets) in a Google Spreadsheet.

    Args:
        spreadsheet_id: Google Drive file ID of the spreadsheet.

    Returns a list of tab names, their index order, and row/column counts.
    """
    def _list():
        return _sheets.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties(sheetId,title,index,sheetType,gridProperties)",
        ).execute()

    try:
        resp = await asyncio.to_thread(_list)
    except Exception as exc:
        return f"ERROR: {exc}"

    sheets = resp.get("sheets", [])
    if not sheets:
        return "No sheets found."

    lines = [f"{'Index':<6} {'Title':<40} {'Rows':>6} {'Cols':>6}"]
    lines.append("-" * 62)
    for s in sheets:
        p = s.get("properties", {})
        gp = p.get("gridProperties", {})
        lines.append(
            f"{p.get('index', ''):<6} {p.get('title', '')[:40]:<40} "
            f"{gp.get('rowCount', '?'):>6} {gp.get('columnCount', '?'):>6}"
        )
    return "\n".join(lines)


@mcp.tool()
async def gdrive_sheet_write_range(
    spreadsheet_id: str,
    tab: str,
    range: str,
    values: list,
) -> str:
    """Write values to a specific range in a Google Sheet tab.

    Writes to exactly the cells specified. Cells outside the range are not touched.
    Use A1 notation for the range (e.g. "A1:C3"). Each inner list is one row.

    Args:
        spreadsheet_id: Google Drive file ID of the spreadsheet.
        tab:    Sheet tab name (e.g. "Income 2026").
        range:  A1 notation range within the tab (e.g. "A1:C3").
        values: 2-D list of values to write, row-major. Example:
                [["Date", "Amount", "Note"], ["2026-08-31", "1200", "salary"]]
    """
    if not values:
        return "ERROR: values must be a non-empty list of rows."

    target = f"'{tab}'!{range}"

    def _write():
        return _sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=target,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

    try:
        resp = await asyncio.to_thread(_write)
    except Exception as exc:
        return f"ERROR writing range: {exc}"

    updated = resp.get("updatedCells", "?")
    updated_range = resp.get("updatedRange", target)
    return f"Written. Updated range: {updated_range} ({updated} cells updated)."


@mcp.tool()
async def gdrive_sheet_append_rows(
    spreadsheet_id: str,
    tab: str,
    values: list,
) -> str:
    """Append rows to the end of a Google Sheet tab.

    Finds the first empty row after existing data and inserts rows there.
    Does not affect any existing data.

    Args:
        spreadsheet_id: Google Drive file ID of the spreadsheet.
        tab:    Sheet tab name (e.g. "Income 2026").
        values: 2-D list of rows to append. Each inner list is one row. Example:
                [["2026-08-31", "1200", "salary"], ["2026-08-31", "50", "transport"]]
    """
    if not values:
        return "ERROR: values must be a non-empty list of rows."

    target = f"'{tab}'"

    def _append():
        return _sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=target,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()

    try:
        resp = await asyncio.to_thread(_append)
    except Exception as exc:
        return f"ERROR appending rows: {exc}"

    updates = resp.get("updates", {})
    updated_range = updates.get("updatedRange", "?")
    updated_rows = updates.get("updatedRows", "?")
    return f"Appended {updated_rows} row(s) to {updated_range}."


@mcp.tool()
async def gdrive_sheet_add_tab(
    spreadsheet_id: str,
    title: str,
) -> str:
    """Add a new tab to a Google Spreadsheet.

    Args:
        spreadsheet_id: Google Drive file ID of the spreadsheet.
        title: Name for the new tab.
    """
    if not title:
        return "ERROR: title is required."

    def _add():
        return _sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {"addSheet": {"properties": {"title": title}}}
                ]
            },
        ).execute()

    try:
        resp = await asyncio.to_thread(_add)
    except Exception as exc:
        return f"ERROR adding tab: {exc}"

    replies = resp.get("replies", [{}])
    added = replies[0].get("addSheet", {}).get("properties", {})
    sheet_id = added.get("sheetId", "?")
    return f"Tab '{title}' added (sheetId: {sheet_id})."


@mcp.tool()
async def gdrive_read_sheet(
    spreadsheet_id: str,
    range: str = "",
    sheet_name: str = "",
    max_rows: int = 500,
) -> str:
    """Read rows from a Google Sheet as JSON.

    Args:
        spreadsheet_id: The Drive file ID of the spreadsheet (or the full
                        spreadsheet ID from the URL).
        range:          A1 notation range, e.g. "A1:F100". If omitted, reads
                        the first sheet from row 1.
        sheet_name:     Sheet tab name (used to build the range if range is
                        omitted). Ignored when range is provided.
        max_rows:       Row limit (default 500).

    Returns a JSON array of objects keyed by the first-row headers.
    """
    def _read():
        if range:
            target = range
        elif sheet_name:
            target = f"'{sheet_name}'"
        else:
            # Let the API pick the first sheet.
            target = ""

        kwargs = {
            "spreadsheetId": spreadsheet_id,
            "valueRenderOption": "FORMATTED_VALUE",
            "dateTimeRenderOption": "FORMATTED_STRING",
        }
        if target:
            return _sheets.spreadsheets().values().get(
                range=target, **kwargs
            ).execute()
        else:
            # No range: get sheet names first, use the first one.
            meta = _sheets.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties.title",
            ).execute()
            first_sheet = meta["sheets"][0]["properties"]["title"]
            return _sheets.spreadsheets().values().get(
                range=f"'{first_sheet}'", **kwargs
            ).execute()

    try:
        resp = await asyncio.to_thread(_read)
    except Exception as exc:
        return f"ERROR reading sheet: {exc}"

    values = resp.get("values", [])
    if not values:
        return "Sheet is empty or the range returned no data."

    headers = values[0]
    rows = values[1 : max_rows + 1]
    result = []
    for row in rows:
        # Pad shorter rows so all keys exist.
        padded = row + [""] * (len(headers) - len(row))
        result.append(dict(zip(headers, padded)))

    truncated = len(values) - 1 > max_rows
    out = json.dumps(result, ensure_ascii=False, indent=2)
    if truncated:
        out += f"\n\n// truncated: showing {max_rows} of {len(values) - 1} data rows"
    return out


# ──────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
