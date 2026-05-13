from __future__ import annotations

import sqlite3

from agent_broker.collaboration import collab_store
from agent_broker.collaboration.collab_contracts import (
    CollabAckRequest,
    CollabGetThreadRequest,
    CollabPollRequest,
    OperatorMessageRequest,
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
    is_decision_authority: bool = False,
) -> ParticipantRef:
    return ParticipantRef(
        participant_id=participant_id,
        participant_type=participant_type,
        transport_type=transport_type,
        is_probe=is_probe,
        collaboration_governed=collaboration_governed,
        is_decision_authority=is_decision_authority,
    )


def _identity(*, decision_authority: bool = True) -> IdentityService:
    decision_authority_ids = ("operator",) if decision_authority else ()
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
            {
                "agent_id": "probe-agent",
                "participant_type": "agent",
                "transport_type": "mcp",
                "is_probe": True,
                "collaboration_governed": True,
            },
        ],
        decision_authority_participant_ids=decision_authority_ids,
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
        ],
        decision_authority_participant_ids=("operator",),
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
            is_decision_authority=True,
        ),
        draft_id=draft_id,
        decision_type=decision_type,
        final_payload_json=None,
        final_content_text=final_content_text,
        to_participants=to_participants,
        reason=None,
    )


def _operator_message_request(
    correlation_id: str = "corr-operator-message",
    *,
    content_text: str = "operator-authored message",
) -> OperatorMessageRequest:
    return OperatorMessageRequest(
        operator_participant=_participant(
            "operator",
            participant_type="operator",
            transport_type="http",
            collaboration_governed=False,
            is_decision_authority=True,
        ),
        to_participants=(_participant("prod-codex"),),
        message_kind="text",
        payload_json={"body": content_text},
        content_text=content_text,
        correlation_id=correlation_id,
        thread_id=None,
        subject="operator message",
    )


def test_operator_initiated_message_is_atomic_authority_and_pollable():
    conn = _conn()
    try:
        service = _service()
        created = service.send_operator_message(conn, _operator_message_request())
        assert created.error is None
        assert created.draft is not None
        assert created.decision is not None
        assert created.deliverable is not None
        assert created.draft.from_participant.participant_id == "operator"
        assert created.decision.decision_type == "operator_initiated"
        assert created.deliverable.from_participant.participant_id == "operator"

        events = collab_store.list_events_for_refs(
            conn,
            draft_ids=(created.draft.draft_id,),
            decision_ids=(created.decision.decision_id,),
            deliverable_ids=(created.deliverable.deliverable_id,),
        )
        assert {event["event_kind"] for event in events} == {
            "operator_composed",
            "operator_initiated_message",
            "deliverable_created",
        }
        assert len(events) == 3

        polled = service.poll(
            conn,
            CollabPollRequest(participant=_participant("prod-codex"), limit=10),
        )
        assert polled.error is None
        assert [item.deliverable_id for item in polled.deliverables] == [
            created.deliverable.deliverable_id
        ]
        acked = service.ack(
            conn,
            CollabAckRequest(
                participant=_participant("prod-codex"),
                deliverable_id=created.deliverable.deliverable_id,
            ),
        )
        assert acked.error is None
    finally:
        conn.close()


def test_operator_initiated_idempotency_conflict_fails_loud():
    conn = _conn()
    try:
        service = _service()
        first = service.send_operator_message(conn, _operator_message_request("corr-operator-idem"))
        assert first.error is None
        second = service.send_operator_message(
            conn, _operator_message_request("corr-operator-idem")
        )
        assert second.error is None
        assert second.draft is not None
        assert first.draft is not None
        assert second.draft.draft_id == first.draft.draft_id

        conflict = service.send_operator_message(
            conn,
            _operator_message_request(
                "corr-operator-idem",
                content_text="different operator-authored message",
            ),
        )
        assert conflict.error is not None
        assert conflict.error.error == "idempotency_conflict"
    finally:
        conn.close()


def test_operator_initiated_decision_type_rejected_on_agent_draft_decision_path():
    conn = _conn()
    try:
        service = _service()
        proposed = service.propose(conn, _proposal("corr-operator-type-rejected"))
        assert proposed.error is None
        assert proposed.draft is not None
        decision = service.decide(
            conn,
            _operator_request(
                proposed.draft.draft_id,
                decision_type="operator_initiated",
            ),
        )
        assert decision.error is not None
        assert decision.error.error == "invalid_payload"
    finally:
        conn.close()


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
        operator_thread = service.get_thread(
            conn,
            CollabGetThreadRequest(
                participant=_participant(
                    "operator",
                    participant_type="operator",
                    transport_type="http",
                    collaboration_governed=False,
                    is_decision_authority=True,
                ),
                thread_id=proposed.draft.thread_id,
            ),
        )
        assert operator_thread.error is None
        event_kinds = [
            entry["event"]["event_kind"]
            for entry in operator_thread.entries
            if entry["entry_type"] == "audit_event"
        ]
        assert "draft_created" in event_kinds
        assert "deliverable_created" in event_kinds
        assert "deliverable_polled" in event_kinds
        assert "deliverable_acked" in event_kinds
        entry_timestamps = [_thread_entry_timestamp(entry) for entry in operator_thread.entries]
        assert entry_timestamps == sorted(entry_timestamps)
    finally:
        conn.close()


