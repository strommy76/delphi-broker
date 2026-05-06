"""
--------------------------------------------------------------------------------
FILE:        test_peer_operator.py
PATH:        ~/projects/agent-broker/tests/test_peer_operator.py
DESCRIPTION: Operator API and web coverage for Phase 7 peer transcript visibility.

CHANGELOG:
2026-05-06 13:33      Codex      [Refactor] Remove audit-service test construction and lock mark-read empty-body behavior.
2026-05-06 13:09      Codex      [Fix] Bind operator mark-read tests to configured identity and probe filtering.
2026-05-06 11:34      Codex      [Feature] Add Phase 7 operator transcript API, web, and mark-read tests.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

from agent_broker.peer import peer_store
from agent_broker.peer.identity_service import IdentityService
from agent_broker.peer.peer_contracts import ParticipantRef, SendRequest
from agent_broker.peer.peer_delivery_service import PeerDeliveryService


def _participant(participant_id: str) -> ParticipantRef:
    if participant_id == "operator":
        return ParticipantRef(
            participant_id=participant_id,
            participant_type="operator",
            transport_type="http",
        )
    if participant_id.endswith("-probe"):
        return ParticipantRef(
            participant_id=participant_id,
            participant_type="agent",
            transport_type="http",
            is_probe=True,
        )
    return ParticipantRef(
        participant_id=participant_id,
        participant_type="agent",
        transport_type="mcp",
    )


def _delivery(api_harness) -> PeerDeliveryService:
    return PeerDeliveryService(
        identity_service=IdentityService.from_agent_registry(api_harness.config.SEED_AGENTS),
        operator_permanently_hidden_thread_ids=api_harness.config.OPERATOR_PERMANENTLY_HIDDEN_THREAD_IDS,
    )


def _seed_peer_message(api_harness, *, recipient: str = "prod-codex") -> tuple[str, str]:
    conn = api_harness.database.get_connection(api_harness.config.DB_PATH)
    try:
        api_harness.database.init_db(conn)
        peer_store.init_peer_schema(conn)
        sent = _delivery(api_harness).send(
            conn,
            SendRequest(
                from_participant=_participant("prod-claude"),
                to_participants=(_participant(recipient),),
                message_kind="text",
                payload_json={"body": "operator transcript"},
                content_text="operator transcript",
                correlation_id="corr-operator-transcript",
                parent_message_id=None,
                thread_id=None,
                subject="operator transcript",
            ),
        )
    finally:
        conn.close()
    assert sent.message is not None
    assert sent.error is None
    return sent.message.thread_id, sent.message.message_id


def test_peer_web_threads_requires_operator_session(api_harness):
    response = api_harness.client.get(
        "/web/peer/threads",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/web/login"


def test_authed_operator_can_list_threads_and_view_transcript(api_harness, operator_token):
    thread_id, _ = _seed_peer_message(api_harness)
    api_harness.client.cookies.set("op_token", operator_token)
    threads = api_harness.client.get("/web/peer/threads")
    assert threads.status_code == 200, threads.text
    assert "Agent Broker" in threads.text
    assert "Peer Transcript" in threads.text
    assert "peer_send" not in threads.text
    assert "peer_poll" not in threads.text
    assert "peer_ack" not in threads.text

    transcript = api_harness.client.get(f"/web/peer/threads/{thread_id}")
    assert transcript.status_code == 200, transcript.text
    assert "operator transcript" in transcript.text
    assert "Peer Transcript" in transcript.text


def test_peer_operator_api_lists_threads_and_message_detail(api_harness):
    thread_id, message_id = _seed_peer_message(api_harness)
    threads = api_harness.client.get("/api/v1/peer/threads")
    assert threads.status_code == 200, threads.text
    assert [thread["thread_id"] for thread in threads.json()["threads"]] == [thread_id]

    thread = api_harness.client.get(f"/api/v1/peer/threads/{thread_id}")
    assert thread.status_code == 200, thread.text
    assert thread.json()["messages"][0]["message"]["message_id"] == message_id

    message = api_harness.client.get(f"/api/v1/peer/messages/{message_id}")
    assert message.status_code == 200, message.text
    assert message.json()["message"]["message_id"] == message_id


def test_peer_operator_mark_read_binds_to_configured_identity(api_harness):
    _, message_id = _seed_peer_message(api_harness)
    spoof = api_harness.client.post(
        f"/api/v1/peer/messages/{message_id}/mark_read",
        json={"recipient_participant": "prod-codex"},
    )
    assert spoof.status_code == 422

    forbidden = api_harness.client.post(f"/api/v1/peer/messages/{message_id}/mark_read")
    assert forbidden.status_code == 403

    _, operator_message_id = _seed_peer_message(api_harness, recipient="operator")
    accepted = api_harness.client.post(f"/api/v1/peer/messages/{operator_message_id}/mark_read")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["recipient_participant"] == "operator"

    detail = api_harness.client.get(f"/api/v1/peer/messages/{operator_message_id}").json()
    assert "message_read" in [event["event_kind"] for event in detail["events"]]


def test_peer_operator_mark_read_rejects_non_empty_body(api_harness):
    _, operator_message_id = _seed_peer_message(api_harness, recipient="operator")
    spoof = api_harness.client.post(
        f"/api/v1/peer/messages/{operator_message_id}/mark_read",
        json={"recipient_participant": "operator"},
    )
    assert spoof.status_code == 422


def test_peer_operator_default_excludes_probe_threads_and_query_can_include(
    api_harness,
    operator_token,
):
    conn = api_harness.database.get_connection(api_harness.config.DB_PATH)
    try:
        api_harness.database.init_db(conn)
        peer_store.init_peer_schema(conn)
        sent = _delivery(api_harness).send(
            conn,
            SendRequest(
                from_participant=_participant("pi-claude-probe"),
                to_participants=(_participant("pi-codex-probe"),),
                message_kind="text",
                payload_json={"body": "probe transcript"},
                content_text="probe transcript",
                correlation_id="corr-operator-probe",
                parent_message_id=None,
                thread_id=None,
                subject="probe transcript",
            ),
        )
    finally:
        conn.close()
    assert sent.message is not None

    api_harness.client.cookies.set("op_token", operator_token)
    hidden = api_harness.client.get("/api/v1/peer/threads")
    shown = api_harness.client.get("/api/v1/peer/threads?include_probes=true")
    web_hidden = api_harness.client.get("/web/peer/threads")
    web_shown = api_harness.client.get("/web/peer/threads?limit=25&offset=0&include_probes=true")
    transcript_with_probes = api_harness.client.get(
        f"/web/peer/threads/{sent.message.thread_id}?include_probes=true"
    )

    assert hidden.status_code == 200, hidden.text
    assert shown.status_code == 200, shown.text
    assert sent.message.thread_id not in [
        thread["thread_id"] for thread in hidden.json()["threads"]
    ]
    assert sent.message.thread_id in [thread["thread_id"] for thread in shown.json()["threads"]]
    assert "probe transcript" not in web_hidden.text
    assert "probe transcript" in web_shown.text
    assert "Hide probes" in web_shown.text
    assert "limit=25" in web_shown.text
    assert "offset=0" in web_shown.text
    assert "/web/peer/threads?include_probes=true" in transcript_with_probes.text


def test_peer_adapters_share_service_singletons(api_harness):
    from agent_broker.peer import peer_api, peer_mcp_tools, peer_web, services

    assert peer_api.DELIVERY_SERVICE is services.DELIVERY_SERVICE
    assert peer_web.DELIVERY_SERVICE is services.DELIVERY_SERVICE
    assert peer_mcp_tools.DELIVERY_SERVICE is services.DELIVERY_SERVICE
    assert peer_mcp_tools.IDENTITY_SERVICE is services.IDENTITY_SERVICE
