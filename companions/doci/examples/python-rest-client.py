"""
Minimal DOCI REST client — single file, stdlib + requests only.

Mirrors examples/curl-rest.sh so agents that don't speak MCP can drive
DOCI from Python directly. Drop the class into your project, set
DOCI_API_URL + DOCI_API_KEY, and you're done.

Run:
    pip install requests
    python examples/python-rest-client.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

import requests


@dataclass
class DOCIClient:
    base_url: str
    api_key: str
    timeout: float = 10.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = self.base_url.rstrip("/") + path
        resp = requests.request(
            method, url, headers=self._headers, timeout=self.timeout, **kwargs
        )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # --- documents -------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health.php")

    def create(self, path: str, content: str, title: str | None = None,
               summary: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
        body = {"path": path, "content": content}
        if title is not None:
            body["title"] = title
        if summary is not None:
            body["summary"] = summary
        if tags is not None:
            body["tags"] = tags
        return self._request("POST", "/documents.php", json=body)

    def get(self, guid: str, *, with_content: bool = False) -> dict[str, Any]:
        q = f"?guid={guid}" + ("&content=1" if with_content else "")
        return self._request("GET", f"/documents.php{q}")

    def update(self, guid: str, **fields: Any) -> dict[str, Any]:
        if not fields:
            raise ValueError("update() needs at least one field")
        body = {"guid": guid, **fields}
        return self._request("PUT", "/documents.php", json=body)

    def delete(self, guid: str) -> dict[str, Any]:
        return self._request("DELETE", f"/documents.php?guid={guid}")

    # --- inbox -----------------------------------------------------------

    def inbox(self, content: str, tags: str | list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if tags is not None:
            body["tags"] = tags
        return self._request("POST", "/inbox.php?action=add", json=body)

    def inbox_promote(self, guid: str, target_path: str) -> dict[str, Any]:
        body = {"guid": guid, "target": target_path}
        return self._request("POST", "/inbox.php?action=promote", json=body)

    # --- threads + versions ---------------------------------------------

    def thread(self, parent_guid: str, title: str, message: str,
               quote: str | None = None, ai_reply: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parent_guid": parent_guid,
            "title": title,
            "message": message,
        }
        if quote is not None:
            body["quote"] = quote
        if ai_reply:
            body["ai_reply"] = True
        return self._request("POST", "/thread.php", json=body)

    def versions(self, document_guid: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/versions.php?document_guid={document_guid}")


def main() -> int:
    client = DOCIClient(
        base_url=os.environ.get("DOCI_API_URL", "http://localhost:8080/api"),
        api_key=os.environ.get("DOCI_API_KEY", "dev-key"),
    )

    print("health:", client.health())

    created = client.create(
        path="examples/python-walkthrough",
        title="Python walkthrough",
        content="# Python walkthrough\n\nFirst paragraph.\n\nSecond paragraph for threading.",
    )
    guid = created["guid"]
    print("created:", guid)

    client.update(guid, tags=["example", "walkthrough"])

    thread = client.thread(
        parent_guid=guid,
        title="What does this mean?",
        quote="Second paragraph for threading.",
        message="Asking the agent for context.",
    )
    print("thread:", thread["guid"])

    print("versions:", [v["guid"] for v in client.versions(guid)])

    client.inbox("Idea: cron the live benchmark on every push.", tags=["ops", "ideas"])

    client.delete(guid)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
