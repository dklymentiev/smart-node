#!/usr/bin/env python3
"""
Seed Mesh with pre-generated demo documents from a JSONL file.

Reads demo-seed.jsonl and pushes documents via PUT /bulk endpoint.
The JSONL file already contains enriched tags, so no separate enrichment step is needed.

Usage:
    python scripts/seed_demo.py --api http://localhost:8001
    python scripts/seed_demo.py --api http://localhost:8001 --file examples/demo-seed.jsonl
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

DEFAULT_SEED_FILE = Path(__file__).parent.parent / "examples" / "demo-seed.jsonl"


def load_jsonl(path: Path) -> list:
    """Read documents from a JSONL file."""
    docs = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    return docs


def upload_bulk(api_url: str, docs: list, batch_size: int = 100) -> None:
    """Upload documents via PUT /bulk in batches."""
    total = len(docs)
    loaded = 0
    errors = 0

    for i in range(0, total, batch_size):
        batch = docs[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} docs)...", end=" ", flush=True)

        try:
            resp = requests.put(
                f"{api_url.rstrip('/')}/bulk",
                json=batch,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                created = data.get("created", len(batch))
                skipped = data.get("skipped", 0)
                loaded += created
                print(f"OK (created: {created}, skipped: {skipped})")
            else:
                errors += len(batch)
                print(f"ERROR {resp.status_code}: {resp.text[:200]}")
        except requests.exceptions.RequestException as e:
            errors += len(batch)
            print(f"ERROR: {e}")

        if i + batch_size < total:
            time.sleep(0.5)

    print(f"\nDone: {loaded} created, {errors} errors out of {total} total")


def main():
    parser = argparse.ArgumentParser(description="Seed Mesh with demo data from JSONL")
    parser.add_argument("--api", default="http://localhost:8001", help="Mesh API URL")
    parser.add_argument("--file", type=str, default=None, help="Path to JSONL seed file")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size for bulk upload")
    args = parser.parse_args()

    seed_path = Path(args.file) if args.file else DEFAULT_SEED_FILE
    if not seed_path.exists():
        print(f"Seed file not found: {seed_path}")
        sys.exit(1)

    # Check API
    api = args.api.rstrip("/")
    print(f"Mesh Demo Seeder")
    print(f"  API: {api}")
    print(f"  File: {seed_path}")
    print()

    try:
        resp = requests.get(f"{api}/health", timeout=10)
        if resp.status_code != 200:
            print(f"API not healthy: {resp.status_code}")
            sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"Cannot reach API: {e}")
        sys.exit(1)

    # Load and upload
    print("Loading seed file...")
    docs = load_jsonl(seed_path)
    print(f"  {len(docs)} documents loaded")
    print()

    print("Uploading...")
    upload_bulk(api, docs, args.batch_size)

    # Show stats
    print()
    time.sleep(2)
    try:
        resp = requests.get(f"{api}/stats", timeout=10)
        if resp.status_code == 200:
            stats = resp.json()
            doc_count = stats.get("total_documents", stats.get("documents", 0))
            queue = stats.get("embedding_queue_size", stats.get("queue_size", "?"))
            print(f"Documents: {doc_count}")
            print(f"Embedding queue: {queue}")
            print(f"\nEmbeddings will be processed in the background.")
            print(f"Monitor progress: curl {api}/stats")
    except Exception:
        pass


if __name__ == "__main__":
    main()
