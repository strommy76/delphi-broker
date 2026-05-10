"""
--------------------------------------------------------------------------------
FILE:        identity_service.py
PATH:        ~/projects/agent-broker/src/agent_broker/peer/identity_service.py
DESCRIPTION: Peer participant identity lookup backed by the configured agent registry.

CHANGELOG:
2026-05-06 12:55      Codex      [Feature] Preserve participant probe flags and expose is_probe registry lookup.
2026-05-06 09:35      Codex      [Feature] Add Phase 5 participant lookup service for peer delivery boundaries.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Iterable

from .peer_contracts import ParticipantRef


class IdentityService:
    """Resolve participants from an explicit registry snapshot."""

    def __init__(self, participants: Iterable[ParticipantRef]) -> None:
        self._participants = {item.participant_id: item for item in participants}

    @classmethod
    def from_agent_registry(
        cls,
        agents: Iterable[dict],
        *,
        decision_authority_participant_ids: Iterable[str] = (),
    ) -> "IdentityService":
        participants: list[ParticipantRef] = []
        decision_authority_ids = frozenset(decision_authority_participant_ids)
        for agent in agents:
            missing = [
                key
                for key in (
                    "agent_id",
                    "participant_type",
                    "transport_type",
                    "is_probe",
                    "collaboration_governed",
                )
                if key not in agent or agent[key] in (None, "")
            ]
            if missing:
                raise ValueError(
                    f"agent registry entry missing peer identity field(s) {missing}: {agent!r}"
                )
            collaboration_governed = agent["collaboration_governed"]
            if not isinstance(collaboration_governed, bool):
                raise ValueError(
                    "agent registry entry has invalid collaboration_governed "
                    f"{collaboration_governed!r}: {agent!r}"
                )
            participants.append(
                ParticipantRef(
                    participant_id=agent["agent_id"],
                    participant_type=agent["participant_type"],
                    transport_type=agent["transport_type"],
                    is_probe=agent["is_probe"],
                    collaboration_governed=collaboration_governed,
                    is_decision_authority=agent["agent_id"] in decision_authority_ids,
                )
            )
        return cls(participants)

    def resolve(self, participant_id: str) -> ParticipantRef | None:
        return self._participants.get(participant_id)

    def all_participants(self) -> tuple[ParticipantRef, ...]:
        return tuple(
            self._participants[participant_id] for participant_id in sorted(self._participants)
        )

    def is_probe(self, participant_id: str) -> bool:
        participant = self.resolve(participant_id)
        return bool(participant and participant.is_probe)
