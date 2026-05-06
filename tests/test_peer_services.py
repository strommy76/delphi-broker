"""
--------------------------------------------------------------------------------
FILE:        test_peer_services.py
PATH:        ~/projects/agent-broker/tests/test_peer_services.py
DESCRIPTION: Service-layer tests for peer identity lookup, audit recording, delivery, polling, acking, and boundary discipline.

CHANGELOG:
2026-05-06 16:15      Codex      [Fix] Add transcript recipient-gate coverage,
                                      segmented query-count assertions, and
                                      collect-all SQL-boundary diagnostics.
2026-05-06 13:32      Codex      [Refactor] Remove dead audit-service seam references and lock permanent hidden-thread semantics.
2026-05-06 13:08      Codex      [Feature] Cover probe segregation, shared services, and batched transcript query behavior.
2026-05-06 11:18      Codex      [Fix] Pin atomic poll/ack rollback behavior and forbid MCP store-boundary regressions.
2026-05-06 09:55      Codex      [Refactor] Extend SQL boundary lint to Phase 6 peer MCP adapter.
2026-05-06 09:47      Codex      [Refactor] Add Block A service-boundary, atomicity, mismatch, and recipient-order coverage.
2026-05-06 09:35      Codex      [Feature] Add Phase 5 peer service coverage for delivery semantics and SQL boundary discipline.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from agent_broker.peer import peer_store
from agent_broker.peer.identity_service import IdentityService
from agent_broker.peer.peer_contracts import (
    AckRequest,
    GetThreadRequest,
    ParticipantRef,
    PollRequest,
    SendRequest,
)
from agent_broker.peer.peer_delivery_service import PeerDeliveryService


def _participant(
    participant_id: str,
    *,
    participant_type: str = "agent",
    transport_type: str = "mcp",
    is_probe: bool = False,
) -> ParticipantRef:
    return ParticipantRef(
        participant_id=participant_id,
        participant_type=participant_type,
        transport_type=transport_type,
        is_probe=is_probe,
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    peer_store.init_peer_schema(conn)
    return conn


def _identity(extra_agents: tuple[str, ...] = ()) -> IdentityService:
    agents = [
        {
            "agent_id": "pi-claude",
            "participant_type": "agent",
            "transport_type": "mcp",
            "is_probe": False,
        },
        {
            "agent_id": "pi-codex",
            "participant_type": "agent",
            "transport_type": "mcp",
            "is_probe": False,
        },
        {
            "agent_id": "prod-codex",
            "participant_type": "agent",
            "transport_type": "mcp",
            "is_probe": False,
        },
        {
            "agent_id": "operator",
            "participant_type": "operator",
            "transport_type": "http",
            "is_probe": False,
        },
        {
            "agent_id": "pi-claude-probe",
            "participant_type": "agent",
            "transport_type": "http",
            "is_probe": True,
        },
        {
            "agent_id": "pi-codex-probe",
            "participant_type": "agent",
            "transport_type": "http",
            "is_probe": True,
        },
    ]
    agents.extend(
        {
            "agent_id": item,
            "participant_type": "agent",
            "transport_type": "mcp",
            "is_probe": False,
        }
        for item in extra_agents
    )
    return IdentityService.from_agent_registry(agents)


def _service() -> PeerDeliveryService:
    return PeerDeliveryService(
        identity_service=_identity(),
        operator_permanently_hidden_thread_ids=frozenset(),
    )


def _service_with_extra_agents(*agent_ids: str) -> PeerDeliveryService:
    return PeerDeliveryService(
        identity_service=_identity(tuple(agent_ids)),
        operator_permanently_hidden_thread_ids=frozenset(),
    )


def _send_request(
    *,
    sender: str = "pi-claude",
    recipients: tuple[str, ...] | None = ("pi-codex",),
    thread_id: str | None = None,
    subject: str | None = "coordination",
    correlation_id: str = "corr-1",
) -> SendRequest:
    return SendRequest(
        from_participant=_participant(sender),
        to_participants=(
            None if recipients is None else tuple(_participant(item) for item in recipients)
        ),
        message_kind="text",
        payload_json={"body": correlation_id},
        content_text=correlation_id,
        correlation_id=correlation_id,
        parent_message_id=None,
        thread_id=thread_id,
        subject=subject,
    )


def _send_and_ack_parent_thread(
    conn: sqlite3.Connection,
) -> tuple[PeerDeliveryService, str, str]:
    delivery = _service()
    sent = delivery.send(conn, _send_request())
    assert sent.message is not None
    assert sent.error is None
    return delivery, sent.message.thread_id, sent.message.message_id


def test_identity_service_resolves_participants_from_agents_json_shape():
    service = _identity()
    assert service.resolve("pi-codex") == _participant("pi-codex")
    assert service.resolve("missing") is None
    assert [item.participant_id for item in service.all_participants()] == [
        "operator",
        "pi-claude",
        "pi-claude-probe",
        "pi-codex",
        "pi-codex-probe",
        "prod-codex",
    ]
    assert service.is_probe("pi-claude-probe") is True
    assert service.is_probe("pi-claude") is False


def test_delivery_send_poll_ack_and_get_thread_round_trip():
    conn = _conn()
    try:
        delivery = _service()
        sent = delivery.send(conn, _send_request())
        assert sent.error is None
        assert sent.message is not None

        polled = delivery.poll(
            conn,
            PollRequest(participant=_participant("pi-codex"), limit=10),
        )
        assert polled.error is None
        assert [message.message_id for message in polled.messages] == [sent.message.message_id]

        thread = delivery.get_thread(
            conn,
            GetThreadRequest(
                participant=_participant("pi-codex"),
                thread_id=sent.message.thread_id,
            ),
        )
        assert thread.error is None
        assert [message.message_id for message in thread.messages] == [sent.message.message_id]

        acked = delivery.ack(
            conn,
            AckRequest(participant=_participant("pi-codex"), message_id=sent.message.message_id),
        )
        assert acked.error is None
        assert acked.acked_ts is not None

        after_ack = delivery.poll(
            conn,
            PollRequest(participant=_participant("pi-codex"), limit=10),
        )
        assert after_ack.messages == ()
    finally:
        conn.close()


def test_send_rejects_missing_parent_message_without_raw_integrity_error():
    conn = _conn()
    try:
        result = _service().send(
            conn,
            SendRequest(
                from_participant=_participant("pi-claude"),
                to_participants=(_participant("pi-codex"),),
                message_kind="text",
                payload_json={"body": "child"},
                content_text="child",
                correlation_id="corr-child",
                parent_message_id="missing-parent",
                thread_id=None,
                subject="coordination",
            ),
        )
    finally:
        conn.close()
    assert result.message is None
    assert result.error is not None
    assert result.error.error == "invalid_payload"
    assert result.error.detail == {"parent_message_id": "missing-parent"}


def test_send_accepts_existing_parent_message_and_none_parent():
    conn = _conn()
    try:
        delivery, thread_id, parent_message_id = _send_and_ack_parent_thread(conn)
        child = delivery.send(
            conn,
            SendRequest(
                from_participant=_participant("pi-codex"),
                to_participants=(_participant("pi-claude"),),
                message_kind="text",
                payload_json={"body": "child"},
                content_text="child",
                correlation_id="corr-child",
                parent_message_id=parent_message_id,
                thread_id=thread_id,
                subject=None,
            ),
        )
    finally:
        conn.close()
    assert child.error is None
    assert child.message is not None
    assert child.message.parent_message_id == parent_message_id


def test_participant_type_mismatch_is_distinct_from_unknown():
    conn = _conn()
    try:
        result = _service().poll(
            conn,
            PollRequest(
                participant=ParticipantRef(
                    participant_id="pi-codex",
                    participant_type="operator",
                    transport_type="mcp",
                ),
                limit=10,
            ),
        )
    finally:
        conn.close()
    assert result.error is not None
    assert result.error.error == "participant_type_mismatch"
    assert result.error.detail is not None
    assert result.error.detail["registry_type"] == "agent"
    assert result.error.detail["claimed_type"] == "operator"


def test_atomic_send_rolls_back_message_receipts_and_audit_on_mid_loop_failure(
    monkeypatch,
):
    conn = _conn()
    original = peer_store._insert_receipt_no_commit
    calls = 0

    def fail_on_second_receipt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fault injection")
        return original(*args, **kwargs)

    monkeypatch.setattr(peer_store, "_insert_receipt_no_commit", fail_on_second_receipt)
    try:
        with pytest.raises(RuntimeError, match="fault injection"):
            _service().send(
                conn,
                _send_request(recipients=("pi-codex", "prod-codex"), correlation_id="corr-fault"),
            )
        messages = list(conn.execute("SELECT * FROM peer_messages"))
        receipts = list(conn.execute("SELECT * FROM peer_receipts"))
        events = list(conn.execute("SELECT * FROM peer_events"))
    finally:
        conn.close()

    assert messages == []
    assert receipts == []
    assert events == []


def test_atomic_poll_rolls_back_delivery_state_when_audit_insert_fails(monkeypatch):
    conn = _conn()
    try:
        delivery = _service()
        sent = delivery.send(conn, _send_request())
        assert sent.message is not None
        original = peer_store._insert_event_no_commit

        def fail_poll_event(*args, **kwargs):
            if kwargs["event_kind"] == "message_polled":
                raise RuntimeError("poll audit fault")
            return original(*args, **kwargs)

        monkeypatch.setattr(peer_store, "_insert_event_no_commit", fail_poll_event)
        with pytest.raises(RuntimeError, match="poll audit fault"):
            delivery.poll(conn, PollRequest(participant=_participant("pi-codex"), limit=10))
        receipt = peer_store.get_receipt(conn, sent.message.message_id, "pi-codex")
        events = list(
            conn.execute(
                "SELECT * FROM peer_events WHERE message_id = ? AND event_kind = 'message_polled'",
                (sent.message.message_id,),
            )
        )
    finally:
        conn.close()
    assert receipt is not None
    assert receipt["delivered_ts"] is None
    assert events == []


def test_atomic_ack_rolls_back_receipt_state_when_audit_insert_fails(monkeypatch):
    conn = _conn()
    try:
        delivery = _service()
        sent = delivery.send(conn, _send_request())
        assert sent.message is not None
        original = peer_store._insert_event_no_commit

        def fail_ack_event(*args, **kwargs):
            if kwargs["event_kind"] == "message_acked":
                raise RuntimeError("ack audit fault")
            return original(*args, **kwargs)

        monkeypatch.setattr(peer_store, "_insert_event_no_commit", fail_ack_event)
        with pytest.raises(RuntimeError, match="ack audit fault"):
            delivery.ack(
                conn,
                AckRequest(
                    participant=_participant("pi-codex"),
                    message_id=sent.message.message_id,
                ),
            )
        receipt = peer_store.get_receipt(conn, sent.message.message_id, "pi-codex")
        events = list(
            conn.execute(
                "SELECT * FROM peer_events WHERE message_id = ? AND event_kind = 'message_acked'",
                (sent.message.message_id,),
            )
        )
    finally:
        conn.close()
    assert receipt is not None
    assert receipt["delivered_ts"] is None
    assert receipt["acked_ts"] is None
    assert events == []


def test_self_message_rejected_as_forbidden_recipient():
    conn = _conn()
    try:
        result = _service().send(conn, _send_request(sender="pi-codex", recipients=("pi-codex",)))
    finally:
        conn.close()
    assert result.message is None
    assert result.error is not None
    assert result.error.error == "forbidden_recipient"


def test_fanout_with_self_recipient_rejected():
    conn = _conn()
    try:
        result = _service().send(
            conn,
            _send_request(sender="pi-claude", recipients=("pi-codex", "pi-claude")),
        )
    finally:
        conn.close()
    assert result.message is None
    assert result.error is not None
    assert result.error.error == "forbidden_recipient"


def test_ack_is_idempotent_without_state_change():
    conn = _conn()
    try:
        delivery = _service()
        sent = delivery.send(conn, _send_request())
        assert sent.message is not None
        first = delivery.ack(
            conn,
            AckRequest(participant=_participant("pi-codex"), message_id=sent.message.message_id),
        )
        second = delivery.ack(
            conn,
            AckRequest(participant=_participant("pi-codex"), message_id=sent.message.message_id),
        )
    finally:
        conn.close()
    assert first.error is None
    assert second.error is not None
    assert second.error.error == "ack_idempotent"
    assert second.acked_ts == first.acked_ts


def test_unacked_messages_redeliver_until_acknowledged():
    conn = _conn()
    try:
        delivery = _service()
        sent = delivery.send(conn, _send_request())
        assert sent.message is not None
        first = delivery.poll(conn, PollRequest(participant=_participant("pi-codex"), limit=10))
        second = delivery.poll(conn, PollRequest(participant=_participant("pi-codex"), limit=10))
    finally:
        conn.close()
    assert [message.message_id for message in first.messages] == [sent.message.message_id]
    assert [message.message_id for message in second.messages] == [sent.message.message_id]


def test_multi_recipient_fanout_uses_receipt_per_recipient():
    conn = _conn()
    try:
        sent = _service().send(
            conn,
            _send_request(recipients=("pi-codex", "prod-codex"), correlation_id="corr-fanout"),
        )
        assert sent.message is not None
        receipts = peer_store.list_receipts_for_message(conn, sent.message.message_id)
    finally:
        conn.close()
    assert [row["recipient_participant"] for row in receipts] == [
        "pi-codex",
        "prod-codex",
    ]
    assert sent.message.to_participants is not None
    assert [item.participant_id for item in sent.message.to_participants] == [
        "pi-codex",
        "prod-codex",
    ]


def test_recipient_order_preserves_sender_intent():
    conn = _conn()
    try:
        sent = _service_with_extra_agents("z-agent", "a-agent").send(
            conn,
            _send_request(recipients=("z-agent", "a-agent"), correlation_id="corr-order"),
        )
    finally:
        conn.close()
    assert sent.message is not None
    assert sent.message.to_participants is not None
    assert [item.participant_id for item in sent.message.to_participants] == [
        "z-agent",
        "a-agent",
    ]


def test_broadcast_fanout_excludes_sender():
    conn = _conn()
    try:
        sent = _service().send(
            conn, _send_request(recipients=None, correlation_id="corr-broadcast")
        )
        assert sent.message is not None
        receipts = peer_store.list_receipts_for_message(conn, sent.message.message_id)
    finally:
        conn.close()
    assert [row["recipient_participant"] for row in receipts] == [
        "operator",
        "pi-claude-probe",
        "pi-codex",
        "pi-codex-probe",
        "prod-codex",
    ]


def test_operator_thread_list_filters_probe_and_permanently_hidden_threads():
    conn = _conn()
    try:
        delivery = _service()
        visible = delivery.send(conn, _send_request(correlation_id="corr-visible"))
        assert visible.message is not None
        probe = delivery.send(
            conn,
            SendRequest(
                from_participant=_participant(
                    "pi-claude-probe",
                    transport_type="http",
                    is_probe=True,
                ),
                to_participants=(
                    _participant("pi-codex-probe", transport_type="http", is_probe=True),
                ),
                message_kind="text",
                payload_json={"body": "probe"},
                content_text="probe",
                correlation_id="corr-probe",
                parent_message_id=None,
                thread_id=None,
                subject="probe thread",
            ),
        )
        assert probe.message is not None
        default = delivery.list_threads(conn, limit=10, offset=0, include_probes=False)
        included = delivery.list_threads(conn, limit=10, offset=0, include_probes=True)

        excluded_delivery = PeerDeliveryService(
            identity_service=_identity(),
            operator_permanently_hidden_thread_ids=frozenset({visible.message.thread_id}),
        )
        excluded = excluded_delivery.list_threads(conn, limit=10, offset=0, include_probes=False)
        excluded_with_probes = excluded_delivery.list_threads(
            conn,
            limit=10,
            offset=0,
            include_probes=True,
        )
    finally:
        conn.close()

    assert [thread["thread_id"] for thread in default["threads"]] == [visible.message.thread_id]
    assert {thread["thread_id"] for thread in included["threads"]} == {
        visible.message.thread_id,
        probe.message.thread_id,
    }
    assert excluded["threads"] == ()
    assert visible.message.thread_id not in [
        thread["thread_id"] for thread in excluded_with_probes["threads"]
    ]
    assert [thread["thread_id"] for thread in excluded_with_probes["threads"]] == [
        probe.message.thread_id
    ]


def test_operator_transcript_and_message_detail_filter_probe_threads():
    conn = _conn()
    try:
        delivery = _service()
        probe = delivery.send(
            conn,
            SendRequest(
                from_participant=_participant(
                    "pi-claude-probe",
                    transport_type="http",
                    is_probe=True,
                ),
                to_participants=(
                    _participant("pi-codex-probe", transport_type="http", is_probe=True),
                ),
                message_kind="text",
                payload_json={"body": "probe"},
                content_text="probe",
                correlation_id="corr-probe-detail",
                parent_message_id=None,
                thread_id=None,
                subject="probe detail",
            ),
        )
        assert probe.message is not None
        with pytest.raises(ValueError):
            delivery.get_thread_transcript(
                conn,
                probe.message.thread_id,
                include_probes=False,
                requires_recipient_check=False,
            )
        with pytest.raises(ValueError):
            delivery.get_message_detail(
                conn,
                probe.message.message_id,
                include_probes=False,
            )
        transcript = delivery.get_thread_transcript(
            conn,
            probe.message.thread_id,
            include_probes=True,
            requires_recipient_check=False,
        )
        detail = delivery.get_message_detail(
            conn,
            probe.message.message_id,
            include_probes=True,
        )
    finally:
        conn.close()

    assert transcript["messages"][0]["message"]["message_id"] == probe.message.message_id
    assert detail["message"]["message_id"] == probe.message.message_id


def _seed_many_message_thread(
    conn: sqlite3.Connection,
    delivery: PeerDeliveryService,
    *,
    count: int,
) -> str:
    first = delivery.send(conn, _send_request(correlation_id="corr-many-0"))
    assert first.message is not None
    thread_id = first.message.thread_id
    for index in range(1, count):
        sent = delivery.send(
            conn,
            _send_request(
                thread_id=thread_id,
                subject=None,
                correlation_id=f"corr-many-{index}",
            ),
        )
        assert sent.message is not None
    return thread_id


def test_poll_thread_and_transcript_batch_receipts_and_events():
    conn = _conn()
    try:
        delivery = _service()
        thread_id = _seed_many_message_thread(conn, delivery, count=12)
        seen: list[str] = []

        def trace(statement: str) -> None:
            upper = statement.upper()
            if upper.startswith("SELECT") and ("PEER_RECEIPTS" in upper or "PEER_EVENTS" in upper):
                seen.append(upper)

        def assert_selects(operation: str, *, max_receipts: int, max_events: int) -> None:
            receipt_selects = [item for item in seen if "PEER_RECEIPTS" in item]
            event_selects = [item for item in seen if "PEER_EVENTS" in item]
            assert len(receipt_selects) <= max_receipts, (operation, receipt_selects)
            assert len(event_selects) <= max_events, (operation, event_selects)

        conn.set_trace_callback(trace)
        delivery.poll(conn, PollRequest(participant=_participant("pi-codex"), limit=12))
        conn.set_trace_callback(None)
        assert_selects("poll", max_receipts=2, max_events=2)
        seen.clear()

        conn.set_trace_callback(trace)
        delivery.get_thread(
            conn,
            GetThreadRequest(participant=_participant("pi-codex"), thread_id=thread_id),
        )
        conn.set_trace_callback(None)
        assert_selects("get_thread", max_receipts=2, max_events=2)
        seen.clear()

        conn.set_trace_callback(trace)
        delivery.get_thread_transcript(
            conn,
            thread_id,
            include_probes=True,
            requires_recipient_check=False,
        )
        conn.set_trace_callback(None)
        assert_selects("get_thread_transcript", max_receipts=2, max_events=2)
    finally:
        conn.close()


def test_forbidden_thread_reader_is_rejected():
    conn = _conn()
    try:
        delivery = _service()
        sent = delivery.send(conn, _send_request())
        assert sent.message is not None
        response = delivery.get_thread(
            conn,
            GetThreadRequest(
                participant=_participant("prod-codex"),
                thread_id=sent.message.thread_id,
            ),
        )
    finally:
        conn.close()
    assert response.error is not None
    assert response.error.error == "forbidden_recipient"


def test_forbidden_thread_transcript_reader_is_rejected():
    conn = _conn()
    try:
        delivery = _service()
        sent = delivery.send(conn, _send_request())
        assert sent.message is not None
        with pytest.raises(PermissionError):
            delivery.get_thread_transcript(
                conn,
                sent.message.thread_id,
                include_probes=True,
                participant=_participant("prod-codex"),
            )
        transcript = delivery.get_thread_transcript(
            conn,
            sent.message.thread_id,
            include_probes=True,
            participant=_participant("pi-codex"),
        )
    finally:
        conn.close()
    assert transcript["thread"]["thread_id"] == sent.message.thread_id


def test_peer_store_events_are_append_only():
    conn = _conn()
    try:
        event = peer_store.insert_event(
            conn,
            event_id="event-service-append-only",
            message_id=None,
            participant_id="pi-claude",
            event_kind="service_test",
            event_ts=peer_store.utc_now(),
            detail_json={"ok": True},
        )
        try:
            conn.execute(
                "UPDATE peer_events SET event_kind = ? WHERE event_id = ?",
                ("mutated", event["event_id"]),
            )
        except sqlite3.IntegrityError as exc:
            error_text = str(exc)
        else:
            error_text = ""
    finally:
        conn.close()
    assert "append-only" in error_text


_SQL_PATTERN = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|PRAGMA)\b")
_PEER_STORE_GUARDED_FILES = {"peer_mcp_tools.py", "peer_web.py", "peer_api.py"}


def _peer_boundary_violations(path_name: str, source: str) -> list[str]:
    violations: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {
            "execute",
            "executemany",
            "executescript",
            "cursor",
        }:
            violations.append(f"{path_name}: direct DB call attribute {node.attr!r}")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _SQL_PATTERN.search(node.value.upper())
        ):
            violations.append(f"{path_name}: embedded SQL string {node.value!r}")
        if (
            path_name in _PEER_STORE_GUARDED_FILES
            and isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "peer_store"
        ):
            violations.append(f"{path_name}: direct peer_store attribute {node.attr!r}")
        if (
            path_name in _PEER_STORE_GUARDED_FILES
            and isinstance(node, (ast.Import, ast.ImportFrom))
            and any(alias.name == "peer_store" for alias in node.names)
        ):
            violations.append(f"{path_name}: direct peer_store import")
    return violations


def _assert_peer_boundary_clean(path_name: str, source: str) -> None:
    violations = _peer_boundary_violations(path_name, source)
    assert not violations, "\n".join(violations)


def test_peer_boundaries_do_not_contain_sql_or_direct_execute_calls():
    root = Path(__file__).resolve().parents[1]
    boundary_paths = [
        root / "src" / "agent_broker" / "peer" / "identity_service.py",
        root / "src" / "agent_broker" / "peer" / "peer_api.py",
        root / "src" / "agent_broker" / "peer" / "peer_delivery_service.py",
        root / "src" / "agent_broker" / "peer" / "peer_mcp_tools.py",
        root / "src" / "agent_broker" / "peer" / "peer_web.py",
    ]
    for path in boundary_paths:
        _assert_peer_boundary_clean(path.name, path.read_text())


def test_peer_boundary_lint_catches_store_access_regressions():
    source = """from . import peer_store

def regression(conn):
    conn.execute("SELECT * FROM peer_messages")
    peer_store.init_peer_schema(conn)
"""
    for path_name in _PEER_STORE_GUARDED_FILES:
        violations = _peer_boundary_violations(path_name, source)
        assert len(violations) >= 3
