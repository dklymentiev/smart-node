"""Action types returned by Router."""

from pydantic import BaseModel
from typing import Optional


class DirectAgent(BaseModel):
    """Route message to a single specialist for direct response."""
    type: str = "direct"
    specialist_id: str
    message: str
    context: Optional[str] = None
    allow_commands: bool = False


class ReinWorkflow(BaseModel):
    """Route message to a Rein workflow for multi-agent processing."""
    type: str = "workflow"
    flow_name: str
    input_data: dict
    message: str
