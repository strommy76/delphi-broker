from __future__ import annotations

import sqlite3

from agent_broker.collaboration import collab_store
from agent_broker.collaboration.collab_contracts import (
    CollabAckRequest,
    CollabGetThreadRequest,
    CollabPollRequest,
    OperatorDecisionRequest,
    ProposeMessageRequest,
)
from agent_broker.collaboration.collab_service import CollaborationService
from agent_broker.peer import peer_store
from agent_broker.peer.identity_service import IdentityService
from agent_broker.peer.peer_contracts import ParticipantRef, SendRequest
from agent_broker.peer.peer_delivery_service import PeerDeliveryService


def _participant(
    participant_id: str,
    *,
    participant_type: str = "agent",
    transport_type: str = "mcp",
    is_probe: bool = False,
    collaboration_governed: bool = True,
) -> ParticipantRef:
    return ParticipantRef(
        participant_id=participant_id,
        participant_type=participant_type,
        transport_type=transport_type,
        is_probe=is_probe,
        collaboration_governed=collaboration_governed,
    )


def _identity() -> IdentityService:
    return IdentityService.from_agent_registry(
        [
            {
                "agent_id": "dev-codex",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": True,
            },
            {
                "agent_id": "prod-codex",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": True,
            },
            {
                "agent_id": "flow-claude",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": True,
            },
            {
                "agent_id": "pi-claude",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": False,
            },
            {
                "agent_id": "pi-codex",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": False,
            },
            {
                "agent_id": "operator",
                "participant_type": "operator",
                "transport_type": "http",
                "is_probe": False,
                "collaboration_governed": False,
            },
        ]
    )


