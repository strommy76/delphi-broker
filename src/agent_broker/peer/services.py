"""
--------------------------------------------------------------------------------
FILE:        services.py
PATH:        ~/projects/agent-broker/src/agent_broker/peer/services.py
DESCRIPTION: Shared peer service singletons backed by the explicit runtime configuration snapshot.

CHANGELOG:
2026-05-06 13:31      Codex      [Refactor] Remove dead peer audit service construction and use permanent hidden-thread config.
2026-05-06 13:00      Codex      [Refactor] Centralize peer identity and delivery service construction for all boundaries.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

from ..config import OPERATOR_PARTICIPANT_ID, OPERATOR_PERMANENTLY_HIDDEN_THREAD_IDS, SEED_AGENTS
from .identity_service import IdentityService
from .peer_delivery_service import PeerDeliveryService

IDENTITY_SERVICE = IdentityService.from_agent_registry(
    SEED_AGENTS,
    decision_authority_participant_ids=(OPERATOR_PARTICIPANT_ID,),
)
DELIVERY_SERVICE = PeerDeliveryService(
    identity_service=IDENTITY_SERVICE,
    operator_permanently_hidden_thread_ids=OPERATOR_PERMANENTLY_HIDDEN_THREAD_IDS,
)
