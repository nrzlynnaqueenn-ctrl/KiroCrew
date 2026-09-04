"""Coverage for the guard and failure paths of
:mod:`kiro_crew.dashboard.chat_regenerate`.

``test_dashboard_chat.py::TestRegenerateAndVariants`` covers the happy paths of
regenerate and variant switching. Untested there: ``edit-resend`` in its
entirety (it is not even wired into the shared test app), every 400/404/409
guard on all three endpoints, the readiness latch that must fire BEFORE the
destructive truncation, the persist-failure paths, and the two done-callbacks.

The app here registers the three handlers directly so ``edit-resend`` is
reachable; ``_run_chat`` is always patched, so no backend session is started.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_regenerate import (
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
    api_chat_slot_switch_variant,
)


def _make_regen_app(state) -> web.Application:
    """App exposing all three chat_regenerate routes, including edit-resend."""
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/regenerate", api_chat_slot_regenerate)
    app.router.add_post(
        "/api/chat/slots/{slot}/switch-variant", api_chat_slot_switch_variant
    )
    app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
    return app


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    return st


def _client(state):
    return TestClient(TestServer(_make_regen_app(state)))


async def _busy(slot) -> None:
    """Pin the slot as running with a task that outlives the request."""

    async def _sleep() -> None:
        await asyncio.sleep(10)

    slot.task = asyncio.create_task(_sleep())


# ── regenerate ──


@pytest.mark.asyncio
async def test_regenerate_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/nope/regenerate")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_regenerate_requires_a_preceding_user_message(state) -> None:
    """An assistant-first transcript has nothing to re-send."""
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "unprompted greeting")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 400
        assert (await resp.json())["error"] == "no preceding user message"
    assert [m["role"] for m in slot.messages] == ["assistant"]  # untouched


@pytest.mark.asyncio
async def test_regenerate_rejects_an_empty_user_message(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "")
    slot.append("assistant", "reply to nothing")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 400
        assert (await resp.json())["error"] == "empty user message"


@pytest.mark.asyncio
async def test_readiness_latch_blocks_before_the_truncation(state) -> None:
    """Regenerate persists the truncation, so an unverified backend must be
    rejected BEFORE history is mutated -- a failed turn cannot undo it."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    blocked = web.json_response({"error": "kiro not verified"}, status=503)

    with patch(
        "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
        new=AsyncMock(return_value=blocked),
    ):
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")

    assert resp.status == 503
    assert [m["role"] for m in slot.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_regenerate_survives_a_history_write_failure(state, caplog) -> None:
    """A failed rewrite must not fail the request, and must leave the
    rewrite flag set so the flush loop still archives the dropped tail."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    slot.drain()

    with patch(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
        side_effect=OSError("disk full"),
    ), patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)

    assert "failed to rewrite session history" in caplog.text
    assert slot._pending_rewrite is True


@pytest.mark.asyncio
async def test_unconsumed_variants_are_discarded_with_a_warning(state, caplog) -> None:
    """If the flush never picks the stash up, the done-callback clears it rather
    than leaking it into the next turn."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    slot.drain()

    with patch(
        "kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()
    ):  # returns without consuming _pending_variants
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
                await asyncio.sleep(0)

    assert slot._pending_variants == []
    assert "pending variants not consumed by flush" in caplog.text


@pytest.mark.asyncio
async def test_regenerate_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
        assert resp.status == 409
    finally:
        slot.task.cancel()


# ── switch-variant ──


@pytest.mark.asyncio
async def test_switch_variant_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/nope/switch-variant", json={"index": 0})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_json_body(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/switch-variant",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid JSON"


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_object_body(state) -> None:
    """A JSON array has no .get(), so an unguarded handler would 500."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json=[0])
    assert resp.status == 400


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_non_integer_index(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        for body in ({"index": "second"}, {}):
            resp = await client.post("/api/chat/slots/s1/switch-variant", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid index"


@pytest.mark.asyncio
async def test_switch_variant_needs_an_assistant_row_with_variants(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "only one answer")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        assert (await resp.json())["error"] == "no variants"


@pytest.mark.asyncio
async def test_switch_variant_rejects_a_corrupt_variant_entry(state) -> None:
    """A restored transcript can hold a non-dict entry; picking it would 500."""
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v1")
    slot.messages[-1]["variants"] = ["a bare string, not an entry"]
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        assert (await resp.json())["error"] == "corrupt variant entry"


@pytest.mark.asyncio
async def test_switch_variant_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v1")
    slot.messages[-1]["variants"] = [{"content": "v1"}]
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/switch-variant", json={"index": 0}
            )
        assert resp.status == 409
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_switch_variant_broadcasts_redacted_content(state) -> None:
    """The broadcast leaves the process, so the chosen variant is redacted."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "what is the key?")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [
        {"content": "the key is AKIAIOSFODNN7EXAMPLE", "ts": "t1"},
        {"content": "v2", "ts": "t2"},
    ]
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 200
        assert (await resp.json())["index"] == 0

    msg_type, payload = state.broadcast_ws.call_args.args
    assert msg_type == "chat_variant_switch"
    assert payload["index"] == 0
    assert "AKIAIOSFODNN7EXAMPLE" not in payload["content"]
    # The stored row keeps the real content; only the wire copy is redacted.
    assert slot.messages[-1]["content"] == "the key is AKIAIOSFODNN7EXAMPLE"
    assert slot.messages[-1]["ts"] == "t1"


