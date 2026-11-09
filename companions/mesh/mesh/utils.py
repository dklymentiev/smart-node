#!/usr/bin/env python3
"""
Utility functions for GUID generation and validation
"""
import re
import uuid
from typing import Optional

# GUID format pattern: doc_{8-32_hex_chars} (supports legacy 8-char and new 16-char)
GUID_PATTERN = re.compile(r'^doc_[0-9a-f]{8,32}$')

def generate_document_guid() -> str:
    """Generate document GUID in format doc_{16_hex_chars}"""
    hex_chars = uuid.uuid4().hex[:16]
    return f"doc_{hex_chars}"

def validate_document_guid(guid: str) -> bool:
    """Validate document GUID format"""
    if not isinstance(guid, str):
        return False
    
    return bool(GUID_PATTERN.match(guid))

def normalize_tags(tags: Optional[list]) -> list:
    """Normalize and validate document tags"""
    if not tags:
        return []
    
    if not isinstance(tags, list):
        return []
    
    # Filter out non-string tags and normalize
    normalized = []
    for tag in tags:
        if isinstance(tag, str) and tag.strip():
            normalized.append(tag.strip().lower())
    
    # Remove duplicates while preserving order
    seen = set()
    unique_tags = []
    for tag in normalized:
        if tag not in seen:
            seen.add(tag)
            unique_tags.append(tag)
    
    return unique_tags
