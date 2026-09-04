"""The restricted-session ceiling on outbound file uploads.

A dashboard slot marked incognito or temporary is denied every artifact write, on
the reasoning that a session the user expected to leave no trace must not persist
its output. Uploading a local file into a chat channel is the same disclosure
with a different destination — and on a channel where the conversation is a
shared thread, it is readable by everyone who can view it.

The decision is channel-neutral: it reads a session key, a dashboard slot, and a
transcript header. Only the audit label differs per channel, so that is the one
parameter. Discord and Telegram both route here rather than each carrying a copy
of the three-state ladder and its fail-closed reasoning.

Dependency direction stays one-way, and that is why the persisted-transcript
probe is a parameter rather than an import: reading it means calling
``dashboard.handlers._shared._probe_persisted_session``, and ``messaging`` may
not import ``dashboard`` — not even function-locally, since a guarded import is
still the same edge and still fails at runtime rather than at import time. Each
dispatcher lives in a package that MAY import ``dashboard``, so it passes the
probe in. Its first-party dependencies stay ``history``, ``sel`` and its own
sibling ``privacy_mode`` — all of which sit at or below this layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from kiro_crew.history import is_incognito_transcript
from kiro_crew.messaging import privacy_mode
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Namespace prefix of a key that can carry a dashboard SLOT. A key in any other
#: namespace is a channel-native conversation, which has no slot but can still be
#: restricted by the channel's own privacy mode.
_DASHBOARD_PREFIX = "dashboard:"

#: ``slot_name -> (file_exists, memory_mode_or_None)``. Blocking filesystem I/O;
#: this module hands it to a worker thread. ``None`` for the mode means "could
#: not tell", which denies.
PersistedProbe = Callable[[str], "tuple[bool, str | None]"]


def _persisted_mode_is_restricted(
    session_key: str, probe: PersistedProbe, unknown_denies: bool = True
) -> bool:
    """Whether ``dashboard:<slot>``'s PERSISTED transcript says it is restricted.

    Blocking (it reads the transcript's metadata line), so callers run it off the
    event loop. *probe* must be ``_probe_persisted_session``, NOT
    ``_persisted_session_memory_mode``: the namespace is stripped off the key
    before it gets here, so one stem can match several transcripts (a legacy bare
    ``<name>.jsonl`` and an archived ``dashboard_<name>.jsonl``), and the
    memory-mode helper answers from the FIRST candidate — letting a persistent
    file answer for an incognito session. The probe refuses to guess and reports
    unknown, as does a header no normal session wrote.

    *unknown_denies* decides what an unreadable mode means, because the two
    ceilings that ask are not symmetric. For an UPLOAD (the default) unknown
    denies: shipping bytes into a DM or a supergroup-readable Topic cannot be taken
    back, and refusing one file costs nothing recoverable. For durable HISTORY it
    does not, because the cost lands the other way — a resumed conversation whose
    tab is simply closed, or whose transcript predates the marker, reads unknown
    too, and denying there silently stops recording a conversation the user never
    asked to keep private. Existence is deliberately not consulted either way: it
    distinguishes none of those cases.

    A probe that raises denies regardless, so a caller cannot open the gate by
    handing in something broken.
    """
    slot_name = session_key.split(":", 1)[-1]
    try:
        _exists, mode = probe(slot_name)
    except Exception:
        logger.warning(
            "could not resolve the persisted memory mode for %s; denying",
            session_key,
            exc_info=True,
        )
        return True
    if mode is None:
        return unknown_denies
    return is_incognito_transcript(mode)


def live_dashboard_slot(dashboard_state: Any, session_key: str) -> Any | None:
    """The OPEN dashboard slot for *session_key*, or ``None``.

    Only meaningful for a resumed ``dashboard:`` key; anything else has no slot.
    """
    if not session_key.startswith(_DASHBOARD_PREFIX):
        return None
    getter = getattr(dashboard_state, "get_slot", None) if dashboard_state is not None else None
    if not callable(getter):
        return None
    try:
        return getter(session_key[len(_DASHBOARD_PREFIX) :])
    except Exception:
        logger.debug("live dashboard slot lookup failed for %s", session_key, exc_info=True)
        return None


async def session_is_restricted(
    dashboard_state: Any,
    session_key: str,
    *,
    persisted_probe: PersistedProbe,
    unknown_denies: bool = True,
) -> bool:
    """True when *session_key* is an incognito or temporary conversation.

    The decision only, with no audit and no per-caller wording, because more than
    one ceiling asks it: file uploads ask before shipping local bytes
    (:func:`uploads_restricted`), and a channel that can RESUME a dashboard
    session asks before appending that turn to durable history. Both must read the
    same answer — a conversation that refuses to write a transcript but ships the
    file, or refuses the file but writes the transcript, keeps neither promise.

    Three inputs, in the order their evidence is strongest:

    * A key that is not ``dashboard:`` is a channel-native conversation. It never
      had a dashboard slot, so the slot rungs below cannot answer for it — but it
      CAN be restricted by the channel's own ``/temporary`` or ``/incognito``, so
      that predicate answers instead. The DURABLE flags are restored first,
      because that predicate reads process-local trackers that only an INBOUND
      channel message populates: a turn no inbound message drove — a cron, a
      webhook-resumed session, a monitor/auto-nudge re-injection, an explicit
      ``file_send`` — would otherwise read empty trackers after a gateway restart
      and act on a mode the user's ``!incognito`` forbids. Same canonical restore,
      for the same reason, as
      ``dashboard.handlers._shared._is_restricted_session``.
    * A LIVE slot answers directly, off the same ``slot.is_restricted`` signal as
      the artifact gate, so the two cannot drift.
    * No live slot, but a ``dashboard:`` key still resolved. This is a real state,
      not a defensive branch: closing the tab pops the slot from the dashboard's
      ``_slots`` AND discards its key from ``_restricted_keys``, while a resume
      binding lives in the persisted session map, which only an explicit unlink
      clears. So the next message into that conversation resolves an incognito
      session whose in-memory restriction is gone. The transcript keeps its
      ``memory_mode`` marker, so resolve the PERSISTED mode instead. What an
      UNREADABLE mode means is the caller's call, via *unknown_denies* — the two
      ceilings are not symmetric there, and ``_persisted_mode_is_restricted``
      carries the reasoning.

    ``privacy_mode.is_restricted`` is NOT a substitute for the two slot rungs: it
    reads a process-local tracker that a dashboard slot never populates, so it
    answers ``False`` for an incognito dashboard session and fails OPEN.

    *persisted_probe* is only consulted on the third rung, and is a parameter
    rather than an import so this module keeps its one-way dependency on
    ``dashboard`` (see the module docstring).
    """
    if not session_key.startswith(_DASHBOARD_PREFIX):
        # Restore the durable flags before reading the process-local trackers (see
        # the docstring). Idempotent, allocation-free for an unflagged key, and an
        # in-memory ``SessionMap`` read rather than disk, so it is safe to run on
        # the loop at every decision point. A state with no session manager is a
        # no-op inside ``hydrate``.
        privacy_mode.hydrate(getattr(dashboard_state, "sessions", None), session_key)
        # The channel's own mode, on the same predicate its transcript, memory and
        # title writes use, so one conversation cannot be private for three of them
        # and public for the fourth.
        return privacy_mode.is_restricted(session_key)
    slot = live_dashboard_slot(dashboard_state, session_key)
    if slot is not None:
        return bool(getattr(slot, "is_restricted", True))
    return await asyncio.to_thread(
        _persisted_mode_is_restricted, session_key, persisted_probe, unknown_denies
    )


async def uploads_restricted(
    dashboard_state: Any,
    session_key: str,
    *,
    channel_type: str,
    persisted_probe: PersistedProbe,
) -> bool:
    """True when *session_key* must not ship local file bytes to *channel_type*.

    The decision itself is :func:`session_is_restricted`, shared with the durable
    history gate so the two ceilings cannot drift: a conversation that refuses to
    write a transcript must not ship the file either. Returning a flat ``False``
    for a channel-native key was correct only while no channel-native conversation
    had a privacy mode, and it became a hole the moment one did. It is not a
    blanket fail-closed either — an unrestricted conversation is allowed, which is
    the common case, and a channel with no modes at all reads ``False`` exactly as
    before.

    This wrapper adds only the upload-specific denial audit, so the ceiling is
    observable, mirroring how each transport audits its authorization denials.
    """
    restricted = await session_is_restricted(
        dashboard_state, session_key, persisted_probe=persisted_probe
    )
    if restricted:
        sel().log_api_access(
            caller=session_key,
            operation=f"{channel_type}_dispatch.upload_files",
            outcome="denied",
            source=channel_type,
            error="restricted_session",
        )
    return restricted
