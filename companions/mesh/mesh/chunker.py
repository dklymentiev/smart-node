#!/usr/bin/env python3
"""
Markdown-aware document chunking for full-content embedding.

Splits documents into overlapping chunks suitable for embedding models
with limited token windows. Preserves semantic boundaries by splitting
on markdown headers, paragraph breaks, then sentence breaks.
"""
import re
from typing import List

# Defaults
TARGET_CHUNK_SIZE = 1200   # chars per chunk
OVERLAP_SIZE = 200         # overlap between consecutive chunks
MIN_CHUNK_SIZE = 100       # merge tiny trailing chunks into previous


def chunk_document(
    text: str,
    target_size: int = TARGET_CHUNK_SIZE,
    overlap: int = OVERLAP_SIZE,
    min_size: int = MIN_CHUNK_SIZE,
) -> List[str]:
    """Split a document into overlapping chunks for embedding.

    Short documents (<= target_size) are returned as a single chunk.

    Splitting priority:
      1. Markdown ## headers
      2. Paragraph breaks (blank lines)
      3. Sentence boundaries
      4. Hard character split (last resort)

    Returns a list of non-empty chunk strings.
    """
    if not text or not text.strip():
        return []

    text = text.strip()

    # Short document: return as-is
    if len(text) <= target_size:
        return [text]

    # Split into sections by markdown headers (## and above)
    sections = _split_by_headers(text)

    # Build chunks from sections
    chunks: List[str] = []
    for section in sections:
        if len(section) <= target_size:
            chunks.append(section)
        else:
            # Section too large: split further by paragraphs
            chunks.extend(_split_by_paragraphs(section, target_size))

    # Now merge small chunks and apply overlap
    chunks = _merge_small_chunks(chunks, min_size)
    chunks = _apply_overlap(chunks, overlap, target_size)
    chunks = _merge_small_chunks(chunks, min_size)

    return [c for c in chunks if c.strip()]


def _split_by_headers(text: str) -> List[str]:
    """Split text on markdown headers (lines starting with ## or #)."""
    # Match lines that start with one or more # followed by space
    parts = re.split(r'(?=^#{1,3}\s)', text, flags=re.MULTILINE)
    return [p for p in parts if p.strip()]


def _split_by_paragraphs(text: str, target_size: int) -> List[str]:
    """Split text on paragraph breaks (double newlines). If paragraphs are
    still too large, split on sentences."""
    paragraphs = re.split(r'\n\s*\n', text)
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if not current:
            current = para
        elif len(current) + len(para) + 2 <= target_size:
            current = current + "\n\n" + para
        else:
            chunks.append(current)
            current = para

    if current:
        chunks.append(current)

    # Split any remaining oversized chunks by sentences
    result: List[str] = []
    for chunk in chunks:
        if len(chunk) <= target_size:
            result.append(chunk)
        else:
            result.extend(_split_by_sentences(chunk, target_size))

    return result


def _split_by_sentences(text: str, target_size: int) -> List[str]:
    """Split text on sentence boundaries. Falls back to hard split."""
    # Split on sentence-ending punctuation followed by space
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks: List[str] = []
    current = ""

    for sent in sentences:
        if not current:
            current = sent
        elif len(current) + len(sent) + 1 <= target_size:
            current = current + " " + sent
        else:
            chunks.append(current)
            current = sent

    if current:
        chunks.append(current)

    # Hard-split anything still too large
    result: List[str] = []
    for chunk in chunks:
        if len(chunk) <= target_size:
            result.append(chunk)
        else:
            result.extend(_hard_split(chunk, target_size))

    return result


def _hard_split(text: str, target_size: int) -> List[str]:
    """Last resort: split on word boundaries near target_size."""
    chunks: List[str] = []
    while len(text) > target_size:
        # Find last space before target_size
        split_at = text.rfind(' ', 0, target_size)
        if split_at <= 0:
            split_at = target_size
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


def _merge_small_chunks(chunks: List[str], min_size: int) -> List[str]:
    """Merge chunks smaller than min_size into the previous chunk."""
    if not chunks:
        return chunks

    merged: List[str] = [chunks[0]]
    for chunk in chunks[1:]:
        if len(chunk) < min_size and merged:
            merged[-1] = merged[-1] + "\n\n" + chunk
        else:
            merged.append(chunk)

    return merged


def _apply_overlap(chunks: List[str], overlap: int, target_size: int) -> List[str]:
    """Add overlap from the end of the previous chunk to the start of the next."""
    if len(chunks) <= 1 or overlap <= 0:
        return chunks

    result: List[str] = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = chunks[i - 1]
        # Take last `overlap` chars from previous chunk as context prefix
        if len(prev) > overlap:
            # Find word boundary near overlap point
            prefix_start = prev[-(overlap):]
            # Trim to first word boundary to avoid cutting words
            space_idx = prefix_start.find(' ')
            if space_idx > 0:
                prefix_start = prefix_start[space_idx + 1:]
            overlap_text = prefix_start + "\n\n" + chunks[i]
        else:
            overlap_text = chunks[i]

        # Don't let overlap make chunk exceed 2x target
        if len(overlap_text) <= target_size * 2:
            result.append(overlap_text)
        else:
            result.append(chunks[i])

    return result
