"""Regenerate, variant switch, and edit-resend endpoints."""

from __future__ import annotations

import asyncio
import copy
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_runner import _run_chat, _start_next_queued_turn
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    slot_history_key,
)
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
    """POST /api/chat/slots/{slot}/edit-resend — edit a user message and resend.

    Shares rewind's context-boundary contract: the truncated history is
    prepared prospectively, the native conversation identity is durably
    cleared BEFORE the history rewrite, either boundary failing is a
    retryable 503, and the live slot is mutated only after both boundaries
    commit. Without the native clear, the replaced suffix could re-enter the
    replacement turn via native session resume; without the 503, a failed
    save would silently dispatch a turn from state that was never persisted.
    """
    # Destructive: this truncates and PERSISTS history before the background
    # turn runs, so a failed turn cannot undo it. Unlike an ordinary send, the
    # readiness latch must be honored BEFORE the mutation.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check — mirror rewind's contract: this endpoint now clears
    # the slot's native conversation identity, so an app must not reach a slot
    # it does not own.
    if request_app:
        if not slot._app or slot._app != request_app:
            sel().log_api_access(
                caller=request_app,
                operation="chat.edit_resend",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app cannot edit-resend unscoped or unowned slot",
            )
            # 404 (not 403): indistinguishable from a missing slot —
            # anti-enumeration (CWE-204); true reason logged via SEL above.
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
    raw_content = body.get("content")
    # An absent content stays "content is required" (the shape this endpoint
    # has always answered); a PRESENT non-string would otherwise reach
    # .strip() and 500.
    if raw_content is not None and not isinstance(raw_content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "content_invalid"}, status=400
        )
    content = (raw_content or "").strip()
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "content_required"}, status=400
        )
    if len(content) > 32_768:
        return web.json_response(
            {"error": "content too long (max 32768 chars)", "code": "content_too_long"}, status=400
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

        # Capture the session identity before any mutation.
        session_key = effective_session_key(slot)

        # An app may edit-resend only the slot's OWN dashboard session. An
        # app-owned slot can carry a channel link (``linked_session_key``),
        # and ``session_key`` then addresses a foreign channel conversation
        # -- clearing its native identity through this endpoint would tear
        # down a session the app does not own. Same 404-not-403 shape as the
        # ownership check above (anti-enumeration); SEL records the truth.
        if request_app and getattr(slot, "linked_session_key", ""):
            sel().log_api_access(
                caller=request_app,
                operation="chat.edit_resend",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app cannot edit-resend a channel-linked slot",
            )
            return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

        # The transcript this edit was authorized against. A concurrent
        # rebinding (a cron injection re-linking the slot) moves the slot to
        # another transcript while the boundaries below are pending; the
        # commit re-checks this key so the edit can never land on state it
        # never read.
        expected_history_key = slot_history_key(slot)

        # Prepare the prospective state on a copy. The dirty-slot flush can run
        # while either durable boundary below is pending, so exposing a truncated
        # live window here could make a rejected edit permanent.
        prospective_slot = copy.copy(slot)
        prospective_slot.messages = list(slot.messages[:index])
        # The commit never reads the copy's queue; break the copy.copy alias so
        # a future edit through the copy cannot silently reach the live queue.
        prospective_slot._queue = []
        prospective_slot._pending = list(slot._pending)
        prospective_slot._question_pending = dict(slot._question_pending)
        prospective_slot._on_question_retired = None
        prospective_slot.event = asyncio.Event()
        if prospective_slot._pending:
            prospective_slot.event.set()
        prospective_slot._dirty = True
        prospective_slot._resumed_count = 0
        prospective_slot._pending_rewrite = True

        # Unlike rewind, edit-resend keeps already-queued entries: they drain
        # onto the edited timeline through their normal trigger, as they always
        # have on this endpoint. The snapshot exists to tell them apart from an
        # entry DIVERTED to the queue by the reservation below, which has no
        # trigger of its own on the abort path.
        pre_await_queue_ids = {item["id"] for item in slot._queue}

        # Rows can land on the LIVE window while the boundaries below are
        # pending: the reservation diverts SENDS to the queue, but direct
        # append doors (a workflow result surfacing via append_and_surface, a
        # channel mirror row) do not consult the reservation. Those rows
        # belong to the NEW timeline and the commit must retain them. Count
        # them by the lifetime-counter delta, NOT by window length: at the
        # window cap ``append`` trims the FRONT to keep the length constant,
        # so a length-based tail slice reads empty exactly when the window is
        # busiest. Appends land only at the end and the trim eats only the
        # front, so the last ``delta`` rows are precisely the arrivals. No
        # await separates this capture from the reservation below, so the
        # baseline is exact.
        pre_await_total_messages = slot.total_messages

        # Build the user row through the slot's normal append path without
        # publishing it to the live slot before persistence succeeds.
        redacted_content, _ = redact_exfiltration_urls(content)
        redacted_content, _ = redact_credentials(redacted_content)
        prospective_slot.append("user", redacted_content, "msg msg-u")
        msgs_snapshot = list(prospective_slot.messages)
        retired_question_ids = [
            question_id
            for question_id in slot._question_pending
            if question_id not in prospective_slot._question_pending
        ]

        # Reserve the slot BEFORE the awaits below. ``slot.running`` derives
        # from ``slot.task``, and the send path is not serialized on
        # ``slot._lock``: without a live task, a send arriving while either
        # durable boundary is pending observes an idle slot, appends its row
        # to ``slot.messages`` and dispatches a competing turn -- which the
        # commit below would then erase. Publishing the dispatch task here
        # (no await between the idle check above and this assignment) makes
        # such a send take the queue path instead; the replacement turn's own
        # teardown drain delivers it. On abort the task starts the next queued
        # turn itself, so a diverted send is never stranded.
        dispatch_ready = asyncio.Event()
        dispatch_commit = False

        async def _edit_dispatch() -> None:
            await dispatch_ready.wait()
            if dispatch_commit:
                await _run_chat(
                    state,
                    slot,
                    redacted_content,
                    _directive_user_origin=not bool(request_app),
                )
                return
            # Edit rejected. A send diverted to the queue by this reservation
            # has no drain trigger of its own (no turn ran), so hand it to the
            # canonical successor dispatch, which re-validates holds before
            # starting anything. Entries that were already queued before the
            # edit keep waiting for their own trigger.
            if any(entry["id"] not in pre_await_queue_ids for entry in slot._queue):
                if await _start_next_queued_turn(state, slot):
                    return
            state.push_slots_update()

        task = asyncio.create_task(_edit_dispatch())
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error(
                    "edit-resend _run_chat failed for %s", slot.key, exc_info=t.exception()
                )

        task.add_done_callback(_on_done)

        # Durably clear the native resume sid before committing the edited
        # history. A failure leaves the original branch intact and dispatches
        # no replacement turn.
        try:
            if state.sessions is not None:
                try:
                    # ``skip_if_busy``: an inbound channel turn (a Slack reply
                    # on the linked session) holds the session semaphore while
                    # ``slot.running`` reads False, so the idle check above
                    # cannot see it -- an unconditional discard would tear
                    # down its provider mid-reply. The refusal is atomic with
                    # the busy probe inside the lifecycle service.
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
                            "code": "rewind_prepare_failed",
                        },
                        status=503,
                    )
                if not discarded:
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "the session is busy with another reply; retry the edit",
                            "code": "rewind_session_busy",
                        },
                        status=409,
                    )
                try:
                    # The sid clear lands in the session map's debounced
                    # writer; a gateway exit before that write would reload
                    # the old sid on restart and resurrect the discarded
                    # conversation. Force the durability point HERE,
                    # endpoint-side, so the shared discard keeps its existing
                    # semantics for its other callers (chat_runner, channel
                    # handlers), which tolerate the debounce.
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
                            "code": "rewind_prepare_failed",
                        },
                        status=503,
                    )

            def _commit_live_state() -> None:
                """Adopt the prepared state on the live slot (synchronous).

                Shared by the normal success path and the cancellation path
                below: once the destructive rewrite has landed on disk, this
                is the only thing that keeps live state matching it. No await
                inside, so it is atomic on the event loop.
                """
                # Adopt the prepared branch AND retain rows that landed on the
                # live window while the boundaries were pending (a workflow
                # result, a channel mirror row): the old inline-truncate
                # implementation kept such arrivals, and erasing them here
                # would be silent data loss. The arrival count is the lifetime
                # counter's delta (window length is wrong at the cap, where
                # the front-trim keeps length constant); appends land only at
                # the end, so the tail slice is exactly the arrivals. The
                # prospective copy froze its own counter at snapshot time, so
                # live bumps are all arrivals. In-place edits to
                # retained-prefix rows survive on their own -- the prospective
                # list shares those dicts. Arrivals persist through the normal
                # flush (``_dirty`` below); the rewrite that just landed
                # carried only the snapshot, which is correct -- the arrivals
                # are appends on top of it, not part of the replaced suffix.
                arrivals_count = min(
                    slot.total_messages - pre_await_total_messages, len(slot.messages)
                )
                arrivals = slot.messages[-arrivals_count:] if arrivals_count > 0 else []
                slot.messages = prospective_slot.messages + arrivals
                # The edited user row was appended on the prospective copy, so
                # its lifetime-counter bump landed on the copy. Count it on the
                # live slot at the moment the row becomes live: consumers use
                # this counter as a "did new rows land meanwhile" witness
                # (chat_slack backfill), and the pre-port implementation
                # counted this row by appending live. An increment (rather
                # than adopting the copy's value) keeps the counter monotone
                # when a diverted send bumped it during the awaited
                # boundaries.
                slot.total_messages += 1
                slot._pending = prospective_slot._pending
                slot._question_pending = prospective_slot._question_pending
                slot.invalidate_source_links()
                slot._dirty = True
                slot._resumed_count = 0
                # Deliberately NOT copied from ``prospective_slot``: the
                # persistence witnesses (``_pending_rewrite``, ``_disk_*``,
                # ``_frozen_prefix_cache``). The save above ran on the LIVE
                # slot and stamped them with the post-rewrite truth
                # (``_pending_rewrite`` cleared, disk window/meta/mtime cache
                # matching the truncated file); the prospective copies are the
                # PRE-save values. Restoring those would re-arm
                # ``_pending_rewrite`` -- making the next flush repeat the
                # destructive rewrite and discard any cross-process append
                # (workflow/cron) that landed in between -- and would move the
                # monotone ``_disk_tail_ts`` floor backwards.
                if slot._pending:
                    slot.event.set()
                else:
                    slot.event.clear()
                if retired_question_ids and callable(slot._on_question_retired):
                    try:
                        slot._on_question_retired(slot.key, retired_question_ids)  # type: ignore[operator]
                    except Exception:
                        logger.debug(
                            "edit-resend: question-retirement announcement failed for slot %s",
                            slot.key,
                            exc_info=True,
                        )
                sel().log_api_access(
                    caller=request_app or "dashboard",
                    operation="chat.edit_resend",
                    outcome="allowed",
                    source="dashboard",
                    resources=f"slot={slot.key},at_index={index}",
                )

            # The worker thread cannot be interrupted: once the rewrite starts
            # it WILL finish, whether or not this handler is still alive. A
            # client disconnect cancels the handler task, and a bare await
            # here would then abandon a completed destructive rewrite --
            # persisted history rewound, live state stale, edited prompt never
            # dispatched. Shield the save; on cancellation, wait for the
            # worker's real outcome and complete the matching commit (and let
            # the reserved dispatch task run the edited prompt) before
            # propagating the cancellation.
            save_task = asyncio.ensure_future(
                asyncio.to_thread(
                    _save_slot_to_history,
                    state,
                    slot,
                    msgs_snapshot,
                    expected_history_key=expected_history_key,
                )
            )
            try:
                saved = await asyncio.shield(save_task)
            except asyncio.CancelledError:
                landed = False
                try:
                    landed = bool(await save_task)
                except Exception:
                    landed = False
                if landed and slot_history_key(slot) == expected_history_key:
                    _commit_live_state()
                    dispatch_commit = True
                    logger.info(
                        "edit-resend: request cancelled after the rewrite landed for %s; "
                        "committed live state and dispatching the edited prompt",
                        slot.key,
                    )
                raise
            except Exception:
                logger.warning("edit-resend: failed to persist truncated history", exc_info=True)
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not save edited conversation; retry the edit",
                        "code": "rewind_save_failed",
                    },
                    status=503,
                )
            if not saved:
                # The save's own guards refused the write (the session was
                # permanently deleted, or the slot was rebound to another
                # transcript, while the write awaited its lock). Nothing was
                # persisted, so reporting success here would dispatch a turn
                # from state that exists only in memory.
                logger.warning(
                    "edit-resend: history save refused for %s (concurrent delete or rebind)",
                    slot.key,
                )
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not save edited conversation; retry the edit",
                        "code": "rewind_save_failed",
                    },
                    status=503,
                )

            # Both irreversible boundaries succeeded. Before adopting the
            # prepared state, confirm the slot still routes to the transcript
            # this edit was authorized against: a concurrent rebinding (a
            # cron injection re-linking the slot mid-persistence) hydrates the
            # slot with ANOTHER conversation's state, and a late commit here
            # would silently replace it. The prospective copy froze the old
            # routing, so the save-side ``expected_history_key`` guard cannot
            # see the live slot move -- this loop-side check is the one that
            # can. No await between this check and the mutations below.
            if slot_history_key(slot) != expected_history_key:
                logger.warning(
                    "edit-resend: slot %s was rebound to another transcript during "
                    "persistence; refusing the commit",
                    slot.key,
                )
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "the conversation changed while saving; retry the edit",
                        "code": "rewind_slot_rebound",
                    },
                    status=503,
                )

            # The prepared state is now the live slot state. Keep the
            # source-link invalidation with this commit rather than leaking it
            # during either await above.
            _commit_live_state()
            dispatch_commit = True
        finally:
            # Wake the reserved dispatch task on every exit: it runs the
            # replacement turn on commit and the queue handoff on abort.
            dispatch_ready.set()

    state.push_slots_update()
    return web.json_response({"ok": True})
