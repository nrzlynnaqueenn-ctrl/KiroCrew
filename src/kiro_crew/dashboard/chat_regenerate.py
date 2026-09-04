"""Regenerate, variant switch, and edit-resend endpoints."""

from __future__ import annotations

import asyncio
import copy
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_VARIANTS = 20


async def api_chat_slot_regenerate(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/regenerate — regenerate the last assistant reply."""
    # Destructive: this truncates and PERSISTS history before the background
    # turn runs, so a failed turn cannot undo it. Unlike an ordinary send, the
    # readiness latch must be honored BEFORE the mutation.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    async with slot._lock:
        if slot.running:
            return web.json_response(
                {"error": "slot is running", "code": "slot_running"}, status=409
            )

        msgs = slot.messages
        ai_idx = -1
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant":
                ai_idx = i
                break
        if ai_idx < 0:
            return web.json_response(
                {"error": "no assistant message to regenerate", "code": "no_assistant_message"},
                status=400,
            )
        u_idx = -1
        for i in range(ai_idx - 1, -1, -1):
            if msgs[i].get("role") == "user":
                u_idx = i
                break
        if u_idx < 0:
            return web.json_response(
                {"error": "no preceding user message", "code": "no_user_message"}, status=400
            )

        user_msg = msgs[u_idx].get("content", "")
        if not user_msg:
            return web.json_response(
                {"error": "empty user message", "code": "empty_user_message"}, status=400
            )

        ai_msg = msgs[ai_idx]
        _rv = ai_msg.get("variants")
        variants: list[dict] = list(_rv) if isinstance(_rv, list) else []  # type: ignore[arg-type]
        current_entry = {"content": ai_msg.get("content", ""), "ts": ai_msg.get("ts", "")}
        if not any(v.get("content") == current_entry["content"] for v in variants):
            variants.append(current_entry)
        if len(variants) > _MAX_VARIANTS:
            variants = variants[-_MAX_VARIANTS:]

        del slot.messages[u_idx + 1 :]
        slot.invalidate_source_links()
        slot._dirty = True
        slot._resumed_count = 0
        # Window was truncated → next save MUST be the archive-safe rewrite path.
        # If the inline save below fails, the flag keeps the flush loop on the
        # rewrite path so the dropped tail is still archived.
        slot._pending_rewrite = True
        slot._pending_variants = variants

        try:
            msgs_snapshot = list(slot.messages)
            await asyncio.to_thread(_save_slot_to_history, state, slot, msgs_snapshot)
        except Exception:
            logger.warning("Regenerate: failed to rewrite session history", exc_info=True)

        sel().log_api_access(
            caller="dashboard",
            operation="chat.regenerate",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )

        hint = (
            "The user regenerated the previous response. Produce a fresh answer — "
            "vary phrasing, structure, or angle. Do not say you already answered or "
            "reference the prior reply."
        )
        task = asyncio.create_task(
            _run_chat(
                state,
                slot,
                user_msg,
                regenerate_hint=hint,
                _directive_user_origin=not bool(request.get("app", "")),
            )
        )
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _clear_pending_on_done(t: asyncio.Task) -> None:
            if slot._pending_variants:
                if not t.cancelled() and t.exception() is None:
                    logger.warning("Regenerate: pending variants not consumed by flush, discarding")
                slot._pending_variants = []

        task.add_done_callback(_clear_pending_on_done)
    state.push_slots_update()
    return web.json_response({"ok": True})


async def api_chat_slot_switch_variant(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/switch-variant — switch which regenerated variant is active."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    try:
        idx = int(body.get("index"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid index", "code": "index_invalid"}, status=400)

    async with slot._lock:
        if slot.running:
            return web.json_response(
                {"error": "slot is running", "code": "slot_running"}, status=409
            )

        target = None
        for m in reversed(slot.messages):
            if m.get("role") == "assistant" and m.get("variants"):
                target = m
                break
        if target is None:
            return web.json_response({"error": "no variants", "code": "no_variants"}, status=400)
        raw_target_variants = target.get("variants")
        variants: list[dict] = (
            list(raw_target_variants)  # type: ignore[arg-type]
            if isinstance(raw_target_variants, list)
            else []
        )
        if idx < 0 or idx >= len(variants):
            return web.json_response(
                {"error": "index out of range", "code": "index_out_of_range"}, status=400
            )

        chosen = variants[idx]
        if not isinstance(chosen, dict):
            return web.json_response(
                {"error": "corrupt variant entry", "code": "variant_corrupt"}, status=400
            )
        target_dict: dict = target
        target_dict["content"] = chosen.get("content", "")
        slot.invalidate_source_links()
        target_dict["ts"] = chosen.get("ts", target_dict.get("ts", ""))
        target_dict["variant_idx"] = idx
        slot._dirty = True
        slot._resumed_count = 0
        try:
            msgs_snapshot = list(slot.messages)
            await asyncio.to_thread(_save_slot_to_history, state, slot, msgs_snapshot)
        except Exception:
            logger.warning("switch-variant: failed to persist", exc_info=True)
        sel().log_api_access(
            caller="dashboard",
            operation="chat.switch_variant",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )
        _bc, _ = redact_exfiltration_urls(target_dict["content"])
        _bc, _ = redact_credentials(_bc)
        state.broadcast_ws(
            "chat_variant_switch",
            {"slot": slot.key, "index": idx, "content": _bc},
        )
        return web.json_response({"ok": True, "index": idx})


async def api_chat_slot_edit_resend(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/edit-resend — edit a user message and resend."""
    # Destructive: this truncates and PERSISTS history before the background
    # turn runs, so a failed turn cannot undo it. Unlike an ordinary send, the
    # readiness latch must be honored BEFORE the mutation.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # A valid-JSON but non-object body (array/scalar) has no .get(), so
    # body.get("index") would raise AttributeError -> 500. Reject it as a 400,
    # matching the guard in api_chat_slot_switch_variant above.
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    index = body.get("index")
    ts = body.get("ts")
    content = (body.get("content") or "").strip()
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "content_required"}, status=400
        )

    async with slot._lock:
        if slot.running:
            return web.json_response(
                {"error": "slot is running", "code": "slot_running"}, status=409
            )

        msgs = slot.messages

        if ts:
            index = next(
                (i for i, m in enumerate(msgs) if m.get("ts") == ts and m.get("role") == "user"),
                -1,
            )
            if not isinstance(index, int) or index < 0:
                return web.json_response(
                    {"error": "user message not found for ts", "code": "user_message_not_found"},
                    status=400,
                )
        elif isinstance(index, int) and 0 <= index < len(msgs):
            if msgs[index].get("role") != "user":
                return web.json_response(
                    {"error": "index is not a user message", "code": "index_not_user_message"},
                    status=400,
                )
        else:
            return web.json_response(
                {"error": "index or ts required", "code": "index_or_ts_required"}, status=400
            )

        # Capture routing/orphan state BEFORE any live mutation, exactly like
        # rewind: the truncation is a real conversation boundary, so the native
        # ACP conversation must be discarded and the truncated history durably
        # saved before the live slot adopts the edit or any replacement turn is
        # dispatched. session_key addresses the session whose native resume
        # identity is cleared; expected_history_key is the transcript this edit
        # was authorized against.
        session_key = effective_session_key(slot)
        expected_history_key = slot_history_key(slot)

        # Prepare the truncated+edited window on a COPY. The dirty-slot flush
        # can run while either durable boundary below is pending, so exposing a
        # truncated live window here could make a rejected edit permanent.
        _bc, _ = redact_exfiltration_urls(content)
        _bc, _ = redact_credentials(_bc)
        prospective_slot = copy.copy(slot)
        prospective_slot.messages = list(slot.messages[:index])
        prospective_slot._dirty = True
        prospective_slot._resumed_count = 0
        # The window was truncated → the next save MUST take the archive-safe
        # rewrite path. Set it on the COPY only; the live slot keeps its current
        # flag until the commit below adopts the prepared state.
        prospective_slot._pending_rewrite = True
        prospective_slot.append("user", _bc, "msg msg-u")
        msgs_snapshot = list(prospective_slot.messages)

        # Durably clear the native conversation BEFORE the history rewrite,
        # mirroring rewind. A failure here leaves the original branch intact and
        # dispatches no replacement turn.
        if state.sessions is not None:
            try:
                # ``skip_if_busy``: an inbound channel turn holds the session
                # semaphore while ``slot.running`` reads False, so the idle
                # check above cannot see it -- an unconditional discard would
                # tear down its provider mid-reply.
                discarded = await state.sessions.discard_conversation(
                    session_key, skip_if_busy=True
                )
            except Exception:
                logger.warning(
                    "edit-resend: failed to discard ACP conversation for %s",
                    session_key,
                    exc_info=True,
                )
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not prepare edited conversation; retry the edit",
                        "code": "edit_resend_prepare_failed",
                    },
                    status=503,
                )
            if not discarded:
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "the session is busy with another reply; retry the edit",
                        "code": "edit_resend_session_busy",
                    },
                    status=409,
                )
            try:
                # Force the durability point endpoint-side: the sid clear lands
                # in the session map's debounced writer, and a gateway exit
                # before that write would resurrect the discarded conversation
                # on restart.
                await state.sessions.aflush()
            except Exception:
                logger.warning(
                    "edit-resend: failed to flush the cleared resume sid for %s",
                    session_key,
                    exc_info=True,
                )
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not prepare edited conversation; retry the edit",
                        "code": "edit_resend_prepare_failed",
                    },
                    status=503,
                )

        # Persist the truncated+edited history via the explicit-snapshot
        # rewrite path. Nothing was mutated on the live slot yet, so a failure
        # (exception OR a save refused by its own guards) means nothing
        # persisted: no live mutation and no dispatch. Unlike rewind, edit-resend
        # has no queue-reservation machinery holding a competing send off the
        # slot, so there is no committed-rewrite-without-dispatch window to
        # shield against here -- a bare await is correct and kept simple.
        try:
            saved = await asyncio.to_thread(
                _save_slot_to_history,
                state,
                slot,
                msgs_snapshot,
                expected_history_key=expected_history_key,
            )
        except Exception:
            logger.warning("edit-resend: failed to persist", exc_info=True)
            state.push_slots_update()
            return web.json_response(
                {
                    "error": "could not save edited conversation; retry the edit",
                    "code": "edit_resend_save_failed",
                },
                status=503,
            )
        if not saved:
            # The save's own guards refused the write (the session was
            # permanently deleted, or the slot was rebound to another
            # transcript, while the write awaited its lock). Nothing was
            # persisted, so dispatching a turn now would run from state that
            # exists only in memory.
            logger.warning(
                "edit-resend: history save refused for %s (concurrent delete or rebind)",
                slot.key,
            )
            state.push_slots_update()
            return web.json_response(
                {
                    "error": "could not save edited conversation; retry the edit",
                    "code": "edit_resend_save_failed",
                },
                status=503,
            )

        # Both boundaries committed. Adopt the prepared state on the LIVE slot
        # and dispatch the replacement turn. No await between the save above and
        # these mutations, so they are atomic on the event loop.
        slot.messages = prospective_slot.messages
        slot.invalidate_source_links()
        slot._dirty = True
        slot._resumed_count = 0

        sel().log_api_access(
            caller="dashboard",
            operation="chat.edit_resend",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )

        task = asyncio.create_task(
            _run_chat(
                state,
                slot,
                _bc,
                _directive_user_origin=not bool(request.get("app", "")),
            )
        )
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error(
                    "edit-resend _run_chat failed for %s", slot.key, exc_info=t.exception()
                )

        task.add_done_callback(_on_done)

    state.push_slots_update()
    return web.json_response({"ok": True})
