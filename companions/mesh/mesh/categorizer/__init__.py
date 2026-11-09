"""Mesh AI Categorizer -- automatic document classification.

Public API:
    classify()    -- classify a single document (called from embedding_worker)
    bootstrap()   -- generate taxonomy from existing documents via clustering
    batch_scan()  -- batch classify untagged documents
"""

import asyncio
import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import asyncpg
import numpy as np

from .classifier_embedding import classify_embedding
from .classifier_llm import (
    classify_docs_batch,
    classify_with_llm,
    design_taxonomy,
    name_cluster,
    name_macro_category,
    propose_new_category,
)
from .config import (
    get_category_threshold,
    get_subcategory_threshold,
    is_categorizer_enabled,
    is_llm_available,
)
from .models import (
    Category,
    ClassificationResult,
    Subcategory,
    Taxonomy,
)
from .taxonomy import TaxonomyStore

logger = logging.getLogger(__name__)

# Per-workspace store instances, initialized lazily
_stores: Dict[str, TaxonomyStore] = {}
_pool: Optional[asyncpg.Pool] = None


async def _get_or_create_store(workspace_id: str = "default") -> TaxonomyStore:
    """Lazy-load a TaxonomyStore for the given workspace."""
    if workspace_id in _stores:
        return _stores[workspace_id]
    if _pool is None:
        raise RuntimeError("Categorizer not initialized -- call init() first")
    store = TaxonomyStore(_pool, workspace_id=workspace_id)
    await store.load()
    _stores[workspace_id] = store
    logger.info(f"TaxonomyStore created for workspace '{workspace_id}' "
                f"({len(store._centroids)} centroids)")
    return store


def get_store(workspace_id: str = "default") -> Optional[TaxonomyStore]:
    """Get an existing TaxonomyStore (set by init()). Used by router."""
    return _stores.get(workspace_id)


async def init(pool: asyncpg.Pool, workspace_id: str = "default") -> None:
    """Initialize the categorizer. Called from main.py lifespan."""
    global _pool
    _pool = pool
    store = await _get_or_create_store(workspace_id)
    if store.taxonomy:
        logger.info("Categorizer initialized with existing taxonomy")
    else:
        logger.info("Categorizer initialized (no taxonomy yet -- run bootstrap)")


async def classify(
    guid: str,
    content: str,
    embedding: List[float],
    pool: asyncpg.Pool,
    workspace_id: str = "default",
) -> Optional[ClassificationResult]:
    """Classify a single document and apply ai-category/ai-subcategory tags.

    Called from embedding_worker after embedding is stored.
    Returns None if categorizer is not ready (no taxonomy).
    """
    store = await _get_or_create_store(workspace_id)
    if not store.centroids_loaded:
        return None

    # Embedding-based classification (~1ms)
    result = classify_embedding(embedding, store)

    # LLM refinement for ambiguous cases
    if is_llm_available() and store.taxonomy:
        ambiguous_low = 0.45
        ambiguous_high = 0.65
        if ambiguous_low <= result.category_score <= ambiguous_high:
            llm_result = await classify_with_llm(content, store.taxonomy, result)
            if llm_result:
                result = llm_result

        # LLM proposal for uncategorized
        if result.category_id == "uncategorized":
            proposal = await propose_new_category(content)
            if proposal:
                await _save_proposal(pool, guid, proposal)

    # Apply tags to document
    await _apply_category_tags(pool, guid, result)
    return result


async def bootstrap(
    pool: asyncpg.Pool,
    use_llm: bool = False,
    sample_size: int = 1000,
    min_cluster_size: int = 5,
    mode: str = "cluster",
    workspace_id: str = "default",
) -> Taxonomy:
    """Generate taxonomy.

    Modes:
    - 'cluster': bottom-up HDBSCAN clustering (embedding-only or LLM-named)
    - 'designed': LLM designs taxonomy from samples, embeddings build centroids
    """
    if mode == "designed":
        return await _bootstrap_designed(pool, sample_size=sample_size, workspace_id=workspace_id)
    return await _bootstrap_cluster(pool, use_llm, sample_size, min_cluster_size, workspace_id=workspace_id)


