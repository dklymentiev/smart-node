"""Embedding-based classifier using cosine similarity to category centroids."""

import logging
from typing import List, Optional

import numpy as np

from .config import get_category_threshold, get_subcategory_threshold
from .models import ClassificationResult
from .taxonomy import TaxonomyStore

logger = logging.getLogger(__name__)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def classify_embedding(
    embedding: List[float],
    store: TaxonomyStore,
) -> ClassificationResult:
    """Classify a document by comparing its embedding to category centroids.

    Returns ClassificationResult with category/subcategory and scores.
    Runs in ~1-2ms (numpy operations only).
    """
    if not store.centroids_loaded:
        return ClassificationResult(category_id="uncategorized", category_name="Uncategorized")

    doc_vec = np.array(embedding, dtype=np.float32)
    cat_threshold = get_category_threshold()
    sub_threshold = get_subcategory_threshold()

    # Find best matching category
    cat_centroids = store.get_category_centroids()
    best_cat_id: Optional[str] = None
    best_cat_name: Optional[str] = None
    best_cat_score: float = 0.0

    for cat_id, (centroid, name) in cat_centroids.items():
        score = cosine_similarity(doc_vec, centroid)
        if score > best_cat_score:
            best_cat_score = score
            best_cat_id = cat_id
            best_cat_name = name

    if best_cat_score < cat_threshold:
        return ClassificationResult(
            category_id="uncategorized",
            category_name="Uncategorized",
            category_score=best_cat_score,
            method="embedding",
        )

    # Find best matching subcategory within winning category
    sub_centroids = store.get_subcategory_centroids(best_cat_id)
    best_sub_id: Optional[str] = None
    best_sub_name: Optional[str] = None
    best_sub_score: float = 0.0

    for sub_id, (centroid, name) in sub_centroids.items():
        score = cosine_similarity(doc_vec, centroid)
        if score > best_sub_score:
            best_sub_score = score
            best_sub_id = sub_id
            best_sub_name = name

    result = ClassificationResult(
        category_id=best_cat_id,
        category_name=best_cat_name,
        category_score=best_cat_score,
        method="embedding",
    )

    if best_sub_score >= sub_threshold:
        result.subcategory_id = best_sub_id
        result.subcategory_name = best_sub_name
        result.subcategory_score = best_sub_score

    return result
