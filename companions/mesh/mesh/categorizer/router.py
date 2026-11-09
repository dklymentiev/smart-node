"""FastAPI router for categorizer endpoints."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from . import _get_or_create_store, batch_scan, bootstrap, classify, get_store
from .config import is_categorizer_enabled, is_llm_available
from .models import (
    BatchRequest,
    BootstrapRequest,
    CategoryCreateRequest,
    CategoryUpdateRequest,
    TaxonomyResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/categorizer", tags=["categorizer"])


# Auth dependency (avoids circular import from main.py)
async def _get_auth(request: Request):
    from ..main import resolve_auth
    return await resolve_auth(request)


async def _require_admin(request: Request):
    auth = await _get_auth(request)
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return auth


def _get_pool(workspace_id: str = "default"):
    """Get the database pool from the store."""
    store = get_store(workspace_id)
    if store is None:
        # Fallback: try default store for pool reference
        store = get_store("default")
    if store is None:
        raise HTTPException(status_code=503, detail="Categorizer not initialized")
    return store.pool


@router.get("/taxonomy", response_model=TaxonomyResponse)
async def get_taxonomy(auth=Depends(_get_auth)):
    """Get current taxonomy with stats and pending proposals."""
    ws = auth.workspace
    store = get_store(ws)
    if store is None or store.taxonomy is None:
        return TaxonomyResponse(
            taxonomy={"version": 1, "categories": []},
            stats={"status": "no taxonomy", "hint": "POST /categorizer/bootstrap to generate"},
        )

    pool = store.pool

    # Gather stats (workspace-scoped, set RLS context)
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.workspaces', $1, false)", ws)
        await conn.execute("SELECT set_config('app.is_admin', 'false', false)")
        total = await conn.fetchval(
            "SELECT count(*) FROM documents WHERE workspace_id = $1", ws
        )
        categorized = await conn.fetchval(
            """
            SELECT count(*) FROM documents
            WHERE workspace_id = $1
              AND EXISTS (SELECT 1 FROM unnest(tags) AS t WHERE t LIKE 'ai-category:%')
            """,
            ws,
        )
        uncategorized_count = await conn.fetchval(
            """
            SELECT count(*) FROM documents
            WHERE workspace_id = $1
              AND EXISTS (SELECT 1 FROM unnest(tags) AS t WHERE t = 'ai-category:uncategorized')
            """,
            ws,
        )
        centroids_count = await conn.fetchval(
            "SELECT count(*) FROM category_centroids WHERE workspace_id = $1", ws
        )

        # Pending proposals
        proposals_rows = await conn.fetch(
            """
            SELECT guid, content, created_at FROM documents
            WHERE tags @> '{type:taxonomy-proposal}'::text[]
              AND workspace_id = $1
            ORDER BY created_at DESC LIMIT 20
            """,
            ws,
        )

    proposals = []
    for row in proposals_rows:
        try:
            proposals.append(
                {"guid": row["guid"], "created_at": str(row["created_at"]),
                 **json.loads(row["content"])}
            )
        except (json.JSONDecodeError, TypeError):
            proposals.append({"guid": row["guid"], "raw": row["content"][:200]})

    return TaxonomyResponse(
        taxonomy=store.taxonomy,
        stats={
            "total_documents": total,
            "categorized": categorized,
            "uncategorized": uncategorized_count,
            "centroids": centroids_count,
            "categories": len(store.taxonomy.categories),
            "llm_available": is_llm_available(),
        },
        proposals=proposals,
    )


@router.post("/bootstrap")
async def run_bootstrap(
    request: BootstrapRequest = BootstrapRequest(),
    auth=Depends(_get_auth),
):
    """Generate taxonomy from existing documents."""
    ws = auth.workspace
    pool = _get_pool(ws)

    if request.mode == "designed" and not is_llm_available():
        raise HTTPException(
            status_code=400,
            detail="LLM required for 'designed' mode. Set LLM_API_URL, LLM_API_KEY, LLM_MODEL.",
        )

    if request.use_llm and not is_llm_available():
        raise HTTPException(
            status_code=400,
            detail="LLM not configured. Set LLM_API_URL, LLM_API_KEY, LLM_MODEL.",
        )

    taxonomy = await bootstrap(
        pool,
        use_llm=request.use_llm,
        sample_size=request.sample_size,
        min_cluster_size=request.min_cluster_size,
        mode=request.mode,
        workspace_id=ws,
    )

    return {
        "status": "ok",
        "categories": len(taxonomy.categories),
        "subcategories": sum(len(c.subcategories) for c in taxonomy.categories),
        "taxonomy": taxonomy.model_dump(),
    }


@router.post("/classify/{guid}")
async def classify_single(guid: str, auth=Depends(_get_auth)):
    """Classify a single document by GUID."""
    ws = auth.workspace
    pool = _get_pool(ws)

    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.workspaces', $1, false)", ws)
        await conn.execute("SELECT set_config('app.is_admin', 'false', false)")
        row = await conn.fetchrow(
            """
            SELECT e.embedding, d.content
            FROM doc_embeddings e
            JOIN documents d ON d.guid = e.guid
            WHERE e.guid = $1 AND d.workspace_id = $2
            """,
            guid,
            ws,
        )

    if not row:
        raise HTTPException(status_code=404, detail=f"Document {guid} not found or has no embedding")

    import numpy as np

    raw = row["embedding"]
    if isinstance(raw, str):
        embedding = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32).tolist()
    else:
        embedding = list(raw)

    result = await classify(guid, row["content"], embedding, pool, workspace_id=ws)
    if result is None:
        raise HTTPException(status_code=503, detail="No taxonomy loaded. Run bootstrap first.")

    return {"guid": guid, "classification": result.model_dump()}


@router.post("/batch")
async def run_batch(
    request: BatchRequest = BatchRequest(),
    auth=Depends(_get_auth),
):
    """Batch classify untagged documents."""
    ws = auth.workspace
    pool = _get_pool(ws)
    stats = await batch_scan(pool, limit=request.limit, force=request.force, workspace_id=ws)
    return {"status": "ok", "stats": stats}


# ---- Category CRUD ----


@router.get("/categories")
async def list_categories(auth=Depends(_get_auth)):
    """List all categories for the current workspace."""
    ws = auth.workspace
    pool = _get_pool(ws)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT category_id, parent_id, name, description, doc_count,
                   created_at, updated_at
            FROM category_centroids
            WHERE workspace_id = $1
            ORDER BY parent_id NULLS FIRST, name
            """,
            ws,
        )

    categories = []
    for r in rows:
        categories.append({
            "id": r["category_id"],
            "parent_id": r["parent_id"],
            "name": r["name"],
            "description": r["description"] or "",
            "doc_count": r["doc_count"] or 0,
            "created_at": str(r["created_at"]) if r["created_at"] else None,
            "updated_at": str(r["updated_at"]) if r["updated_at"] else None,
        })

    return categories