@pytest.mark.asyncio
async def test_switch_variant_survives_a_persist_failure(state, caplog) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("assistant", "v2")
    slot.messages[-1]["variants"] = [{"content": "v1", "ts": "t1"}, {"content": "v2"}]

    with patch(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
        side_effect=OSError("disk full"),
    ):
        with caplog.at_level("WARNING"):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/switch-variant", json={"index": 0}
                )

    assert resp.status == 200
    assert "switch-variant: failed to persist" in caplog.text
    assert slot.messages[-1]["content"] == "v1"


# ── edit-resend ──


async def _wait_for_dispatch(run) -> None:
    """Wait for the reserved dispatch task to reach ``_run_chat``.

    The edited prompt is dispatched by a task gated on the handler's commit,
    not inline in the handler, so the await lands a few loop passes after the
    response.
    """
    for _ in range(50):
        if run.await_count:
            return
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_edit_resend_by_ts_truncates_and_resends(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "deploy alpha", ts="t1")
    slot.append("assistant", "deployed alpha", ts="t2")
    slot.append("user", "deploy beta", ts="t3")
    slot.append("assistant", "deployed beta", ts="t4")
    slot.drain()
    run = AsyncMock()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"ts": "t3", "content": "  deploy gamma  "},
            )
            assert resp.status == 200
            await _wait_for_dispatch(run)

    assert [m["content"] for m in slot.messages] == [
        "deploy alpha",
        "deployed alpha",
        "deploy gamma",
    ]
    assert run.await_args.args[2] == "deploy gamma"
    assert state.push_slots_update.called


@pytest.mark.asyncio
async def test_edit_resend_by_index_truncates_from_that_row(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
            assert resp.status == 200
            await asyncio.sleep(0)

    assert [m["content"] for m in slot.messages] == ["edited"]


@pytest.mark.asyncio
async def test_edit_resend_redacts_the_edited_content(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "use AKIAIOSFODNN7EXAMPLE please"},
            )
            assert resp.status == 200
            await _wait_for_dispatch(run)

    assert "AKIAIOSFODNN7EXAMPLE" not in slot.messages[-1]["content"]
    assert "AKIAIOSFODNN7EXAMPLE" not in run.await_args.args[2]


