"""Optional LLM-based classifier using OpenAI-compatible API.

Used for:
1. Refining ambiguous classifications (score between 0.45-0.65)
2. Classifying uncategorized documents
3. Naming clusters during bootstrap
"""

import json
import logging
from typing import Dict, List, Optional

import httpx

from .config import get_llm_api_key, get_llm_api_url, get_llm_model, get_llm_timeout
from .models import ClassificationResult, Taxonomy

logger = logging.getLogger(__name__)


async def classify_with_llm(
    content: str,
    taxonomy: Taxonomy,
    ambiguous_result: Optional[ClassificationResult] = None,
) -> Optional[ClassificationResult]:
    """Classify a document using LLM.

    Args:
        content: Document text (truncated to ~2000 chars).
        taxonomy: Current taxonomy for category list.
        ambiguous_result: If provided, the embedding result that was ambiguous.

    Returns ClassificationResult or None on failure.
    """
    url = get_llm_api_url()
    if not url:
        return None

    categories_desc = "\n".join(
        f"- {c.id}: {c.name} -- {c.description}"
        + (
            "\n  Subcategories: "
            + ", ".join(f"{s.id} ({s.name})" for s in c.subcategories)
            if c.subcategories
            else ""
        )
        for c in taxonomy.categories
    )

    prompt = (
        "Classify this document into one category and optionally one subcategory.\n\n"
        f"Available categories:\n{categories_desc}\n\n"
        f"Document (first 2000 chars):\n{content[:2000]}\n\n"
        'Respond with JSON only: {"category_id": "...", "subcategory_id": "..." or null}\n'
        "If none fit, respond: {\"category_id\": \"uncategorized\", \"subcategory_id\": null}"
    )

    try:
        async with httpx.AsyncClient(timeout=get_llm_timeout()) as client:
            resp = await client.post(
                url.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {get_llm_api_key()}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": get_llm_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 100,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()

            # Parse JSON from response (handle markdown code blocks)
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            result = json.loads(text)

            cat_id = result.get("category_id", "uncategorized")
            sub_id = result.get("subcategory_id")

            # Find names from taxonomy
            cat_name = cat_id
            sub_name = sub_id
            for c in taxonomy.categories:
                if c.id == cat_id:
                    cat_name = c.name
                    for s in c.subcategories:
                        if s.id == sub_id:
                            sub_name = s.name
                            break
                    break

            return ClassificationResult(
                category_id=cat_id,
                category_name=cat_name,
                category_score=0.9,  # LLM confidence proxy
                subcategory_id=sub_id,
                subcategory_name=sub_name,
                subcategory_score=0.9 if sub_id else 0.0,
                method="llm",
            )

    except Exception as e:
        logger.warning(f"LLM classification failed: {e}")
        return None


async def name_cluster(
    representative_docs: List[str],
) -> Optional[Dict[str, str]]:
    """Ask LLM to name a cluster from representative documents.

    Returns {"id": "slug", "name": "Display Name", "description": "..."} or None.
    """
    url = get_llm_api_url()
    if not url:
        return None

    docs_text = "\n---\n".join(doc[:500] for doc in representative_docs[:5])
    prompt = (
        "These documents are in the same topic cluster. "
        "Name this category in 2-3 words.\n\n"
        f"Documents:\n{docs_text}\n\n"
        'Respond JSON only: {"id": "slug-name", "name": "Display Name", '
        '"description": "Brief description of what this category covers"}'
    )

    return await _llm_json_call(prompt, temperature=0.3, max_tokens=150)


async def name_macro_category(
    micro_cluster_summaries: List[str],
) -> Optional[Dict[str, str]]:
    """Ask LLM to name a macro-category from its constituent micro-clusters.

    Each summary describes a group of similar documents.
    LLM sees the big picture and names it in 2 words.

    Returns {"id": "slug", "name": "Display Name", "description": "..."} or None.
    """
    url = get_llm_api_url()
    if not url:
        return None

    summaries_text = "\n".join(
        f"- Group {i+1}: {s}" for i, s in enumerate(micro_cluster_summaries)
    )
    prompt = (
        "Below are descriptions of several groups of documents that are semantically related.\n"
        "What is the common HIGH-LEVEL theme? Name it in exactly 2 words.\n"
        "Think about WHAT these documents are about, not HOW they are structured.\n\n"
        f"{summaries_text}\n\n"
        'Respond JSON only: {"id": "slug-name", "name": "Two Words", '
        '"description": "One sentence describing this category"}'
    )

    return await _llm_json_call(prompt, temperature=0.2, max_tokens=100)


async def _llm_json_call(
    prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 150,
) -> Optional[Dict[str, str]]:
    """Make an LLM call expecting JSON response."""
    url = get_llm_api_url()
    try:
        async with httpx.AsyncClient(timeout=get_llm_timeout()) as client:
            resp = await client.post(
                url.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {get_llm_api_key()}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": get_llm_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(text)
    except Exception as e:
        logger.warning(f"LLM call failed: {e}")
        return None


async def design_taxonomy(
    doc_samples: List[Dict[str, str]],
) -> Optional[List[Dict[str, str]]]:
    """Ask LLM to design a taxonomy from representative document samples.

    Args:
        doc_samples: List of {"preview": "first 150 chars", "tags": "type:worklog, guid:abc"}

    Returns list of {"id": "slug", "name": "Name", "description": "..."} or None.
    """
    url = get_llm_api_url()
    if not url:
        return None

    docs_text = "\n".join(
        f"{i+1}. [{s.get('tags', '')}] {s['preview']}"
        for i, s in enumerate(doc_samples)
    )

    prompt = (
        "You are helping organize a personal knowledge base of a solo developer/entrepreneur.\n"
        "Below are 50 representative documents from this person's collection.\n"
        "Each line shows [existing tags] and the first 150 characters of content.\n\n"
        f"{docs_text}\n\n"
        "Based on these samples, propose 10-15 TOP-LEVEL categories that reflect\n"
        "this person's ACTIVITIES and INTERESTS (not document format).\n\n"
        "Think like the owner organizing their notes:\n"
        "- Active projects they work on (group related docs under project names)\n"
        "- Types of activity (monitoring, research, planning, development)\n"
        "- Personal notes, ideas, decisions\n"
        "- Testing/debugging artifacts\n\n"
        "Rules:\n"
        "- Each category should be 2-3 words max\n"
        "- Categories should be BROAD enough to cover 50+ documents each\n"
        "- Don't create per-platform categories (no 'NPM Monitoring' vs 'PyPI Monitoring')\n"
        "- Instead group by PURPOSE: 'Platform Monitoring' covers all of them\n"
        "- Include an 'Other' category for anything that doesn't fit\n\n"
        "Respond with JSON array only:\n"
        '[{"id": "slug-name", "name": "Display Name", '
        '"description": "What documents belong here"}]\n'
    )

    result = await _llm_json_call(prompt, temperature=0.3, max_tokens=2000)
    if isinstance(result, list):
        return result
    return None


async def classify_docs_batch(
    docs: List[Dict[str, str]],
    categories: List[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    """Ask LLM to classify a batch of documents into given categories.

    Args:
        docs: List of {"guid": "...", "preview": "first 150 chars"}
        categories: List of {"id": "...", "name": "...", "description": "..."}

    Returns {"doc_guid": "category_id", ...} or None.
    """
    url = get_llm_api_url()
    if not url:
        return None

    cat_text = "\n".join(
        f"- {c['id']}: {c['name']} -- {c['description']}" for c in categories
    )

    docs_text = "\n".join(
        f"{d['guid']}: {d['preview']}" for d in docs
    )

    prompt = (
        "Classify each document into exactly one category.\n\n"
        f"Categories:\n{cat_text}\n\n"
        f"Documents:\n{docs_text}\n\n"
        "Respond with JSON object mapping document GUID to category ID:\n"
        '{"doc_guid1": "category-id", "doc_guid2": "category-id", ...}\n'
        "Use the exact category IDs from the list above."
    )

    return await _llm_json_call(prompt, temperature=0.1, max_tokens=4000)


async def propose_new_category(
    content: str,
) -> Optional[Dict[str, str]]:
    """Ask LLM to propose a new category for an uncategorized document.

    Returns {"id": "slug", "name": "Name", "description": "..."} or None.
    """
    url = get_llm_api_url()
    if not url:
        return None

    prompt = (
        "This document doesn't fit any existing category. "
        "Suggest a new category for it.\n\n"
        f"Document (first 1500 chars):\n{content[:1500]}\n\n"
        'Respond JSON only: {"id": "slug-name", "name": "Display Name", '
        '"description": "Brief description of what this category covers"}'
    )

    try:
        async with httpx.AsyncClient(timeout=get_llm_timeout()) as client:
            resp = await client.post(
                url.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {get_llm_api_key()}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": get_llm_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 150,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(text)
    except Exception as e:
        logger.warning(f"LLM category proposal failed: {e}")
        return None
