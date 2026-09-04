"""``GET /api/agents`` ships an explicit allowlist, never the whole record.

The endpoint used to build each row with ``{**dataclasses.asdict(agent_cfg)}``,
which made its response contract "every field ``KiroCrewAgentConfig`` has now,
plus every field anyone adds later", automatically — a field added by someone
who never looked at this endpoint shipped to the browser by omission. #8454
converted both row sources to an explicit allowlist, mirroring the rule
``handlers/members.py`` already documents for ``GET /api/members``.

These tests are the half that keeps it converted. The key set is pinned as a
literal, and a separate ratchet compares that literal against the live record
so a field added to ``KiroCrewAgentConfig`` fails here rather than silently
appearing in the response — which is the whole point of the change: the default
becomes "nothing unless someone adds it".
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import types
import unittest.mock
from pathlib import Path
from typing import cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers.agents import _EDITOR_WRITTEN_BACK, _agent_roster_row

# The EXACT key set every row carries, from BOTH sources (the ``cfg.agents``
# rows and the project-scope rows). ``name`` and ``scope`` are handler-added;
# the rest are allowlisted record fields. Changing this set is a
# network-boundary contract change: check the frontend consumers first
# (``website/src/components/AgentSelector.tsx`` declares the ``KiroCrewAgent``
# interface the dashboard reads).
ROSTER_ROW_KEYS = frozenset(
    {
        "name",
        "scope",
        "kiro_agent",
        "workspace",
        "memory_store",
        "model",
        "reasoning_effort",
        "description",
        "triggers",
        "source",
        "session_color",
    }
)

# Record fields deliberately withheld, each verified to have no consumer in
# ``website/src``: the two watchdog windows are backend scheduling knobs the
# roster does not render, and ``telegram_account`` is deprecated and inert.
WITHHELD_RECORD_FIELDS = frozenset(
    {
        "watchdog_tool_stall_suspect_secs",
        "watchdog_tool_stall_hard_cap_secs",
        "telegram_account",
    }
)


def _make_app() -> web.Application:
    """Minimal aiohttp app with just the roster endpoint."""
    from kiro_crew.dashboard.handlers import api_kirocrew_agents

    app = web.Application()
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


def _seed_config_with_every_field_set() -> dict:
    """A config whose agent sets EVERY record field, withheld ones included.

    A withheld field left at its default is indistinguishable from a withheld
    field that is absent, so the fixture gives each one a distinctive value: an
    assertion on its absence then actually proves the allowlist rather than the
    default happening to be falsy.
    """
    return {
        "agents": {
            "roster-probe": {
                "kiro_agent": "kirocrew",
                "workspace": "probe-ws",
                "memory_store": "probe-ms",
                "model": "claude-opus-5",
                "reasoning_effort": "high",
                "description": "probe description",
                "triggers": "probe triggers",
                "source": "kirocrew",
                "session_color": "#abcdef",
                # Withheld — must NOT appear in the response.
                "watchdog_tool_stall_suspect_secs": 111.0,
                "watchdog_tool_stall_hard_cap_secs": 222.0,
                "telegram_account": "probe-telegram-binding",
            },
        },
        "default_agent": "roster-probe",
        "workspaces": {"default": {"dir": "workspace"}, "probe-ws": {"dir": "workspace"}},
    }


class TestRosterRowKeySet:
    """The response's exact key set, measured at the endpoint."""

    @pytest.mark.asyncio
    async def test_global_row_ships_exactly_the_allowlist(self) -> None:
        """A ``cfg.agents`` row carries the allowlist and nothing else."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config_with_every_field_set(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                async with TestClient(TestServer(_make_app())) as client:
                    resp = await client.get("/api/agents")
                    assert resp.status == 200
                    row = {a["name"]: a for a in (await resp.json())["agents"]}["roster-probe"]

            assert set(row) == ROSTER_ROW_KEYS
            # The allowlisted values still arrive — an allowlist that shipped
            # the right KEYS with empty values would pass a key-set assertion
            # while breaking every consumer.
            assert row["scope"] == "global"
            assert row["workspace"] == "probe-ws"
            assert row["memory_store"] == "probe-ms"
            assert row["model"] == "claude-opus-5"
            assert row["reasoning_effort"] == "high"
            assert row["description"] == "probe description"
            assert row["triggers"] == "probe triggers"
            assert row["session_color"] == "#abcdef"
            # And the withheld fields are gone even though the config set them
            # to distinctive non-default values.
            assert not (set(row) & WITHHELD_RECORD_FIELDS)
            assert "probe-telegram-binding" not in json.dumps(row)
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_project_row_ships_the_same_key_set(self, monkeypatch) -> None:
        """A project-scope row carries the SAME keys as a global row.

        The two sources were separate spreads before #8454, so they could drift
        into different key sets; pinning both is what makes one allowlist the
        answer for the whole response.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: "/probe/project",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.project_agent_names",
            lambda project_dir, **kw: frozenset({"project-only-agent"}),
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(_seed_config_with_every_field_set(), f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = _make_app()
                # Truthy state with no conversation log: the handler then takes
                # the project-scan branch and skips the usage re-sort.
                app["state"] = types.SimpleNamespace(conversation_log=None)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents")
                    assert resp.status == 200
                    rows = {a["name"]: a for a in (await resp.json())["agents"]}

            assert "project-only-agent" in rows, "project scan produced no row to check"
            project_row = rows["project-only-agent"]
            assert set(project_row) == ROSTER_ROW_KEYS
            assert set(project_row) == set(rows["roster-probe"])
            assert project_row["scope"] == "project"
            assert not (set(project_row) & WITHHELD_RECORD_FIELDS)
        finally:
            tmp.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_app_token_caller_gets_the_same_keys_with_scrubbed_values(self) -> None:
        """End to end: the caller class is resolved from the request, not passed in.

        The row-level tests cover ``redact=`` directly; this one covers the
        WIRING -- that the handler reads the caller class off the request at all.
        A middleware sets ``request["app"]``, which is what ``token_auth`` does
        for a verified app token and what ``members.py::_deny_app_caller`` reads.
        """
        probe = "AKIAIOSFODNN7EXAMPLE"
        seed = _seed_config_with_every_field_set()
        seed["agents"]["roster-probe"]["description"] = f"see {probe}"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(seed, f)
            tmp = Path(f.name)
        try:

            @web.middleware
            async def _as_app(request: web.Request, handler):  # type: ignore[no-untyped-def]
                request["app"] = "probe-app"
                return await handler(request)

            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                app = web.Application(middlewares=[_as_app])
                from kiro_crew.dashboard.handlers import api_kirocrew_agents

                app.router.add_get("/api/agents", api_kirocrew_agents)
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get("/api/agents")
                    assert resp.status == 200
                    body = await resp.json()
                    row = {a["name"]: a for a in body["agents"]}["roster-probe"]

            assert set(row) == ROSTER_ROW_KEYS, "key set must not depend on caller class"
            assert probe not in json.dumps(row), "app token received an unscrubbed value"
        finally:
            tmp.unlink(missing_ok=True)


class TestRosterRowIsAnAllowlistNotASpread:
    """The property that survives the record growing."""

    def test_record_field_added_later_is_not_shipped_by_omission(self) -> None:
        """Every ``KiroCrewAgentConfig`` field is either allowlisted or withheld.

        This is the ratchet. It fails when a field is ADDED to the record, and
        that failure is the feature: the author has to decide whether the
        browser should see it, instead of a spread deciding for them.
        """
        record_fields = {f.name for f in dataclasses.fields(KiroCrewAgentConfig)}
        unclassified = record_fields - ROSTER_ROW_KEYS - WITHHELD_RECORD_FIELDS
        assert not unclassified, (
            f"KiroCrewAgentConfig grew {sorted(unclassified)}. GET /api/agents is an "
            "explicit allowlist (#8454), so decide deliberately: add the field to "
            "_agent_roster_row AND ROSTER_ROW_KEYS if the dashboard needs it, or to "
            "WITHHELD_RECORD_FIELDS if it must not leave the process."
        )
        # The withheld set must name real fields — a typo there would silently
        # stop classifying anything and let the next added field through.
        assert WITHHELD_RECORD_FIELDS <= record_fields
        # And the allowlist must not claim a record field that no longer exists.
        assert ROSTER_ROW_KEYS - {"name", "scope"} <= record_fields

    def test_an_attribute_the_allowlist_does_not_name_is_dropped(self) -> None:
        """Behavioral proof, not just a literal comparison.

        A record carrying an extra attribute — the shape of a field added
        later — serializes to the same key set. Under the old spread this row
        would have carried ``secret_future_field``.
        """
        grown = dataclasses.make_dataclass(
            "GrownAgentConfig",
            [
                (f.name, f.type, dataclasses.field(default=f.default))
                for f in dataclasses.fields(KiroCrewAgentConfig)
            ]
            + [("secret_future_field", str, dataclasses.field(default="must-not-ship"))],
        )
        row = _agent_roster_row("grown", "global", cast(KiroCrewAgentConfig, grown()), redact=False)
        assert set(row) == ROSTER_ROW_KEYS
        assert "secret_future_field" not in row
        assert "must-not-ship" not in json.dumps(row)


class TestRosterValuePolicy:
    """A value is scrubbed EXACTLY where no write path accepts the result."""

    PROBE = "AKIAIOSFODNN7EXAMPLE"

    # Fields the loader does NOT coerce: a hand-edited config.json can put a
    # non-string under any of these and it reaches the record verbatim. Measured
    # against the live loader, not read off the dataclass (which declares all of
    # them `str`).
    UNCOERCED_BY_LOADER = ("kiro_agent", "workspace", "memory_store", "description", "source")

    # Record fields NOT in AgentUpdatePayload, so no write path accepts them.
    NOT_WRITTEN_BACK = ("description", "source")

    def _row(self, *, redact: bool, **kw: object) -> dict[str, object]:
        return _agent_roster_row("probe", "global", KiroCrewAgentConfig(**kw), redact=redact)

    def test_written_back_set_matches_the_frontend_payload(self) -> None:
        """The constant is a claim about `saveEdit`; pin it so it cannot drift.

        `website/src/pages/KiroCrewAgentsPage.tsx`'s `AgentUpdatePayload` names
        exactly these seven, and `saveEdit` sends all of them unconditionally.
        If that payload changes, this set must change with it -- otherwise a
        newly-editable field keeps being scrubbed and its stored value is
        destroyed on the next save.
        """
        assert _EDITOR_WRITTEN_BACK == {
            "kiro_agent",
            "workspace",
            "memory_store",
            "triggers",
            "model",
            "reasoning_effort",
            "session_color",
        }
        record_fields = {f.name for f in dataclasses.fields(KiroCrewAgentConfig)}
        assert _EDITOR_WRITTEN_BACK <= record_fields
        # Everything shipped that is neither handler-authored nor written back is
        # exactly the set safe to scrub for the owner.
        shipped_record = ROSTER_ROW_KEYS - {"name", "scope"}
        assert shipped_record - _EDITOR_WRITTEN_BACK == set(self.NOT_WRITTEN_BACK)

    def test_owner_gets_written_back_fields_verbatim(self) -> None:
        """A field the sheet writes back must survive byte-identical.

        `saveEdit` returns these unconditionally, so a transform is persisted
        over the operator's original on the next save of an unrelated field --
        the defect an earlier revision of this PR shipped. The probe is
        credential-shaped because that is the value a scrub would alter.
        """
        cfg = KiroCrewAgentConfig(
            kiro_agent=self.PROBE,
            workspace=f"ws-{self.PROBE}",
            memory_store=f"ms-{self.PROBE}",
            triggers=f"use when {self.PROBE}",
            model=self.PROBE,
            reasoning_effort=self.PROBE,
            session_color=self.PROBE,
        )
        row = _agent_roster_row("probe", "global", cfg, redact=False)
        for field in _EDITOR_WRITTEN_BACK:
            assert row[field] == getattr(cfg, field), f"{field} was altered on the owner path"

    @pytest.mark.parametrize("field", NOT_WRITTEN_BACK)
    def test_owner_gets_non_editable_fields_scrubbed(self, field: str) -> None:
        """`description` / `source` ARE scrubbed for the owner: nothing writes them.

        `description` is package-controlled, not operator-typed --
        `_do_agents_sync` copies it straight off a discovered agent spec -- and it
        is absent from `AgentUpdatePayload`, so scrubbing costs no fidelity.
        """
        row = self._row(redact=False, **{field: f"see {self.PROBE}"})
        assert self.PROBE not in json.dumps(row[field])

    def test_owner_keeps_name_addressable(self) -> None:
        """`name` addresses the owner-gated CRUD routes, so it stays verbatim."""
        row = _agent_roster_row(
            f"crew-{self.PROBE}", "project", KiroCrewAgentConfig(), redact=False
        )
        assert row["name"] == f"crew-{self.PROBE}"
        assert row["scope"] == "project"

    @pytest.mark.parametrize("field", UNCOERCED_BY_LOADER)
    def test_owner_does_not_coerce_a_written_back_non_string(self, field: str) -> None:
        """A malformed written-back value is reported as stored, not rewritten to "".

        Coercing is the same data-loss shape as scrubbing: the sheet would PUT
        `""` over whatever the config holds. Rejecting a malformed stored value
        is the config layer's call, not a serializer's.
        """
        cfg = KiroCrewAgentConfig()
        object.__setattr__(cfg, field, {"nested": "object"})
        row = _agent_roster_row("probe", "global", cfg, redact=False)
        if field in _EDITOR_WRITTEN_BACK:
            assert row[field] == {"nested": "object"}
        else:
            assert row[field] == ""

    def test_app_token_gets_every_value_scrubbed(self) -> None:
        """Nothing is exempt for an app token, because nothing is reachable.

        It cannot write back (every mutating agent route is `_require_owner`) and
        cannot read the same records from `/api/config/kirocrew` (app-token scope
        is deny-by-default per path). So this row is its only door.
        """
        cfg = KiroCrewAgentConfig(
            kiro_agent=self.PROBE,
            workspace=f"ws-{self.PROBE}",
            memory_store=f"ms-{self.PROBE}",
            triggers=f"use when {self.PROBE}",
            model=self.PROBE,
            reasoning_effort=self.PROBE,
            session_color=self.PROBE,
            description=f"see {self.PROBE}",
            source=self.PROBE,
        )
        row = _agent_roster_row(f"crew-{self.PROBE}", "global", cfg, redact=True)
        assert self.PROBE not in json.dumps(row)

    def test_app_token_also_scrubs_name(self) -> None:
        """`name` is agent-writable text and an app cannot address anything with it.

        Crew creation enforces only that a name is non-empty (the shared
        `_AGENT_NAME_RE` grammar guards `kiro_agent`, not `name`), so a
        credential-shaped name is reachable. The owner's addressability argument
        does not transfer: the `/api/agents/{name}` routes are owner-gated.
        """
        row = _agent_roster_row(f"crew-{self.PROBE}", "global", KiroCrewAgentConfig(), redact=True)
        assert self.PROBE not in json.dumps(row["name"])
        # Benign names are untouched, so ordinary rosters are unaffected.
        plain = _agent_roster_row("kirocrew", "global", KiroCrewAgentConfig(), redact=True)
        assert plain["name"] == "kirocrew"

    @pytest.mark.parametrize("field", UNCOERCED_BY_LOADER)
    def test_app_token_flattens_every_non_string(self, field: str) -> None:
        """No nested structure reaches an app token, which has no write-back path."""
        cfg = KiroCrewAgentConfig()
        object.__setattr__(cfg, field, {"nested": "object"})
        row = _agent_roster_row("probe", "global", cfg, redact=True)
        assert row[field] == ""
        assert "nested" not in json.dumps(row)

    def test_benign_content_is_never_altered_on_either_path(self) -> None:
        """The scrub must not eat legitimate text, or it is a rendering bug."""
        cfg = KiroCrewAgentConfig(description="a plain crew", triggers="use for triage")
        for redact in (False, True):
            row = _agent_roster_row("kirocrew", "global", cfg, redact=redact)
            assert row["description"] == "a plain crew"
            assert row["triggers"] == "use for triage"
            assert row["name"] == "kirocrew"

    def test_both_caller_classes_ship_the_same_key_set(self) -> None:
        """Only values differ. A caller-dependent KEY set would be a second contract."""
        cfg = KiroCrewAgentConfig(description="d", triggers="t")
        owner = _agent_roster_row("probe", "global", cfg, redact=False)
        app = _agent_roster_row("probe", "global", cfg, redact=True)
        assert set(owner) == set(app) == ROSTER_ROW_KEYS
