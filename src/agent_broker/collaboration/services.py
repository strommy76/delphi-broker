"""Shared collaboration service singletons."""

from __future__ import annotations

from ..config import OPERATOR_PARTICIPANT_ID, SEED_AGENTS
from ..peer.identity_service import IdentityService
from .collab_service import CollaborationService

IDENTITY_SERVICE = IdentityService.from_agent_registry(SEED_AGENTS)
COLLABORATION_SERVICE = CollaborationService(
    identity_service=IDENTITY_SERVICE,
    operator_participant_id=OPERATOR_PARTICIPANT_ID,
)