@pytest.mark.asyncio
async def test_edit_resend_unknown_slot_is_404(state) -> None:
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/nope/edit-resend", json={"index": 0, "content": "x"}
        )
    assert resp.status == 404


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_json_body(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid JSON"


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_object_body(state) -> None:
    """A valid-JSON array has no .get() -- without the guard this is a 500."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/edit-resend", json=["x"])
    assert resp.status == 400


@pytest.mark.asyncio
async def test_edit_resend_requires_non_blank_content(state) -> None:
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        for body in ({"index": 0, "content": "   "}, {"index": 0}):
            resp = await client.post("/api/chat/slots/s1/edit-resend", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "content is required"


@pytest.mark.asyncio
async def test_edit_resend_unknown_ts_is_rejected(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first", ts="t1")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"ts": "t9", "content": "edited"}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "user message not found for ts"
    assert len(slot.messages) == 1


@pytest.mark.asyncio
async def test_edit_resend_index_must_point_at_a_user_row(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 1, "content": "edited"}
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "index is not a user message"


@pytest.mark.asyncio
async def test_edit_resend_needs_an_index_or_a_ts(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    async with _client(state) as client:
        for body in ({"content": "edited"}, {"index": 99, "content": "edited"}):
            resp = await client.post("/api/chat/slots/s1/edit-resend", json=body)
            assert resp.status == 400
            assert (await resp.json())["error"] == "index or ts required"


@pytest.mark.asyncio
async def test_edit_resend_rejected_while_a_turn_is_in_flight(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )
        assert resp.status == 409
        assert [m["content"] for m in slot.messages] == ["first"]
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_edit_resend_readiness_latch_blocks_before_the_truncation(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    blocked = web.json_response({"error": "kiro not verified"}, status=503)

    with patch(
        "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
        new=AsyncMock(return_value=blocked),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": "edited"}
            )

    assert resp.status == 503
    assert [m["content"] for m in slot.messages] == ["first"]


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_boundary_cannot_be_saved(state) -> None:
    """A failed history rewrite must 503, not silently dispatch.

    The replaced log-and-continue dispatched a replacement turn from state
    that was never persisted, permanently desynchronizing the live window
    from disk. The rewrite failure is now a retryable 503 and the live slot
    keeps the original branch.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)

    with patch(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
        side_effect=OSError("disk full"),
    ), patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_save_failed"

    assert slot.messages == original_messages
    run.assert_not_awaited()
    # The native boundary ran first by design: the discarded native session is
    # not restorable, and a later turn cold-starts from the (unchanged)
    # persisted history.
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_save_is_refused(state) -> None:
    """A save refused by its own guards (returns False) must 503, not dispatch."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    original_messages = list(slot.messages)

    with patch(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
        MagicMock(return_value=False),
    ), patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_save_failed"

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_native_boundary_cannot_be_saved(state) -> None:
    """A failed native-conversation discard must leave the old branch in place."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_prepare_failed"

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_rejects_when_the_sid_flush_fails(state) -> None:
    """The cleared resume sid must be durable before the commit.

    ``discard_conversation`` lands the sid clear in the session map's
    debounced writer; the endpoint forces the durability point and a flush
    failure takes the same 503 path as a failed discard.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_prepare_failed"

    assert slot.messages == original_messages
    run.assert_not_awaited()
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
    state.sessions.aflush.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_resend_refuses_while_a_channel_turn_holds_the_session(state) -> None:
    """A busy session (inbound channel reply in flight) must 409, not discard.

    An inbound channel turn holds the session semaphore while
    ``slot.running`` reads False, so the idle check cannot see it; the
    discard is asked with ``skip_if_busy`` and its refusal surfaces as a
    retryable 409 with the slot untouched.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    original_messages = list(slot.messages)
    state.sessions.discard_conversation = AsyncMock(return_value=False)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "rewind_session_busy"

    assert slot.messages == original_messages
    run.assert_not_awaited()
    state.sessions.aflush.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_success_ordering(state, monkeypatch) -> None:
    """Native clear (+ durable flush) BEFORE the rewrite, slot mutation last.

    The save must observe the UNTOUCHED live slot (the prospective copy is
    what carries the truncated window), so a flush racing the boundaries can
    never persist a rejected edit.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    order: list[str] = []

    async def _discard(key, **kwargs):
        order.append("discard")
        return True

    async def _flush():
        order.append("flush")

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)
    state.sessions.aflush = AsyncMock(side_effect=_flush)

    def _record_save(_state, saved_slot, messages, *, expected_history_key):
        order.append("save")
        # The save goes through the LIVE slot object (so its own
        # expected_history_key guard can see a concurrent rebind), but the
        # live window is untouched: the candidate travels as the snapshot.
        assert saved_slot is slot
        assert saved_slot.messages == original_messages
        assert [m["content"] for m in messages] == ["edited"]
        assert expected_history_key
        return True

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history", _record_save
    )

    async def _run(_state, _slot, content, **kwargs):
        order.append("dispatch")

    run = AsyncMock(side_effect=_run)
    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 200
            await _wait_for_dispatch(run)

    assert order == ["discard", "flush", "save", "dispatch"]
    assert [m["content"] for m in slot.messages] == ["edited"]


@pytest.mark.asyncio
async def test_edit_resend_reserves_the_slot_and_keeps_queue_entries(state) -> None:
    """A send arriving during the awaited boundaries queues and survives.

    The reservation makes ``slot.running`` read True while the durable
    boundaries are pending, so a concurrent send takes the queue path
    instead of starting a competing turn. Unlike rewind, edit-resend also
    keeps entries queued BEFORE the edit: they drain onto the edited
    timeline through their normal trigger, as they always have here.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    earlier_id = slot.queue_append("queued before the edit")
    observed: dict = {}

    async def _discard(key, **kwargs):
        # Runs inside the awaited boundary: the reservation must already
        # be visible, and a producer can still reach the queue.
        observed["running"] = slot.running
        observed["arrived_id"] = slot.queue_append("queued during edit")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 200
            await _wait_for_dispatch(run)

    assert observed["running"] is True
    queued_ids = [entry["id"] for entry in slot._queue]
    assert observed["arrived_id"] in queued_ids
    assert earlier_id in queued_ids