def test_collaboration_thread_denies_unrelated_participants_without_leaking():
    conn = _conn()
    try:
        service = _service()
        proposed = service.propose(conn, _proposal("corr-thread-access"))
        assert proposed.draft is not None

        unrelated = service.get_thread(
            conn,
            CollabGetThreadRequest(
                participant=_participant("flow-claude"),
                thread_id=proposed.draft.thread_id,
            ),
        )
        assert unrelated.error is not None
        assert unrelated.error.error == "thread_not_found"

        recipient = service.get_thread(
            conn,
            CollabGetThreadRequest(
                participant=_participant("prod-codex"),
                thread_id=proposed.draft.thread_id,
            ),
        )
        assert recipient.error is None
        assert recipient.entries == ()
    finally:
        conn.close()


def test_collaboration_propose_to_existing_thread_requires_membership():
    conn = _conn()
    try:
        service = _service()
        proposed = service.propose(conn, _proposal("corr-thread-member"))
        assert proposed.draft is not None

        injected = service.propose(
            conn,
            ProposeMessageRequest(
                from_participant=_participant("flow-claude"),
                to_participants=(_participant("prod-codex"),),
                message_kind="text",
                payload_json={"body": "foreign"},
                content_text="foreign",
                correlation_id="corr-thread-injection",
                thread_id=proposed.draft.thread_id,
                subject=None,
            ),
        )
        assert injected.draft is None
        assert injected.error is not None
        assert injected.error.error == "forbidden_thread"

        approved = service.decide(conn, _operator_request(proposed.draft.draft_id))
        assert approved.error is None
        assert approved.deliverable is not None
        reply = service.propose(
            conn,
            ProposeMessageRequest(
                from_participant=_participant("prod-codex"),
                to_participants=(_participant("dev-codex"),),
                message_kind="text",
                payload_json={"body": "reply"},
                content_text="reply",
                correlation_id="corr-thread-reply",
                thread_id=proposed.draft.thread_id,
                subject=None,
            ),
        )
        assert reply.error is None
        assert reply.draft is not None
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


def test_operator_decision_authority_comes_from_registry_property():
    conn = _conn()
    try:
        service = CollaborationService(
            identity_service=_identity(decision_authority=False),
            operator_participant_id="operator",
        )
        proposed = service.propose(conn, _proposal("corr-operator-authority"))
        assert proposed.draft is not None

        decision = service.decide(conn, _operator_request(proposed.draft.draft_id))
        assert decision.decision is None
        assert decision.deliverable is None
        assert decision.error is not None
        assert decision.error.error == "forbidden_participant"
    finally:
        conn.close()


def test_duplicate_recipients_fail_loud_before_store_boundary():
    conn = _conn()
    try:
        service = _service()
        duplicate_proposal = ProposeMessageRequest(
            from_participant=_participant("dev-codex"),
            to_participants=(_participant("prod-codex"), _participant("prod-codex")),
            message_kind="text",
            payload_json={"body": "draft"},
            content_text="draft",
            correlation_id="corr-duplicate-proposal",
            thread_id=None,
            subject="coordination",
        )
        proposed = service.propose(conn, duplicate_proposal)
        assert proposed.draft is None
        assert proposed.error is not None
        assert proposed.error.error == "forbidden_recipient"
        assert proposed.error.detail == {"participant_id": "prod-codex"}

        redirect_draft = service.propose(conn, _proposal("corr-duplicate-redirect")).draft
        assert redirect_draft is not None
        redirected = service.decide(
            conn,
            _operator_request(
                redirect_draft.draft_id,
                decision_type="redirect_and_approve",
                to_participants=(_participant("flow-claude"), _participant("flow-claude")),
            ),
        )
        assert redirected.deliverable is None
        assert redirected.error is not None
        assert redirected.error.error == "forbidden_recipient"
        assert redirected.error.detail == {"participant_id": "flow-claude"}
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
        original_thread = service.get_thread(
            conn,
            CollabGetThreadRequest(
                participant=_participant("prod-codex"),
                thread_id=redirected_draft.thread_id,
            ),
        )
        assert original_thread.error is not None
        assert original_thread.error.error == "thread_not_found"
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
        rejected_thread = service.get_thread(
            conn,
            CollabGetThreadRequest(
                participant=_participant("prod-codex"),
                thread_id=rejected_draft.thread_id,
            ),
        )
        assert rejected_thread.error is not None
        assert rejected_thread.error.error == "thread_not_found"
    finally:
        conn.close()