async def _bootstrap_designed(
    pool: asyncpg.Pool,
    sample_size: int = 500,
    workspace_id: str = "default",
) -> Taxonomy:
    """LLM-designed taxonomy: LLM sees samples, proposes categories, then
    LLM classifies a batch of docs as seeds, centroids computed from those.

    Phase 1: Sample 50 diverse docs -> LLM designs 10-15 categories
    Phase 2: Sample 500 docs -> LLM classifies in batches of 30
    Phase 3: Compute initial centroids from LLM-classified seeds
    Phase 4: Refine centroids by classifying ALL docs with embeddings,
             then recomputing centroids from the full classified set.
             (k-means with LLM-initialized centers)
    """
    store = await _get_or_create_store(workspace_id)

    logger.info("Bootstrap (designed mode) starting")

    # Phase 1: Sample diverse docs for taxonomy design
    async with pool.acquire() as conn:
        # Get docs with various type: tags for diversity
        diverse_rows = await conn.fetch("""
            (SELECT guid, LEFT(content, 150) as preview,
                    array_to_string(tags, ', ') as tags_str
             FROM documents WHERE tags @> '{type:worklog}'::text[]
               AND workspace_id = $1
             ORDER BY random() LIMIT 8)
            UNION ALL
            (SELECT guid, LEFT(content, 150),
                    array_to_string(tags, ', ')
             FROM documents WHERE tags @> '{type:note}'::text[]
               AND workspace_id = $1
             ORDER BY random() LIMIT 7)
            UNION ALL
            (SELECT guid, LEFT(content, 150),
                    array_to_string(tags, ', ')
             FROM documents WHERE tags @> '{type:decision}'::text[]
               AND workspace_id = $1
             ORDER BY random() LIMIT 5)
            UNION ALL
            (SELECT guid, LEFT(content, 150),
                    array_to_string(tags, ', ')
             FROM documents WHERE tags @> '{type:research}'::text[]
               AND workspace_id = $1
             ORDER BY random() LIMIT 5)
            UNION ALL
            (SELECT guid, LEFT(content, 150),
                    array_to_string(tags, ', ')
             FROM documents WHERE tags @> '{type:artifact}'::text[]
               AND workspace_id = $1
             ORDER BY random() LIMIT 5)
            UNION ALL
            (SELECT guid, LEFT(content, 150),
                    array_to_string(tags, ', ')
             FROM documents
             WHERE NOT EXISTS (SELECT 1 FROM unnest(tags) t WHERE t LIKE 'type:%')
               AND workspace_id = $1
             ORDER BY random() LIMIT 10)
            UNION ALL
            (SELECT guid, LEFT(content, 150),
                    array_to_string(tags, ', ')
             FROM documents WHERE workspace_id = $1
             ORDER BY random() LIMIT 10)
        """, workspace_id)

    doc_samples = [
        {"preview": r["preview"].replace("\n", " "), "tags": r["tags_str"] or ""}
        for r in diverse_rows
    ]
    logger.info(f"Phase 1: Designing taxonomy from {len(doc_samples)} diverse samples")

    cat_list = await design_taxonomy(doc_samples)
    if not cat_list:
        logger.error("LLM failed to design taxonomy")
        return Taxonomy(version=1, categories=[])

    logger.info(f"Phase 1 result: {len(cat_list)} categories proposed")
    for c in cat_list:
        logger.info(f"  - {c.get('id')}: {c.get('name')} -- {c.get('description', '')[:80]}")

    # Phase 2: Classify docs in batches via LLM to build seed assignments
    async with pool.acquire() as conn:
        seed_rows = await conn.fetch("""
            SELECT d.guid, LEFT(d.content, 150) as preview, e.embedding
            FROM documents d
            JOIN doc_embeddings e ON d.guid = e.guid
            WHERE d.workspace_id = $2
            ORDER BY random()
            LIMIT $1
        """, min(sample_size, 500), workspace_id)

    seed_docs = [
        {"guid": r["guid"], "preview": r["preview"].replace("\n", " ")}
        for r in seed_rows
    ]

    # Classify in batches of 30
    assignments: Dict[str, str] = {}  # guid -> category_id
    batch_size = 30
    for i in range(0, len(seed_docs), batch_size):
        batch = seed_docs[i:i + batch_size]
        result = await classify_docs_batch(batch, cat_list)
        if result and isinstance(result, dict):
            assignments.update(result)
            logger.info(f"Phase 2: classified batch {i//batch_size + 1}, "
                        f"total assignments: {len(assignments)}")

    logger.info(f"Phase 2: {len(assignments)} documents classified by LLM")

    # Phase 3: Compute initial centroids from LLM-classified seeds
    guid_to_embedding: Dict[str, np.ndarray] = {}
    for r in seed_rows:
        raw = r["embedding"]
        if isinstance(raw, str):
            vec = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
        else:
            vec = np.array(raw, dtype=np.float32)
        guid_to_embedding[r["guid"]] = vec

    categories: List[Category] = []
    centroid_map: Dict[str, np.ndarray] = {}  # cat_id -> centroid vector
    cat_info_map: Dict[str, dict] = {}  # cat_id -> {name, description}

    for cat_info in cat_list:
        cat_id = cat_info["id"]
        cat_name = cat_info.get("name", cat_id)
        cat_desc = cat_info.get("description", "")
        cat_info_map[cat_id] = {"name": cat_name, "description": cat_desc}

        member_embeddings = []
        for guid, assigned_cat in assignments.items():
            if assigned_cat == cat_id and guid in guid_to_embedding:
                member_embeddings.append(guid_to_embedding[guid])

        if not member_embeddings:
            logger.info(f"  Category '{cat_name}': no seed docs, skipping centroid")
            categories.append(Category(id=cat_id, name=cat_name, description=cat_desc))
            continue

        centroid_map[cat_id] = np.mean(member_embeddings, axis=0)
        categories.append(Category(id=cat_id, name=cat_name, description=cat_desc))
        logger.info(f"  Category '{cat_name}': {len(member_embeddings)} seed docs")

    logger.info(f"Phase 3: {len(centroid_map)} initial centroids from LLM seeds")

    # Phase 4: Iterative centroid refinement using ALL document embeddings.
    # Classify every doc with initial centroids, then recompute centroids.
    # This is like k-means refinement with LLM-initialized centers.
    async with pool.acquire() as conn:
        all_rows = await conn.fetch("""
            SELECT e.guid, e.embedding
            FROM doc_embeddings e
            JOIN documents d ON d.guid = e.guid
            WHERE d.workspace_id = $1
        """, workspace_id)

    logger.info(f"Phase 4: Refining centroids using {len(all_rows)} document embeddings")

    # Parse all embeddings
    all_embeddings: Dict[str, np.ndarray] = {}
    for r in all_rows:
        raw = r["embedding"]
        if isinstance(raw, str):
            vec = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
        else:
            vec = np.array(raw, dtype=np.float32)
        all_embeddings[r["guid"]] = vec

    # Build centroid matrix for fast batch cosine similarity
    cat_ids_with_centroids = list(centroid_map.keys())

    # 2 refinement iterations
    for iteration in range(2):
        centroid_matrix = np.vstack([centroid_map[cid] for cid in cat_ids_with_centroids])
        # Normalize centroid vectors
        centroid_norms = np.linalg.norm(centroid_matrix, axis=1, keepdims=True)
        centroid_normed = centroid_matrix / np.maximum(centroid_norms, 1e-10)

        # Classify all docs using vectorized cosine similarity
        new_assignments: Dict[str, List[np.ndarray]] = {cid: [] for cid in cat_ids_with_centroids}

        for guid, emb in all_embeddings.items():
            emb_norm = emb / max(np.linalg.norm(emb), 1e-10)
            scores = centroid_normed @ emb_norm  # (n_categories,)
            best_idx = int(np.argmax(scores))
            best_cat = cat_ids_with_centroids[best_idx]
            new_assignments[best_cat].append(emb)

        # Recompute centroids from full classified set
        for cat_id in cat_ids_with_centroids:
            members = new_assignments[cat_id]
            if members:
                centroid_map[cat_id] = np.mean(members, axis=0)

        dist_info = ", ".join(
            f"{cat_info_map[cid]['name']}: {len(new_assignments[cid])}"
            for cid in cat_ids_with_centroids
        )
        logger.info(f"  Iteration {iteration + 1}: {dist_info}")

    # Phase 5: Generate subcategories via HDBSCAN within each category.
    # For categories with >50 docs, cluster their embeddings to find subtopics.
    # Need guid->category mapping from last iteration for content lookup.
    guid_to_cat: Dict[str, str] = {}
    centroid_matrix = np.vstack([centroid_map[cid] for cid in cat_ids_with_centroids])
    centroid_norms = np.linalg.norm(centroid_matrix, axis=1, keepdims=True)
    centroid_normed = centroid_matrix / np.maximum(centroid_norms, 1e-10)
    for guid, emb in all_embeddings.items():
        emb_norm = emb / max(np.linalg.norm(emb), 1e-10)
        scores = centroid_normed @ emb_norm
        best_idx = int(np.argmax(scores))
        guid_to_cat[guid] = cat_ids_with_centroids[best_idx]

    # Fetch content previews for LLM naming
    async with pool.acquire() as conn:
        content_rows = await conn.fetch(
            "SELECT guid, LEFT(content, 300) as preview FROM documents "
            "WHERE workspace_id = $1",
            workspace_id,
        )
    guid_to_content: Dict[str, str] = {r["guid"]: r["preview"] for r in content_rows}

    logger.info("Phase 5: Generating subcategories via HDBSCAN within categories")

    MIN_DOCS_FOR_SUBCATS = 200
    MIN_SUB_CLUSTER_SIZE = 8
    sub_centroid_rows = []
    categories_with_subs: List[Category] = []

    for cat_id in cat_ids_with_centroids:
        info = cat_info_map[cat_id]
        # Collect embeddings belonging to this category
        cat_guids = [g for g, c in guid_to_cat.items() if c == cat_id]

        if len(cat_guids) < MIN_DOCS_FOR_SUBCATS:
            # Not enough docs, keep category without subcategories
            categories_with_subs.append(
                Category(id=cat_id, name=info["name"], description=info["description"])
            )
            logger.info(f"  {info['name']}: {len(cat_guids)} docs (too few for subcategories)")
            continue

        # Stack embeddings for this category
        cat_embs = np.vstack([all_embeddings[g] for g in cat_guids])

        # Run HDBSCAN
        sub_labels = _run_hdbscan(cat_embs, min_cluster_size=MIN_SUB_CLUSTER_SIZE)
        sub_ids = set(sub_labels)
        sub_ids.discard(-1)

        if len(sub_ids) < 2:
            categories_with_subs.append(
                Category(id=cat_id, name=info["name"], description=info["description"])
            )
            logger.info(f"  {info['name']}: {len(cat_guids)} docs, "
                        f"HDBSCAN found {len(sub_ids)} clusters (not enough)")
            continue

        subcategories = []
        for sub_label in sorted(sub_ids):
            mask = sub_labels == sub_label
            sub_indices = np.where(mask)[0]
            sub_guids = [cat_guids[i] for i in sub_indices]
            sub_embs = cat_embs[mask]
            sub_centroid = sub_embs.mean(axis=0)

            # Name subcategory via LLM (from representative doc previews)
            rep_contents = [guid_to_content.get(g, "")[:300] for g in sub_guids[:5]]
            if is_llm_available():
                llm_name = await name_cluster(rep_contents)
            else:
                llm_name = None

            if llm_name:
                sub_id = f"{cat_id}/{llm_name.get('id', f'sub-{sub_label}')}"
                sub_name = llm_name.get("name", f"Subcluster {sub_label}")
                sub_desc = llm_name.get("description", "")
            else:
                sub_id = f"{cat_id}/sub-{sub_label}"
                sub_name = f"Subcluster {sub_label}"
                sub_desc = f"{len(sub_guids)} documents"

            subcategories.append(
                Subcategory(id=sub_id, name=sub_name, description=sub_desc)
            )
            sub_centroid_rows.append(
                (sub_id, cat_id, sub_name, sub_desc,
                 sub_centroid.tolist(), len(sub_guids))
            )

        categories_with_subs.append(
            Category(id=cat_id, name=info["name"],
                     description=info["description"], subcategories=subcategories)
        )
        logger.info(f"  {info['name']}: {len(cat_guids)} docs -> "
                     f"{len(subcategories)} subcategories")

    # Replace categories list with subcategory-enriched version
    categories = categories_with_subs

    # Build final centroid rows (categories + subcategories)
    centroid_rows = []
    for cat_id in cat_ids_with_centroids:
        info = cat_info_map[cat_id]
        doc_count = len(new_assignments[cat_id])
        centroid_rows.append(
            (cat_id, None, info["name"], info["description"],
             centroid_map[cat_id].tolist(), doc_count)
        )
    centroid_rows.extend(sub_centroid_rows)

    # Save
    taxonomy = Taxonomy(version=1, categories=categories)
    await store.save(taxonomy)
    if centroid_rows:
        await store.save_centroids(centroid_rows)

    total_subs = sum(len(c.subcategories) for c in categories)
    logger.info(f"Bootstrap (designed) complete: {len(categories)} categories, "
                f"{total_subs} subcategories, {len(centroid_rows)} centroids "
                f"(refined from {len(all_embeddings)} docs)")
    return taxonomy