@pytest.mark.asyncio
async def test_edit_resend_failed_save_keeps_live_and_persisted_branch_during_flush(
    state, monkeypatch
) -> None:
    """A flush pending beside a rejected rewrite must save the original window."""
    import threading

    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    original_messages = list(slot.messages)
    slot._question_pending = {"question-1": {"blocking": False}}
    retired = MagicMock()
    slot._on_question_retired = retired
    slot._dirty = True

    save_started = threading.Event()
    fail_save = threading.Event()

    def _wait_then_fail(_state, saved_slot, _messages, **kwargs):
        assert saved_slot is slot
        assert kwargs.get("expected_history_key")
        assert slot._question_pending == {"question-1": {"blocking": False}}
        retired.assert_not_called()
        save_started.set()
        fail_save.wait()
        raise OSError("disk full")

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history", _wait_then_fail
    )
    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
        async with _client(state) as client:
            request_task = asyncio.create_task(
                client.post(
                    "/api/chat/slots/s1/edit-resend",
                    json={"index": 0, "content": "edited"},
                )
            )
            try:
                await asyncio.wait_for(asyncio.to_thread(save_started.wait), timeout=1)
                await asyncio.to_thread(state.flush_slot_now, slot)
                fail_save.set()
                resp = await request_task
            finally:
                fail_save.set()
                if not request_task.done():
                    await request_task

            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_save_failed"

    persisted = state.conversation_log.read_messages("dashboard:s1")
    assert [(m["role"], m["content"]) for m in persisted] == [
        (m["role"], m["content"]) for m in original_messages
    ]
    assert slot.messages == original_messages
    assert slot._question_pending == {"question-1": {"blocking": False}}
    retired.assert_not_called()


@pytest.mark.asyncio
async def test_edit_resend_refuses_the_commit_when_the_slot_is_rebound(state) -> None:
    """A slot rebound to another transcript mid-boundary must not be replaced."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    original_messages = list(slot.messages)

    async def _rebinding_discard(key, **kwargs):
        # Runs inside the awaited boundary: the slot moves to another
        # transcript while the edit persists.
        slot.linked_session_key = "slack:9876543210.999"
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_rebinding_discard)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 503
            # Two fences cover different windows: the save's own
            # expected_history_key guard (rewind_save_failed) catches a rebind
            # visible at write time; the commit-side re-check
            # (rewind_slot_rebound) catches one landing after the save
            # returned. Either refusal is correct -- the point is that the
            # commit never happens.
            assert (await resp.json())["code"] in {
                "rewind_save_failed",
                "rewind_slot_rebound",
            }

    assert slot.messages == original_messages
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_commit_keeps_the_post_save_persistence_witnesses(
    state, monkeypatch
) -> None:
    """The commit must not restore pre-save persistence witnesses.

    The save runs on the LIVE slot and stamps the post-rewrite truth:
    ``_pending_rewrite`` cleared and the ``_disk_*`` witnesses matching the
    truncated file. Copying the prospective slot's pre-save values back
    would re-arm ``_pending_rewrite`` -- so the NEXT flush repeats the
    destructive rewrite and can discard a cross-process append that landed
    in between -- and would move the monotone ``_disk_tail_ts`` floor
    backwards.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    slot._pending_rewrite = True  # pre-save: a rewrite is owed
    slot._disk_tail_ts = "2026-05-21T15:00:00Z"

    def _save_stamps_witnesses(_state, saved_slot, msgs, *, expected_history_key):
        # Emulate the real save's post-write bookkeeping on the live slot.
        saved_slot._pending_rewrite = False
        saved_slot._disk_window_len = len(msgs)
        saved_slot._disk_meta_observed = True
        saved_slot.note_disk_tail("2026-05-21T16:00:05Z")
        return True

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history",
        _save_stamps_witnesses,
    )

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 200
            await _wait_for_dispatch(run)

    assert slot._pending_rewrite is False
    assert slot._disk_window_len == 1
    assert slot._disk_meta_observed is True
    assert slot._disk_tail_ts == "2026-05-21T16:00:05Z"


