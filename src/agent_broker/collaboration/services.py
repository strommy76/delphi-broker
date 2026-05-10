"""Shared collaboration service singletons."""

from __future__ import annotations

from ..config import OPERATOR_PARTICIPANT_ID
from ..peer.services import IDENTITY_SERVICE
from .collab_service import CollaborationService

COLLABORATION_SERVICE = CollaborationService(
    identity_service=IDENTITY_SERVICE,
    operator_participant_id=OPERATOR_PARTICIPANT_ID,
)