async def _bootstrap_cluster(
    pool: asyncpg.Pool,
    use_llm: bool = False,
    sample_size: int = 1000,
    min_cluster_size: int = 5,
    workspace_id: str = "default",
) -> Taxonomy:
    """Generate taxonomy via two-phase clustering.

    Phase 1: HDBSCAN -> many micro-clusters (fine-grained)
    Phase 2: Meta-cluster centroids -> 10-15 macro-categories (high-level)
    Phase 3: LLM names macro-categories from micro-cluster summaries
    """
    store = await _get_or_create_store(workspace_id)

    logger.info(f"Bootstrap starting (use_llm={use_llm}, sample_size={sample_size}, "
                f"workspace={workspace_id})")

    # -- Fetch embeddings --
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.guid, e.embedding, d.content, d.tags
            FROM doc_embeddings e
            JOIN documents d ON d.guid = e.guid
            WHERE d.workspace_id = $2
            ORDER BY d.updated_at DESC
            LIMIT $1
            """,
            sample_size,
            workspace_id,
        )

    if len(rows) < min_cluster_size * 2:
        logger.warning(f"Not enough documents for clustering ({len(rows)})")
        taxonomy = Taxonomy(version=1, categories=[])
        await store.save(taxonomy)
        return taxonomy

    guids, embeddings, contents, all_tags = [], [], [], []
    for row in rows:
        guids.append(row["guid"])
        raw = row["embedding"]
        if isinstance(raw, str):
            vec = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
        else:
            vec = np.array(raw, dtype=np.float32)
        embeddings.append(vec)
        contents.append(row["content"][:500])
        all_tags.append(row["tags"] or [])
    X = np.vstack(embeddings)

    # -- Phase 1: Fine-grained micro-clusters --
    labels = _run_hdbscan(X, min_cluster_size)
    micro_labels = set(labels)
    micro_labels.discard(-1)
    logger.info(f"Phase 1: {len(micro_labels)} micro-clusters from {len(X)} docs")

    # Build micro-cluster info
    micro_clusters = []
    for cid in sorted(micro_labels):
        mask = labels == cid
        indices = np.where(mask)[0]
        cluster_X = X[mask]
        centroid = cluster_X.mean(axis=0)
        cluster_contents = [contents[i] for i in indices]
        cluster_tags = [all_tags[i] for i in indices]
        summary = _summarize_micro_cluster(cluster_tags, cluster_contents)
        micro_clusters.append({
            "id": cid,
            "centroid": centroid,
            "doc_count": int(mask.sum()),
            "indices": indices,
            "contents": cluster_contents,
            "tags": cluster_tags,
            "summary": summary,
        })

    # -- Phase 2: Meta-cluster centroids into macro-categories --
    # Use AgglomerativeClustering for predictable number of macro-categories.
    # Target: ~1 macro-category per 5-7 micro-clusters, clamped to 8-20 range.
    micro_centroids = np.vstack([mc["centroid"] for mc in micro_clusters])
    n_macro = max(8, min(20, len(micro_clusters) // 6))
    n_macro = min(n_macro, len(micro_clusters))  # can't exceed micro count

    from sklearn.cluster import AgglomerativeClustering
    agg = AgglomerativeClustering(n_clusters=n_macro, metric="cosine", linkage="average")
    macro_labels = agg.fit_predict(micro_centroids)
    macro_ids = set(macro_labels)
    logger.info(f"Phase 2: {len(macro_ids)} macro-categories from {len(micro_clusters)} micro-clusters")

    # -- Phase 3: Build taxonomy --
    categories: List[Category] = []
    centroid_rows: List[Tuple[str, Optional[str], str, str, List[float], int]] = []

    for macro_id in sorted(macro_ids):
        member_micro = [micro_clusters[i] for i, l in enumerate(macro_labels) if l == macro_id]
        if not member_micro:
            continue

        # Macro centroid = average of all member doc embeddings
        all_member_indices = np.concatenate([mc["indices"] for mc in member_micro])
        macro_centroid = X[all_member_indices].mean(axis=0)
        total_docs = sum(mc["doc_count"] for mc in member_micro)

        # Collect micro-cluster summaries for LLM
        micro_summaries = [mc["summary"] for mc in member_micro]

        # Name the macro-category
        if use_llm and is_llm_available():
            llm_result = await name_macro_category(micro_summaries)
            if llm_result:
                cat_id = llm_result.get("id", f"category-{macro_id}")
                cat_name = llm_result.get("name", f"Category {macro_id}")
                cat_desc = llm_result.get("description", "")
            else:
                cat_name, cat_desc = _name_from_tags(
                    [t for mc in member_micro for t in mc["tags"]],
                    [c for mc in member_micro for c in mc["contents"]],
                    macro_id,
                )
                cat_id = cat_name.lower().replace(" ", "-").replace("/", "-")[:60]
        else:
            cat_name, cat_desc = _name_from_tags(
                [t for mc in member_micro for t in mc["tags"]],
                [c for mc in member_micro for c in mc["contents"]],
                macro_id,
            )
            cat_id = cat_name.lower().replace(" ", "-").replace("/", "-")[:60]

        # Subcategories = individual micro-clusters within this macro-category
        subcategories = []
        sub_centroid_rows = []
        for mc in member_micro:
            if use_llm and is_llm_available():
                llm_sub = await name_cluster(mc["contents"][:5])
                if llm_sub:
                    mc_id = mc["id"]
                    sub_id = f"{cat_id}/{llm_sub.get('id', f'sub-{mc_id}')}"
                    sub_name = llm_sub.get("name", mc["summary"][:40])
                    sub_desc = llm_sub.get("description", mc["summary"])
                else:
                    sub_id = f"{cat_id}/sub-{mc['id']}"
                    sub_name = mc["summary"][:60]
                    sub_desc = mc["summary"]
            else:
                sub_id = f"{cat_id}/sub-{mc['id']}"
                sub_name = mc["summary"][:60]
                sub_desc = mc["summary"]

            subcategories.append(Subcategory(id=sub_id, name=sub_name, description=sub_desc))
            sub_centroid_rows.append(
                (sub_id, cat_id, sub_name, sub_desc, mc["centroid"].tolist(), mc["doc_count"])
            )

        category = Category(id=cat_id, name=cat_name, description=cat_desc, subcategories=subcategories)
        categories.append(category)
        centroid_rows.append((cat_id, None, cat_name, cat_desc, macro_centroid.tolist(), total_docs))
        centroid_rows.extend(sub_centroid_rows)

    # Save
    taxonomy = Taxonomy(version=1, categories=categories)
    await store.save(taxonomy)
    await store.save_centroids(centroid_rows)

    logger.info(f"Bootstrap complete: {len(categories)} categories, {len(centroid_rows)} centroids")
    return taxonomy


async def batch_scan(
    pool: asyncpg.Pool,
    limit: int = 500,
    force: bool = False,
    workspace_id: str = "default",
) -> Dict[str, int]:
    """Batch classify documents that don't have ai-category tags.

    Uses vectorized cosine similarity for speed, then batched DB updates.
    Returns stats: {"classified": N, "uncategorized": M, "skipped": K, "errors": E}
    """
    store = await _get_or_create_store(workspace_id)
    if not store.centroids_loaded:
        return {"error": "No taxonomy loaded. Run bootstrap first."}

    stats = {"classified": 0, "uncategorized": 0, "skipped": 0, "errors": 0}

    async with pool.acquire() as conn:
        if force:
            rows = await conn.fetch(
                """
                SELECT e.guid, e.embedding
                FROM doc_embeddings e
                JOIN documents d ON d.guid = e.guid
                WHERE d.workspace_id = $2
                ORDER BY d.updated_at DESC
                LIMIT $1
                """,
                limit,
                workspace_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT e.guid, e.embedding
                FROM doc_embeddings e
                JOIN documents d ON d.guid = e.guid
                WHERE d.workspace_id = $2
                  AND NOT EXISTS (
                    SELECT 1 FROM unnest(d.tags) AS t
                    WHERE t LIKE 'ai-category:%'
                )
                ORDER BY d.updated_at DESC
                LIMIT $1
                """,
                limit,
                workspace_id,
            )

    if not rows:
        return stats

    # Build centroid matrix for vectorized classification
    cat_centroids = store.get_category_centroids()
    cat_ids = list(cat_centroids.keys())
    cat_names = [cat_centroids[cid][1] for cid in cat_ids]
    centroid_matrix = np.vstack([cat_centroids[cid][0] for cid in cat_ids])
    centroid_norms = np.linalg.norm(centroid_matrix, axis=1, keepdims=True)
    centroid_normed = centroid_matrix / np.maximum(centroid_norms, 1e-10)

    threshold = get_category_threshold()
    sub_threshold = get_subcategory_threshold()

    # Precompute subcategory centroid matrices per category
    sub_matrices: Dict[str, tuple] = {}  # cat_id -> (sub_ids, normed_matrix)
    for cat_id in cat_ids:
        sub_centroids = store.get_subcategory_centroids(cat_id)
        if sub_centroids:
            sub_ids_list = list(sub_centroids.keys())
            sub_matrix = np.vstack([sub_centroids[sid][0] for sid in sub_ids_list])
            sub_norms = np.linalg.norm(sub_matrix, axis=1, keepdims=True)
            sub_normed = sub_matrix / np.maximum(sub_norms, 1e-10)
            sub_matrices[cat_id] = (sub_ids_list, sub_normed)

    # Classify all docs: category + subcategory
    # Each entry: (guid, cat_tag, sub_tag_or_none)
    tag_updates: List[tuple] = []

    for row in rows:
        try:
            raw = row["embedding"]
            if isinstance(raw, str):
                emb = np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)
            else:
                emb = np.array(raw, dtype=np.float32)

            emb_normed = emb / max(np.linalg.norm(emb), 1e-10)
            scores = centroid_normed @ emb_normed
            best_idx = int(np.argmax(scores))
            best_score = float(scores[best_idx])

            if best_score < threshold:
                tag_updates.append((row["guid"], "ai-category:uncategorized", None))
                stats["uncategorized"] += 1
            else:
                best_cat = cat_ids[best_idx]
                sub_tag = None

                # Find best subcategory within winning category
                if best_cat in sub_matrices:
                    sub_ids_list, sub_normed = sub_matrices[best_cat]
                    sub_scores = sub_normed @ emb_normed
                    best_sub_idx = int(np.argmax(sub_scores))
                    best_sub_score = float(sub_scores[best_sub_idx])
                    if best_sub_score >= sub_threshold:
                        sub_tag = f"ai-subcategory:{sub_ids_list[best_sub_idx]}"

                tag_updates.append((row["guid"], f"ai-category:{best_cat}", sub_tag))
                stats["classified"] += 1
        except Exception as e:
            logger.warning(f"Batch classify failed for {row['guid']}: {e}")
            stats["errors"] += 1

    # Batched DB update -- single transaction per batch of 500
    batch_size = 500
    for i in range(0, len(tag_updates), batch_size):
        batch = tag_updates[i:i + batch_size]
        async with pool.acquire() as conn:
            async with conn.transaction():
                for guid, cat_tag, sub_tag in batch:
                    new_tags = [cat_tag]
                    if sub_tag:
                        new_tags.append(sub_tag)
                    # Build concatenated tags array in SQL
                    await conn.execute(
                        """
                        UPDATE documents SET
                            tags = ARRAY(SELECT t FROM unnest(tags) AS t
                                         WHERE t NOT LIKE 'ai-category:%'
                                           AND t NOT LIKE 'ai-subcategory:%')
                                   || $2::text[],
                            updated_at = NOW()
                        WHERE guid = $1
                        """,
                        guid, new_tags,
                    )

    logger.info(f"Batch scan complete: {stats}")
    return stats