@router.post("/categories")
async def create_category(
    request: CategoryCreateRequest,
    auth=Depends(_require_admin),
):
    """Create a new category (admin only)."""
    ws = auth.workspace
    store = await _get_or_create_store(ws)

    await store.upsert_centroid(
        category_id=request.id,
        name=request.name,
        description=request.description,
        parent_id=request.parent_id,
    )

    return {"status": "ok", "id": request.id, "workspace": ws}


@router.put("/categories/{category_id}")
async def update_category(
    category_id: str,
    request: CategoryUpdateRequest,
    auth=Depends(_require_admin),
):
    """Update a category name/description (admin only)."""
    ws = auth.workspace
    store = await _get_or_create_store(ws)

    # Verify it exists
    pool = store.pool
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT 1 FROM category_centroids "
            "WHERE category_id = $1 AND workspace_id = $2",
            category_id, ws,
        )
    if not existing:
        raise HTTPException(status_code=404, detail=f"Category '{category_id}' not found")

    desc = request.description
    if desc is None:
        # Keep existing description
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT description FROM category_centroids "
                "WHERE category_id = $1 AND workspace_id = $2",
                category_id, ws,
            )
            desc = row["description"] if row else ""

    await store.upsert_centroid(
        category_id=category_id,
        name=request.name,
        description=desc,
    )

    return {"status": "ok", "id": category_id}


@router.delete("/categories/{category_id}")
async def delete_category(
    category_id: str,
    auth=Depends(_require_admin),
):
    """Delete a category and strip its tags from documents (admin only)."""
    ws = auth.workspace
    store = await _get_or_create_store(ws)

    deleted = await store.delete_centroid(category_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Category '{category_id}' not found")

    # Strip ai-category/ai-subcategory tags referencing this category from documents
    pool = store.pool
    async with pool.acquire() as conn:
        await conn.execute("SELECT set_config('app.workspaces', $1, false)", ws)
        await conn.execute("SELECT set_config('app.is_admin', 'false', false)")
        await conn.execute(
            """
            UPDATE documents SET
                tags = ARRAY(
                    SELECT t FROM unnest(tags) AS t
                    WHERE t != $1 AND t != $2
                      AND NOT (t LIKE $3)
                ),
                updated_at = NOW()
            WHERE workspace_id = $4
              AND (tags @> ARRAY[$1]::text[]
                   OR tags @> ARRAY[$2]::text[]
                   OR EXISTS (SELECT 1 FROM unnest(tags) t2 WHERE t2 LIKE $3))
            """,
            f"ai-category:{category_id}",
            f"ai-subcategory:{category_id}",
            f"ai-subcategory:{category_id}/%",
            ws,
        )

    return {"status": "ok", "id": category_id, "deleted": True}