def _property_identity() -> IdentityService:
    return IdentityService.from_agent_registry(
        [
            {
                "agent_id": "source-one",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": True,
            },
            {
                "agent_id": "target-two",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": True,
            },
            {
                "agent_id": "direct-one",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": False,
            },
            {
                "agent_id": "direct-two",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": False,
                "collaboration_governed": False,
            },
            {
                "agent_id": "operator",
                "participant_type": "operator",
                "transport_type": "http",
                "is_probe": False,
                "collaboration_governed": False,
            },
        ]
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    peer_store.init_peer_schema(conn)
    collab_store.init_collab_schema(conn)
    return conn


def _service() -> CollaborationService:
    return CollaborationService(identity_service=_identity(), operator_participant_id="operator")


def _proposal(correlation_id: str = "corr-1") -> ProposeMessageRequest:
    return ProposeMessageRequest(
        from_participant=_participant("dev-codex"),
        to_participants=(_participant("prod-codex"),),
        message_kind="text",
        payload_json={"body": "draft"},
        content_text="draft",
        correlation_id=correlation_id,
        thread_id=None,
        subject="coordination",
    )


def _operator_request(
    draft_id: str,
    *,
    decision_type: str = "approve",
    final_content_text: str | None = None,
    to_participants: tuple[ParticipantRef, ...] | None = None,
) -> OperatorDecisionRequest:
    return OperatorDecisionRequest(
        operator_participant=_participant(
            "operator",
            participant_type="operator",
            transport_type="http",
            collaboration_governed=False,
        ),
        draft_id=draft_id,
        decision_type=decision_type,
        final_payload_json=None,
        final_content_text=final_content_text,
        to_participants=to_participants,
        reason=None,
    )


def test_collaboration_approval_gates_delivery_and_ack():
    conn = _conn()
    try:
        service = _service()
        proposed = service.propose(conn, _proposal())
        assert proposed.error is None
        assert proposed.draft is not None

        before = service.poll(
            conn,
            CollabPollRequest(participant=_participant("prod-codex"), limit=10),
        )
        assert before.error is None
        assert before.deliverables == ()
        recipient_thread = service.get_thread(
            conn,
            CollabGetThreadRequest(
                participant=_participant("prod-codex"),
                thread_id=proposed.draft.thread_id,
            ),
        )
        assert recipient_thread.entries == ()

        approved = service.decide(conn, _operator_request(proposed.draft.draft_id))
        assert approved.error is None
        assert approved.deliverable is not None

        pre_delivery_ack = service.ack(
            conn,
            CollabAckRequest(
                participant=_participant("prod-codex"),
                deliverable_id=approved.deliverable.deliverable_id,
            ),
        )
        assert pre_delivery_ack.error is not None
        assert pre_delivery_ack.error.error == "delivery_required"

        after = service.poll(
            conn,
            CollabPollRequest(participant=_participant("prod-codex"), limit=10),
        )
        assert [item.deliverable_id for item in after.deliverables] == [
            approved.deliverable.deliverable_id
        ]

        acked = service.ack(
            conn,
            CollabAckRequest(
                participant=_participant("prod-codex"),
                deliverable_id=approved.deliverable.deliverable_id,
            ),
        )
        assert acked.error is None
        second_ack = service.ack(
            conn,
            CollabAckRequest(
                participant=_participant("prod-codex"),
                deliverable_id=approved.deliverable.deliverable_id,
            ),
        )
        assert second_ack.error is not None
        assert second_ack.error.error == "ack_idempotent"
    finally:
        conn.close()


def test_draft_submission_is_idempotent_and_conflicts_fail_loud():
    conn = _conn()
    try:
        service = _service()
        first = service.propose(conn, _proposal("corr-idem"))
        second = service.propose(conn, _proposal("corr-idem"))
        assert first.draft is not None and second.draft is not None
        assert first.draft.draft_id == second.draft.draft_id

        changed = ProposeMessageRequest(
            from_participant=_participant("dev-codex"),
            to_participants=(_participant("prod-codex"),),
            message_kind="text",
            payload_json={"body": "changed"},
            content_text="changed",
            correlation_id="corr-idem",
            thread_id=None,
            subject="coordination",
        )
        conflict = service.propose(conn, changed)
        assert conflict.error is not None
        assert conflict.error.error == "idempotency_conflict"
    finally:
        conn.close()


def test_operator_edit_redirect_and_reject_preserve_delivery_authority():
    conn = _conn()
    try:
        service = _service()
        edited_draft = service.propose(conn, _proposal("corr-edit")).draft
        assert edited_draft is not None
        edited = service.decide(
            conn,
            _operator_request(
                edited_draft.draft_id,
                decision_type="edit_and_approve",
                final_content_text="edited body",
            ),
        )
        assert edited.deliverable is not None
        assert edited.deliverable.content_text == "edited body"

        redirected_draft = service.propose(conn, _proposal("corr-redirect")).draft
        assert redirected_draft is not None
        redirected = service.decide(
            conn,
            _operator_request(
                redirected_draft.draft_id,
                decision_type="redirect_and_approve",
                to_participants=(_participant("flow-claude"),),
            ),
        )
        assert redirected.deliverable is not None
        original_poll = service.poll(
            conn,
            CollabPollRequest(participant=_participant("prod-codex"), limit=10),
        )
        redirected_ids = {item.deliverable_id for item in original_poll.deliverables}
        assert redirected.deliverable.deliverable_id not in redirected_ids
        new_poll = service.poll(
            conn,
            CollabPollRequest(participant=_participant("flow-claude"), limit=10),
        )
        assert redirected.deliverable.deliverable_id in {
            item.deliverable_id for item in new_poll.deliverables
        }

        rejected_draft = service.propose(conn, _proposal("corr-reject")).draft
        assert rejected_draft is not None
        rejected = service.decide(
            conn,
            _operator_request(rejected_draft.draft_id, decision_type="reject"),
        )
        assert rejected.error is None
        assert rejected.deliverable is None
    finally:
        conn.close()


def test_peer_send_is_blocked_for_collaboration_governed_participants_but_not_peers():
    conn = _conn()
    try:
        peer_service = PeerDeliveryService(
            identity_service=_identity(),
            operator_permanently_hidden_thread_ids=frozenset(),
        )
        blocked = peer_service.send(
            conn,
            SendRequest(
                from_participant=_participant("dev-codex"),
                to_participants=(_participant("prod-codex"),),
                message_kind="text",
                payload_json={"body": "bypass"},
                content_text="bypass",
                correlation_id="corr-bypass",
                parent_message_id=None,
                thread_id=None,
                subject="bypass",
            ),
        )
        assert blocked.message is None
        assert blocked.error is not None
        assert blocked.error.error == "collaboration_required"

        blocked_without_claim = peer_service.send(
            conn,
            SendRequest(
                from_participant=ParticipantRef(
                    participant_id="dev-codex",
                    participant_type="agent",
                    transport_type="mcp",
                ),
                to_participants=(_participant("pi-codex", collaboration_governed=False),),
                message_kind="text",
                payload_json={"body": "registry-owned"},
                content_text="registry-owned",
                correlation_id="corr-registry-owned",
                parent_message_id=None,
                thread_id=None,
                subject="registry-owned",
            ),
        )
        assert blocked_without_claim.error is not None
        assert blocked_without_claim.error.error == "collaboration_required"

        allowed = peer_service.send(
            conn,
            SendRequest(
                from_participant=_participant("pi-claude", collaboration_governed=False),
                to_participants=(_participant("pi-codex", collaboration_governed=False),),
                message_kind="text",
                payload_json={"body": "peer"},
                content_text="peer",
                correlation_id="corr-peer",
                parent_message_id=None,
                thread_id=None,
                subject="peer",
            ),
        )
        assert allowed.error is None
        assert allowed.message is not None
    finally:
        conn.close()


def test_collaboration_authority_uses_participant_properties_not_deployment_names():
    conn = _conn()
    try:
        identity = _property_identity()
        collab_service = CollaborationService(
            identity_service=identity,
            operator_participant_id="operator",
        )
        proposal = collab_service.propose(
            conn,
            ProposeMessageRequest(
                from_participant=_participant("source-one"),
                to_participants=(_participant("target-two"),),
                message_kind="text",
                payload_json={"body": "draft"},
                content_text="draft",
                correlation_id="corr-property-collab",
                thread_id=None,
                subject="property-driven",
            ),
        )
        assert proposal.error is None
        assert proposal.draft is not None

        peer_service = PeerDeliveryService(
            identity_service=identity,
            operator_permanently_hidden_thread_ids=frozenset(),
        )
        blocked = peer_service.send(
            conn,
            SendRequest(
                from_participant=_participant("source-one"),
                to_participants=(_participant("direct-one", collaboration_governed=False),),
                message_kind="text",
                payload_json={"body": "blocked"},
                content_text="blocked",
                correlation_id="corr-property-blocked",
                parent_message_id=None,
                thread_id=None,
                subject="blocked",
            ),
        )
        assert blocked.error is not None
        assert blocked.error.error == "collaboration_required"

        allowed = peer_service.send(
            conn,
            SendRequest(
                from_participant=_participant("direct-one", collaboration_governed=False),
                to_participants=(_participant("direct-two", collaboration_governed=False),),
                message_kind="text",
                payload_json={"body": "allowed"},
                content_text="allowed",
                correlation_id="corr-property-allowed",
                parent_message_id=None,
                thread_id=None,
                subject="allowed",
            ),
        )
        assert allowed.error is None
        assert allowed.message is not None
    finally:
        conn.close()
