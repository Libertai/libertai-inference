from fastapi import APIRouter

router = APIRouter(prefix="/agents", tags=["Agents"])

from src.routes.agents.agents import (  # noqa
    create_agent,
    list_agents,
    get_agent_public_info,
    resubscribe_agent,
    delete_agent,
)