def _thread_entry_timestamp(entry: dict) -> tuple[str, int]:
    if entry["entry_type"] == "draft":
        return (entry["draft"]["created_ts"], 0)
    if entry["entry_type"] == "decision":
        return (entry["decision"]["decision_ts"], 2)
    if entry["entry_type"] == "deliverable":
        return (entry["deliverable"]["created_ts"], 4)
    if entry["entry_type"] == "audit_event":
        order = {
            "draft_created": 1,
            "operator_approve": 3,
            "operator_edit_and_approve": 3,
            "operator_redirect_and_approve": 3,
            "operator_reject": 3,
            "deliverable_created": 5,
            "deliverable_polled": 6,
            "deliverable_acked": 7,
        }.get(entry["event"]["event_kind"], 8)
        return (entry["event"]["event_ts"], order)
    return ("", 99)


def test_operator_decisions_fail_loud_on_invalid_field_combinations():
    conn = _conn()
    try:
        service = _service()
        approve_draft = service.propose(conn, _proposal("corr-invalid-approve")).draft
        assert approve_draft is not None
        invalid_approve = service.decide(
            conn,
            OperatorDecisionRequest(
                operator_participant=_operator_request(approve_draft.draft_id).operator_participant,
                draft_id=approve_draft.draft_id,
                decision_type="approve",
                final_payload_json={"body": "edited"},
                final_content_text=None,
                to_participants=None,
                reason=None,
            ),
        )
        assert invalid_approve.error is not None
        assert invalid_approve.error.error == "invalid_payload"

        edit_draft = service.propose(conn, _proposal("corr-invalid-edit")).draft
        assert edit_draft is not None
        invalid_edit = service.decide(
            conn,
            _operator_request(
                edit_draft.draft_id,
                decision_type="edit_and_approve",
                final_content_text="edited",
                to_participants=(_participant("flow-claude"),),
            ),
        )
        assert invalid_edit.error is not None
        assert invalid_edit.error.error == "invalid_payload"

        redirect_draft = service.propose(conn, _proposal("corr-invalid-redirect")).draft
        assert redirect_draft is not None
        invalid_redirect = service.decide(
            conn,
            OperatorDecisionRequest(
                operator_participant=_operator_request(
                    redirect_draft.draft_id
                ).operator_participant,
                draft_id=redirect_draft.draft_id,
                decision_type="redirect_and_approve",
                final_payload_json=None,
                final_content_text="edited",
                to_participants=(_participant("flow-claude"),),
                reason=None,
            ),
        )
        assert invalid_redirect.error is not None
        assert invalid_redirect.error.error == "invalid_payload"

        reject_draft = service.propose(conn, _proposal("corr-invalid-reject")).draft
        assert reject_draft is not None
        invalid_reject = service.decide(
            conn,
            _operator_request(
                reject_draft.draft_id,
                decision_type="reject",
                to_participants=(_participant("flow-claude"),),
            ),
        )
        assert invalid_reject.error is not None
        assert invalid_reject.error.error == "invalid_payload"
    finally:
        conn.close()


def test_pending_probe_drafts_are_hidden_by_default():
    conn = _conn()
    try:
        service = _service()
        probe_proposal = ProposeMessageRequest(
            from_participant=_participant("probe-agent", is_probe=True),
            to_participants=(_participant("prod-codex"),),
            message_kind="text",
            payload_json={"body": "probe"},
            content_text="probe",
            correlation_id="corr-probe-hidden",
            thread_id=None,
            subject="probe",
        )
        proposed = service.propose(conn, probe_proposal)
        assert proposed.error is None

        assert service.list_pending_drafts(conn, include_probes=False)["drafts"] == []
        visible = service.list_pending_drafts(conn, include_probes=True)["drafts"]
        assert [draft["draft_id"] for draft in visible] == [proposed.draft.draft_id]
    finally:
        conn.close()


def test_approved_unacked_deliverable_redelivers_after_restart(tmp_path):
    db_path = tmp_path / "collab.sqlite"
    first = sqlite3.connect(db_path)
    first.row_factory = sqlite3.Row
    first.execute("PRAGMA foreign_keys=ON")
    peer_store.init_peer_schema(first)
    collab_store.init_collab_schema(first)
    service = _service()
    proposed = service.propose(first, _proposal("corr-restart"))
    assert proposed.draft is not None
    approved = service.decide(first, _operator_request(proposed.draft.draft_id))
    assert approved.error is None
    first.close()

    second = sqlite3.connect(db_path)
    second.row_factory = sqlite3.Row
    second.execute("PRAGMA foreign_keys=ON")
    peer_store.init_peer_schema(second)
    collab_store.init_collab_schema(second)
    try:
        redelivered = service.poll(
            second,
            CollabPollRequest(participant=_participant("prod-codex"), limit=10),
        )
        assert [item.deliverable_id for item in redelivered.deliverables] == [
            approved.deliverable.deliverable_id
        ]
    finally:
        second.close()


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
