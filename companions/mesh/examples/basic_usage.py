#!/usr/bin/env python3
"""
Basic Mesh usage: create documents, search, and manage tags.

Requirements:
    pip install httpx

Usage:
    python examples/basic_usage.py
"""

import httpx

BASE_URL = "http://localhost:8000"


def main():
    client = httpx.Client(base_url=BASE_URL, timeout=30)

    # 1. Health check
    resp = client.get("/health")
    print(f"Health: {resp.json()['status']}")

    # 2. Create documents
    docs = [
        {
            "content": "Decided to use PostgreSQL with pgvector for semantic search",
            "tags": ["type:decision", "topic:database"],
        },
        {
            "content": "Implemented rate limiting: 60 req/min for search, 30 for embed",
            "tags": ["type:worklog", "topic:security"],
        },
        {
            "content": "TODO: Add WebSocket support for real-time updates",
            "tags": ["type:task", "topic:api"],
        },
    ]

    created_guids = []
    for doc in docs:
        resp = client.put("/", json=doc)
        data = resp.json()
        created_guids.append(data["guid"])
        print(f"Created: {data['guid']} ({len(data['tags'])} tags)")

    # 3. Semantic search (finds by meaning, not exact words)
    import time
    time.sleep(2)  # Wait for embedding indexing

    resp = client.post("/search", json={"query": "which database?", "limit": 5})
    results = resp.json()["results"]
    print(f"\nSearch 'which database?' -> {len(results)} results")
    for r in results:
        print(f"  [{r['similarity_score']:.3f}] {r['content'][:60]}...")

    # 4. Filter search by tags
    resp = client.post(
        "/search",
        json={"query": "security", "limit": 5, "tags": ["type:worklog"]},
    )
    results = resp.json()["results"]
    print(f"\nSearch 'security' (worklogs only) -> {len(results)} results")

    # 5. List tags
    resp = client.get("/tags", params={"prefix": "type:"})
    tags = resp.json()["tags"]
    print(f"\nDocument types: {', '.join(t['tag'] for t in tags)}")

    # 6. Get stats
    resp = client.get("/stats")
    stats = resp.json()
    print(f"\nStats: {stats['documents']} docs, {stats['indexed']} indexed")

    # 7. Clean up
    for guid in created_guids:
        client.delete(f"/{guid}")
    print(f"\nCleaned up {len(created_guids)} documents")


if __name__ == "__main__":
    main()