@pytest.mark.asyncio
async def test_edit_resend_cancelled_mid_save_still_commits_the_landed_rewrite(
    state, monkeypatch
) -> None:
    """A client disconnect during the save must not abandon the rewrite.

    The worker thread finishes the destructive rewrite regardless of the
    handler's fate; on cancellation the handler waits for the worker's
    outcome, commits the live state to match the persisted one, and the
    reserved dispatch task still runs the edited prompt.
    """
    import threading

    from aiohttp.test_utils import make_mocked_request

    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    save_started = threading.Event()
    release = threading.Event()

    def _gated_save(*_args, **_kwargs):
        save_started.set()
        release.wait()
        return True

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_regenerate._save_slot_to_history", _gated_save
    )
    run = AsyncMock()
    monkeypatch.setattr("kiro_crew.dashboard.chat_regenerate._run_chat", run)

    app = _make_regen_app(state)
    fake_request = make_mocked_request(
        "POST", "/api/chat/slots/s1/edit-resend", match_info={"slot": "s1"}, app=app
    )
    fake_request["app"] = ""

    async def _json():
        return {"index": 0, "content": "edited"}

    fake_request.json = _json  # type: ignore[method-assign]
    handler_task = asyncio.create_task(api_chat_slot_edit_resend(fake_request))
    await asyncio.wait_for(asyncio.to_thread(save_started.wait), timeout=2)
    handler_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await handler_task

    # The commit landed: truncated window plus the edited user row.
    assert [m["content"] for m in slot.messages] == ["edited"]
    # The reserved dispatch task still runs the edited prompt.
    await _wait_for_dispatch(run)
    run.assert_awaited_once()
    assert run.await_args.args[2] == "edited"