# -- Private helpers --


async def _apply_category_tags(
    pool: asyncpg.Pool, guid: str, result: ClassificationResult
) -> None:
    """Add ai-category: and ai-subcategory: tags to document."""
    async with pool.acquire() as conn:
        # Remove existing ai-category/ai-subcategory tags and add new ones
        row = await conn.fetchrow("SELECT tags FROM documents WHERE guid = $1", guid)
        if not row:
            return
        existing = row["tags"] or []

        # Filter out old ai-category/ai-subcategory tags
        new_tags = [t for t in existing if not t.startswith(("ai-category:", "ai-subcategory:"))]

        if result.category_id:
            new_tags.append(f"ai-category:{result.category_id}")
        if result.subcategory_id:
            new_tags.append(f"ai-subcategory:{result.subcategory_id}")

        await conn.execute(
            "UPDATE documents SET tags = $1, updated_at = NOW() WHERE guid = $2",
            new_tags,
            guid,
        )


async def _save_proposal(pool: asyncpg.Pool, source_guid: str, proposal: dict) -> None:
    """Save a new category proposal as a Mesh document."""
    content = json.dumps(
        {"source_guid": source_guid, "proposal": proposal},
        ensure_ascii=False,
    )
    guid = "doc_" + hashlib.md5(
        f"proposal-{source_guid}-{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()[:8]
    tags = ["type:taxonomy-proposal", "topic:ai-categorizer", "status:proposed"]
    now = datetime.now(timezone.utc)
    content_hash = hashlib.md5(content.encode()).hexdigest()

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO documents (guid, content, content_hash, source, tags, created_at, updated_at)
            VALUES ($1, $2, $3, 'system', $4, $5, $5)
            ON CONFLICT (guid) DO NOTHING
            """,
            guid, content, content_hash, tags, now,
        )


def _name_from_tags(
    tags_list: List[List[str]], contents: List[str], cluster_id: int
) -> Tuple[str, str]:
    """Extract a category name from the most common topic: tags in a cluster."""
    topic_counter: Counter = Counter()
    for tags in tags_list:
        for tag in tags:
            if tag.startswith("topic:"):
                topic_counter[tag.split(":", 1)[1]] += 1

    if topic_counter:
        top_topic = topic_counter.most_common(1)[0][0]
        name = top_topic.replace("-", " ").replace("_", " ").title()
    else:
        name = f"Cluster {cluster_id}"

    # Build description from first few doc previews
    previews = [c[:100] for c in contents[:3]]
    description = " | ".join(previews)
    return name, description


def _summarize_micro_cluster(tags_list: List[List[str]], contents: List[str]) -> str:
    """Build a one-line summary of a micro-cluster for the LLM to read."""
    # Collect topic tags
    topic_counter: Counter = Counter()
    type_counter: Counter = Counter()
    for tags in tags_list:
        for tag in tags:
            if tag.startswith("topic:"):
                topic_counter[tag.split(":", 1)[1]] += 1
            elif tag.startswith("type:"):
                type_counter[tag.split(":", 1)[1]] += 1

    parts = []
    if topic_counter:
        top_topics = [t for t, _ in topic_counter.most_common(3)]
        parts.append(f"topics: {', '.join(top_topics)}")
    if type_counter:
        top_type = type_counter.most_common(1)[0][0]
        parts.append(f"type: {top_type}")

    # First lines of 2-3 representative docs
    for c in contents[:3]:
        first_line = c.strip().split("\n")[0][:100]
        parts.append(f'"{first_line}"')

    return f"{len(contents)} docs. " + ". ".join(parts)


def _run_hdbscan(X: np.ndarray, min_cluster_size: int = 5) -> np.ndarray:
    """Run HDBSCAN clustering, return label array."""
    try:
        from sklearn.cluster import HDBSCAN as SklearnHDBSCAN
        clusterer = SklearnHDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=2,
            metric="euclidean",
        )
    except ImportError:
        from sklearn.cluster import OPTICS
        clusterer = OPTICS(min_samples=min_cluster_size, metric="euclidean")
    return clusterer.fit_predict(X)
