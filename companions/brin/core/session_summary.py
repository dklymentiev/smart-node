"""Session Summary: extracts facts from completed sessions and writes to Mesh workspace.

On session end:
1. Build condensed transcript from messages
2. Send to LLM (via Gateway) for fact extraction + summary
3. Write summary + facts to agent's Mesh workspace
"""

import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("brin.session_summary")

MESH_URL = os.environ.get("MESH_API_URL", "http://localhost:8000").rstrip("/")
# The key comes from the environment only. It used to fall back to a key
# written into this file, so every node shipped with it and spent on it.
def _api_key() -> str:
    return os.environ.get("OPENROUTER_API_KEY", "").strip()


OPENROUTER_MODEL = "anthropic/claude-3.5-haiku"

EXTRACTION_PROMPT = """Analyze this conversation and extract:
1. A one-line summary (what the conversation was about)
2. Key facts about the user (preferences, decisions, stated goals, personal info)
3. Action items or commitments made

Format your response as JSON:
{
  "summary": "one-line summary",
  "facts": ["fact 1", "fact 2", ...],
  "action_items": ["item 1", ...]
}

Only include concrete, reusable facts. Skip greetings and small talk.
If no meaningful facts, return empty arrays.

Conversation:
"""

MAX_TRANSCRIPT_CHARS = 4000


class SessionSummary:
    """Extracts facts from session transcripts and writes to Mesh."""

    def __init__(self, mesh_url: str = MESH_URL):
        self.mesh_url = mesh_url

    async def summarize_and_store(
        self,
        session_data: dict,
        workspace: str,
        agent_name: str,
    ) -> Optional[dict]:
        """Extract facts from session and write to Mesh workspace.

        Args:
            session_data: Full session JSON (with messages array)
            workspace: Mesh workspace name (e.g. 'agent-branding')
            agent_name: Agent name for tagging

        Returns:
            Dict with summary, facts, mesh_guid or None on failure.
        """
        messages = session_data.get("messages", [])
        if len(messages) < 2:
            logger.info("Session too short (%d msgs), skipping summary", len(messages))
            return None

        # Build condensed transcript
        transcript = self._build_transcript(messages)
        if not transcript:
            return None

        # Extract facts via LLM
        extraction = await self._extract_facts(transcript)
        if not extraction:
            return None

        summary = extraction.get("summary", "")
        facts = extraction.get("facts", [])
        action_items = extraction.get("action_items", [])

        if not summary and not facts:
            logger.info("No facts extracted from session")
            return None

        # Build Mesh document content
        session_id = session_data.get("session_id", "unknown")
        content = self._build_mesh_content(
            session_id, agent_name, summary, facts, action_items,
        )

        # Write to Mesh workspace
        mesh_guid = await self._write_to_mesh(content, workspace, agent_name)

        result = {
            "summary": summary,
            "facts": facts,
            "action_items": action_items,
            "mesh_guid": mesh_guid,
        }
        logger.info(
            "Session summary stored: %d facts, %d actions, guid=%s",
            len(facts), len(action_items), mesh_guid,
        )
        return result

    def _build_transcript(self, messages: list) -> str:
        """Build condensed transcript from messages."""
        lines = []
        for msg in messages:
            role = "User" if msg.get("role") == "user" else "Assistant"
            content = msg.get("content", "")
            # Truncate long messages
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"{role}: {content}")

        transcript = "\n".join(lines)
        if len(transcript) > MAX_TRANSCRIPT_CHARS:
            transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n[...truncated]"
        return transcript

    async def _extract_facts(self, transcript: str) -> Optional[dict]:
        """Send transcript to OpenRouter LLM for fact extraction."""
        prompt = EXTRACTION_PROMPT + transcript
        key = _api_key()
        if not key:
            logger.warning("session summary skipped: OPENROUTER_API_KEY is not set")
            return None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": OPENROUTER_MODEL,
                        "messages": [
                            {"role": "system", "content": "You extract structured information from conversations. Always respond with valid JSON only."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 1000,
                    },
                )
                r.raise_for_status()
                data = r.json()
                content = data["choices"][0]["message"]["content"]

            return self._parse_extraction(content)

        except Exception as e:
            logger.warning("Fact extraction via OpenRouter failed: %s", e)
            return None

    def _parse_extraction(self, content: str) -> Optional[dict]:
        """Parse JSON from LLM response (handles markdown code blocks)."""
        text = content.strip()

        # Strip markdown code block if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines (```json and ```)
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            # Try to find JSON in the text
            import re
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

        logger.warning("Could not parse extraction response: %s", text[:200])
        return None

    def _build_mesh_content(
        self,
        session_id: str,
        agent_name: str,
        summary: str,
        facts: list,
        action_items: list,
    ) -> str:
        """Build markdown content for Mesh document."""
        lines = [f"# Session Summary: {session_id}"]
        lines.append(f"\n**Agent:** {agent_name}")
        lines.append(f"\n## Summary\n{summary}")

        if facts:
            lines.append("\n## Facts")
            for fact in facts:
                lines.append(f"- {fact}")

        if action_items:
            lines.append("\n## Action Items")
            for item in action_items:
                lines.append(f"- {item}")

        return "\n".join(lines)

    async def _write_to_mesh(
        self, content: str, workspace: str, agent_name: str,
    ) -> Optional[str]:
        """Write summary document to Mesh workspace."""
        tags = [
            "type:session-memory",
            f"agent:{agent_name}",
            "source:brin-summary",
        ]

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.put(
                    f"{self.mesh_url}/",
                    json={"content": content, "tags": tags},
                    headers={"X-Workspace": workspace},
                )
                r.raise_for_status()
                data = r.json()
                return data.get("guid")
        except Exception as e:
            logger.warning("Failed to write summary to Mesh: %s", e)
            return None