@pytest.mark.asyncio
async def test_edit_resend_app_cannot_reach_an_unowned_slot(state) -> None:
    """An app-authenticated edit of a slot it does not own is refused.

    This endpoint clears the slot's native conversation identity, so the
    ownership contract is rewind's: denied with a 404 indistinguishable
    from a missing slot (anti-enumeration).
    """
    from aiohttp.test_utils import make_mocked_request

    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    original_messages = list(slot.messages)

    app = _make_regen_app(state)
    fake_request = make_mocked_request(
        "POST", "/api/chat/slots/s1/edit-resend", match_info={"slot": "s1"}, app=app
    )
    fake_request["app"] = "some-app"  # the slot is unscoped (no owner)

    async def _json():
        return {"index": 0, "content": "x"}

    fake_request.json = _json  # type: ignore[method-assign]
    resp = await api_chat_slot_edit_resend(fake_request)
    assert resp.status == 404
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_app_cannot_reach_a_channel_linked_session(state) -> None:
    """An app-owned slot with a channel link must not edit through it.

    ``effective_session_key`` resolves a linked slot to the channel's own
    session, so an app edit would clear the native identity of a
    conversation the app does not own. Denied with the same 404 shape as
    the ownership check (anti-enumeration).
    """
    from aiohttp.test_utils import make_mocked_request

    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot._app = "some-app"
    slot.linked_session_key = "slack:1234567890.123"
    original_messages = list(slot.messages)

    app = _make_regen_app(state)
    fake_request = make_mocked_request(
        "POST", "/api/chat/slots/s1/edit-resend", match_info={"slot": "s1"}, app=app
    )
    fake_request["app"] = "some-app"  # owns the slot, but the slot is linked

    async def _json():
        return {"index": 0, "content": "x"}

    fake_request.json = _json  # type: ignore[method-assign]
    resp = await api_chat_slot_edit_resend(fake_request)
    assert resp.status == 404
    assert slot.messages == original_messages
    state.sessions.discard_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_aborted_hands_a_diverted_send_to_the_queue_drain(state) -> None:
    """A send diverted by the reservation must not strand when the edit aborts.

    The diverted entry took the queue path only because the reservation made
    ``slot.running`` read True; with no turn running afterwards it has no
    drain trigger of its own, so the abort branch must hand it to the
    canonical successor dispatch.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    async def _divert_then_refuse(key, **kwargs):
        slot.queue_append("queued during edit")
        return False  # busy -> 409 abort

    state.sessions.discard_conversation = AsyncMock(side_effect=_divert_then_refuse)
    drain = AsyncMock(return_value=True)

    with patch("kiro_crew.dashboard.chat_regenerate._start_next_queued_turn", new=drain), patch(
        "kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()
    ) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 409
            for _ in range(50):
                if drain.await_count:
                    break
                await asyncio.sleep(0.02)

    drain.assert_awaited_once()
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_resend_aborted_leaves_prior_queue_entries_to_their_own_trigger(state) -> None:
    """Entries queued BEFORE the edit keep waiting; the abort drains nothing.

    Only a reservation-diverted send needs the abort handoff. Pre-existing
    entries were queued against a running turn and drain through that turn's
    own teardown -- starting one here would be a new turn the user never
    asked this edit to start.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()
    earlier_id = slot.queue_append("queued before the edit")
    state.sessions.discard_conversation = AsyncMock(return_value=False)
    drain = AsyncMock(return_value=True)

    with patch("kiro_crew.dashboard.chat_regenerate._start_next_queued_turn", new=drain), patch(
        "kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 409
            assert slot.task is not None
            await asyncio.wait({slot.task}, timeout=2)

    drain.assert_not_awaited()
    assert [entry["id"] for entry in slot._queue] == [earlier_id]


@pytest.mark.asyncio
async def test_edit_resend_counts_the_edited_row_in_the_lifetime_counter(state) -> None:
    """``total_messages`` is the "did new rows land" witness for backfills.

    The edited row is appended on the prospective copy, so its bump lands on
    the copy; the commit must count it on the live slot or the counter
    understates by one row per edit (the pre-port implementation appended
    live and counted).
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()
    before = slot.total_messages

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 200
            await _wait_for_dispatch(run)

    assert slot.total_messages == before + 1


@pytest.mark.asyncio
async def test_edit_resend_rejects_a_non_string_content(state) -> None:
    """A present non-string content is a 400, not an AttributeError 500."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend", json={"index": 0, "content": 123}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "content_invalid"
    assert len(slot.messages) == 1


@pytest.mark.asyncio
async def test_edit_resend_rejects_an_oversized_content(state) -> None:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    async with _client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/edit-resend",
            json={"index": 0, "content": "x" * 32_769},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "content_too_long"
    assert len(slot.messages) == 1


@pytest.mark.asyncio
async def test_edit_resend_keeps_rows_that_landed_during_the_boundaries(state) -> None:
    """A direct append during the awaited boundaries must survive the commit.

    The reservation diverts SENDS to the queue, but direct append doors (a
    workflow result surfacing, a channel mirror row) do not consult it. Such
    a row belongs to the new timeline; the commit must retain it rather than
    erase it with the frozen snapshot -- the pre-port implementation kept it.
    """
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.append("assistant", "answer")
    slot.drain()

    async def _discard_with_arrival(key, **kwargs):
        # Runs inside the awaited boundary: a workflow result lands on the
        # LIVE window through the direct append door.
        slot.append("assistant", "workflow result", "msg msg-a")
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard_with_arrival)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 0, "content": "edited"},
            )
            assert resp.status == 200
            await _wait_for_dispatch(run)

    assert [m["content"] for m in slot.messages] == ["edited", "workflow result"]
    # The arrival is owed to the next flush, not lost from persistence.
    assert slot._dirty is True


