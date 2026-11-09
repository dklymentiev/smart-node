"""Message router: decides whether to use a direct agent or Rein workflow."""

import logging
import re
from pathlib import Path
from typing import Union

from models.actions import DirectAgent, ReinWorkflow
from core.specialist_loader import SpecialistLoader
from core.channel_routing import resolve_route, CLIENT_LOCAL_INTENTS, COMMAND_ENABLED_SPECIALISTS

logger = logging.getLogger("brin.router")

FLOWS_DIR = "/srv/agents/flows"

# Workflow trigger keywords (flow_name -> keywords)
WORKFLOW_TRIGGERS = {
    "deliberation": ["deliberation", "deliberate"],
    "code-review": ["code review"],
    "security-audit": ["security audit"],
    "brainstorm": ["brainstorm"],
}


class Router:
    """Routes incoming messages to appropriate handler."""

    def __init__(self, loader: SpecialistLoader, intent_service=None, flows_dir: str = FLOWS_DIR):
        self.loader = loader
        self.intent_service = intent_service
        self.flows_dir = Path(flows_dir)

    async def route(self, message: str, context: dict = None) -> Union[DirectAgent, ReinWorkflow]:
        """Determine how to handle a message.

        Priority:
        1. @mention of specific specialist -> DirectAgent
        2. Workflow trigger keywords -> ReinWorkflow
        3. Channel routing (if channel in context) -> classify intent + route
        4. Default -> DirectAgent("main") or first available
        """
        context = context or {}
        text = message.strip()

        # 1. Check for @specialist mention
        mention_match = re.match(r"^@([\w-]+)\s*(.*)", text, re.DOTALL)
        if mention_match:
            specialist_id = mention_match.group(1)
            remaining = mention_match.group(2).strip()

            # Verify specialist exists
            if self.loader.get(specialist_id):
                return DirectAgent(
                    specialist_id=specialist_id,
                    message=remaining or text,
                    allow_commands=specialist_id in COMMAND_ENABLED_SPECIALISTS,
                    context=context.get("history"),
                )

        # 2. Check for workflow triggers
        text_lower = text.lower()
        for flow_name, keywords in WORKFLOW_TRIGGERS.items():
            for kw in keywords:
                if kw in text_lower:
                    # Verify flow exists on disk
                    flow_dir = self.flows_dir / flow_name
                    if flow_dir.exists():
                        return ReinWorkflow(
                            flow_name=flow_name,
                            input_data={"topic": text},
                            message=text,
                        )

        # 3. Channel-based routing (intent classification + routing table)
        channel = context.get("channel")
        if channel and self.intent_service:
            try:
                # The classifier is off: a model call before every message, failing
                # every time on a node (119 of 119, 2026-10-31), so every route already
                # was "unknown".
                # result = await self.intent_service.classify(text)
                # intent = result.intent if hasattr(result, "intent") else str(result)
                intent = "unknown"

                # Skip client-local intents (they should never reach here, but safety check)
                if intent not in CLIENT_LOCAL_INTENTS:
                    route_result = resolve_route(intent, channel)
                    logger.info(
                        "Channel routing: intent=%s channel=%s -> specialist=%s commands=%s",
                        intent, channel, route_result.specialist_id, route_result.allow_commands,
                    )
                    return DirectAgent(
                        specialist_id=route_result.specialist_id,
                        message=text,
                        allow_commands=route_result.allow_commands,
                        context=context.get("history"),
                    )
            except Exception as e:
                logger.warning("Channel routing failed: %s, falling back to default", e)

        # 4. Default: main agent
        default_id = context.get("default_agent", "main")

        # If "main" specialist doesn't exist, use generic response
        if not self.loader.get(default_id):
            # Pick first available specialist or fallback
            specialists = self.loader.list()
            default_id = specialists[0].id if specialists else "assistant"

        return DirectAgent(
            specialist_id=default_id,
            message=text,
            context=context.get("history"),
        )

    def available_flows(self) -> list[str]:
        """List available Rein workflow names."""
        if not self.flows_dir.exists():
            return []
        return [
            d.name for d in sorted(self.flows_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".")
        ]