@pytest.mark.asyncio
async def test_edit_resend_keeps_an_arrival_when_the_window_is_at_cap(
    state, monkeypatch
) -> None:
    """The arrival survives even when the front-trim keeps the length constant.

    At ``_MAX_SLOT_MESSAGES`` an append trims the FRONT, so the window length
    never grows -- a length-based tail slice reads empty exactly when the
    window is busiest. The commit counts arrivals by the lifetime-counter
    delta instead.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", 4)
    slot = state.get_or_create_slot("s1")
    slot.append("user", "u0")
    slot.append("assistant", "a0")
    slot.append("user", "u1")
    slot.append("assistant", "a1")
    slot.drain()
    assert len(slot.messages) == 4  # at cap

    async def _discard_with_arrival(key, **kwargs):
        # The append lands at the cap: the front row is trimmed, length stays 4.
        slot.append("assistant", "workflow result", "msg msg-a")
        assert len(slot.messages) == 4
        return True

    state.sessions.discard_conversation = AsyncMock(side_effect=_discard_with_arrival)

    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()) as run:
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/edit-resend",
                json={"index": 2, "content": "edited"},
            )
            assert resp.status == 200
            await _wait_for_dispatch(run)

    assert [m["content"] for m in slot.messages] == ["u0", "a0", "edited", "workflow result"]


@pytest.mark.asyncio
async def test_edit_resend_logs_a_failing_background_turn(state, caplog) -> None:
    """The task is fire-and-forget, so its exception must be surfaced by the
    done-callback or it is swallowed entirely."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "first")
    slot.drain()

    with patch(
        "kiro_crew.dashboard.chat_regenerate._run_chat",
        new=AsyncMock(side_effect=RuntimeError("backend exploded")),
    ):
        with caplog.at_level("ERROR"):
            async with _client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend",
                    json={"index": 0, "content": "edited"},
                )
                assert resp.status == 200
                assert slot.task is not None
                await asyncio.wait({slot.task}, timeout=2)
                await asyncio.sleep(0)

    assert "edit-resend _run_chat failed" in caplog.text


# ── machine-readable refusal codes ──
# The tests above pin each refusal's human sentence. These pin the `code`
# beside it, which is the half a caller can branch on: "slot is running" is a
# developer sentence that a client must not string-match to tell a BUSY slot
# (retry once the turn ends) from a MISSING one (stop and refresh).


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("regenerate", None),
        ("switch-variant", {"index": 0}),
        ("edit-resend", {"index": 0, "content": "edited"}),
    ],
)
@pytest.mark.asyncio
async def test_every_endpoint_refuses_a_busy_slot_with_slot_running(state, path, body) -> None:
    """All three endpoints share one busy-slot refusal, so they must share one
    code -- a client that special-cases the retryable case cannot be asked to
    learn a different spelling per endpoint."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "hello v1")
    await _busy(slot)
    try:
        async with _client(state) as client:
            resp = await client.post(f"/api/chat/slots/s1/{path}", json=body)
            assert resp.status == 409
            payload = await resp.json()
            assert payload["code"] == "slot_running"
            # The human sentence is unchanged: the code is additive, so an
            # existing client that renders `error` keeps working.
            assert payload["error"] == "slot is running"
    finally:
        slot.task.cancel()


@pytest.mark.parametrize(
    ("path", "body", "status", "code"),
    [
        ("regenerate", None, 404, "slot_not_found"),
        ("switch-variant", {"index": 0}, 404, "slot_not_found"),
        ("edit-resend", {"index": 0, "content": "x"}, 404, "slot_not_found"),
    ],
)
@pytest.mark.asyncio
async def test_unknown_slot_refusals_carry_slot_not_found(state, path, body, status, code) -> None:
    async with _client(state) as client:
        resp = await client.post(f"/api/chat/slots/nope/{path}", json=body)
        assert resp.status == status
        assert (await resp.json())["code"] == code


@pytest.mark.asyncio
async def test_no_variants_refusal_carries_its_own_code(state) -> None:
    """Distinct from a busy slot: nothing to switch to is permanent for this
    row, so a client must not offer a retry."""
    slot = state.get_or_create_slot("s1")
    slot.append("user", "hi")
    slot.append("assistant", "only reply")
    async with _client(state) as client:
        resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
        assert resp.status == 400
        payload = await resp.json()
        assert payload["code"] == "no_variants"
        assert payload["error"] == "no variants"
