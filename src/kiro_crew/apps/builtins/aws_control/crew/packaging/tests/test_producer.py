"""Tests for the crew bundle producer.

Two things need proving: the four-entry bundle comes out in the right shape, and
the deny-by-default guards actually refuse. The guards are the part whose failure
ships a credential, so each one is MUTATION-tested: the guard's source line is
disabled in an exec-loaded copy of the module and the same scenario is shown to
leak, proving the guard is load-bearing rather than decorative.

The module is loaded by exec-ing its file under a throwaway name rather than
``import packaging`` -- the environment also carries the unrelated PyPA ``packaging``
distribution, and a top-level ``import packaging`` would collide. The CLI end-to-end
test runs ``python -m packaging.build`` in a subprocess whose cwd is the crew root,
where this directory's ``packaging`` shadows the site-packages one for that child
only. That cwd is the driver's contract, not a test convenience: see
``smc-deploy.sh``'s ``cd "$CREW_ROOT" && "$py" -m packaging.build``.

Run only this file:
    python -m pytest \
        src/kiro_crew/apps/builtins/aws_control/crew/packaging/tests/test_producer.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# The crew root: the directory `python -m packaging.build` must run in.
CREW_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = CREW_ROOT / "packaging" / "build.py"

# A synthetic AWS key shape -- not a real credential, built to match the pattern
# and nothing else, so the scanner has something to fire on.
FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"[4:] + "ABCD"

_variant_counter = 0


def load_build(mutate: tuple[str, str] | None = None) -> types.ModuleType:
    """Exec ``packaging/build.py`` into a throwaway module.

    ``mutate`` is an ``(old, new)`` substring pair applied to the source before
    exec, used to disable exactly one guard and observe the leak it prevents.
    """
    global _variant_counter
    _variant_counter += 1
    text = BUILD_PY.read_text(encoding="utf-8")
    if mutate is not None:
        old, new = mutate
        assert old in text, f"mutation anchor not found: {old!r}"
        text = text.replace(old, new, 1)
    mod = types.ModuleType(f"smc_build_v{_variant_counter}")
    mod.__file__ = str(BUILD_PY)
    # Register before exec: @dataclass resolves annotations via
    # sys.modules.get(cls.__module__), which is None for an unregistered module.
    sys.modules[mod.__name__] = mod
    # The exec IS the mechanism under test, and the input is not attacker-reachable:
    # `text` is this repository's own `packaging/build.py`, read from a path derived
    # from __file__, optionally with one substring swapped by a literal pair written
    # in this file. Nothing here reads a request, an environment variable or a
    # filesystem location a caller chooses. Importing the module normally cannot
    # replace its constant strings, and patching the functions afterwards would test
    # the patch rather than the guard, so a mutation test of a module-level guard has
    # to compile a variant of the source. The alternative is not a safer test, it is
    # no test: the guards this exercises are the ones that keep a private key out of
    # a published bundle.
    exec(  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
        compile(text, str(BUILD_PY), "exec"), mod.__dict__
    )
    return mod


# ---------------------------------------------------------------------------
# fixtures: a crew source (agents/<name>.json + skills/) the producer reads
# ---------------------------------------------------------------------------
def make_crew(
    root: Path,
    name: str = "frontdesk",
    *,
    prompt: str = "You are the front desk. Answer questions about hours and location.",
    tools: list | None = None,
    allowed_tools: list | None = None,
    mcp_servers: dict | None = None,
    skills: dict[str, dict[str, str]] | None = None,
) -> Path:
    """Write a crew home and return it. ``skills`` maps skill id -> {filename: text}."""
    spec: dict = {"name": name, "prompt": prompt}
    if tools is not None:
        spec["tools"] = tools
    if allowed_tools is not None:
        spec["allowedTools"] = allowed_tools
    if mcp_servers is not None:
        spec["mcpServers"] = mcp_servers
    agents = root / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    skills_root = root / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    for sid, files in (skills or {}).items():
        d = skills_root / sid
        d.mkdir(parents=True, exist_ok=True)
        for fname, text in files.items():
            (d / fname).write_text(text, encoding="utf-8")
    return root


def sign_plan(
    mod: types.ModuleType,
    # The crew source the exec-loaded module builds; `Any` because its class is
    # defined inside that throwaway module and has no name to annotate against.
    crew: Any,
    agent_spec: dict,
    out: Path,
    *,
    select: dict[str, set[str]] | None = None,
    reviewed_by: str = "someone",
    reviewed_at: str = "2026-09-03T00:00:00+00:00",
) -> Path:
    """Write a fresh plan, flip the chosen ids to include, sign it, return its path."""
    candidates = mod.enumerate_all(crew, agent_spec)
    plan_path = out / mod.PLAN_FILENAME
    mod.write_plan(plan_path, crew.name, candidates)
    doc = json.loads(plan_path.read_text())
    doc["reviewed_by"] = reviewed_by
    doc["reviewed_at"] = reviewed_at
    for kind, ids in (select or {}).items():
        for entry in doc.get(kind, []):
            if entry["id"] in ids:
                entry["include"] = True
    plan_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return plan_path


# ---------------------------------------------------------------------------
# shape: the four-entry layout
# ---------------------------------------------------------------------------
def test_empty_bundle_is_valid_and_well_shaped(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ\nhours"}})
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    report = mod.build_bundle(crew, spec, cands, None, out)  # no plan => deny-all

    assert (out / "manifest.json").is_file()
    assert (out / "agent.json").is_file()
    assert (out / "mcp.json").is_file()
    assert (out / "skills").is_dir()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["crew_name"] == "frontdesk"
    assert manifest["bundle_version"] == mod.BUNDLE_VERSION
    assert manifest["digest"].startswith("sha256:")
    assert report.skill_count == 0
    # the skill did not ship, and the owner can see why
    assert any(d["id"] == "faq" and "deny-by-default" in d["reason"] for d in report.denied)


def test_agent_name_forced_to_crew_name(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", name="frontdesk")
    # spec on disk claims a different name
    spec_path = src / "agents" / "frontdesk.json"
    doc = json.loads(spec_path.read_text())
    doc["name"] = "my-local-crew"
    spec_path.write_text(json.dumps(doc))
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)
    assert json.loads((out / "agent.json").read_text())["name"] == "frontdesk"


def test_digest_matches_source_algorithm(tmp_path):
    """Digest is sha256 over sorted [rel, sha256(bytes)] rows, manifest excluded,
    'sha256:'-prefixed -- the algorithm ported from crew_export/bundle.py."""
    mod = load_build()
    src = make_crew(tmp_path / "home")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)

    import hashlib

    rows = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.relative_to(out).as_posix() != "manifest.json":
            rows.append([p.relative_to(out).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    expected = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
    assert json.loads((out / "manifest.json").read_text())["digest"] == expected


# ---------------------------------------------------------------------------
# GUARD 1: deny-by-default -- a fresh (even signed) plan ships nothing
# ---------------------------------------------------------------------------
def test_signed_plan_selecting_nothing_ships_nothing(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    plan_path = sign_plan(mod, crew, spec, out, select=None)  # signed, nothing chosen
    plan = mod.merge_plans([plan_path], "frontdesk")
    mod.verify(plan, "frontdesk", cands)
    report = mod.build_bundle(crew, spec, cands, plan, out)
    assert report.skill_count == 0


def test_MUTATION_deny_by_default(tmp_path):
    """Disable the include filter in Plan.included; a signed-but-empty plan now
    leaks every skill. Mutation: drop the `if on` filter so all entries count as
    included."""
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"

    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    cands = good.enumerate_all(crew, spec)
    plan_path = sign_plan(good, crew, spec, out, select=None)
    assert (
        good.build_bundle(
            crew, spec, cands, good.merge_plans([plan_path], "frontdesk"), out
        ).skill_count
        == 0
    )

    bad = load_build(
        mutate=(
            "return {cid for cid, on in self.selections.get(kind, {}).items() if on}",
            "return {cid for cid, on in self.selections.get(kind, {}).items()}",
        )
    )
    out2 = tmp_path / "bundle2"
    plan_path2 = sign_plan(bad, crew, spec, out2, select=None)
    leaked = bad.build_bundle(
        crew, spec, bad.enumerate_all(crew, spec), bad.merge_plans([plan_path2], "frontdesk"), out2
    )
    assert leaked.skill_count == 1, "mutation must leak the unselected skill"


# ---------------------------------------------------------------------------
# GUARD 2: the signature -- an unsigned plan that selects is refused
# ---------------------------------------------------------------------------
def test_unsigned_plan_that_selects_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(
        mod, crew, spec, out, select={"skills": {"faq"}}, reviewed_by="", reviewed_at=""
    )
    with pytest.raises(mod.ExportRefused, match="unreviewed"):
        mod.merge_plans([plan_path], "frontdesk")


def test_MUTATION_signature(tmp_path):
    """Disable the is_signed check in verify; an unsigned selection now passes.
    (merge_plans also gates unsigned selections, so the mutation targets both the
    verify signature line and the merge signature line.)"""
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    cands = good.enumerate_all(crew, spec)
    plan_path = sign_plan(
        good, crew, spec, out, select={"skills": {"faq"}}, reviewed_by="", reviewed_at=""
    )
    with pytest.raises(good.ExportRefused):
        good.merge_plans([plan_path], "frontdesk")

    bad = load_build(
        mutate=(
            "if plan.selects_anything() and not plan.is_signed():",
            "if False and plan.selects_anything() and not plan.is_signed():",
        )
    )
    # merge no longer refuses; verify would still catch it -- unless verify's own
    # signature line is also disabled, which is the real guard under test here.
    bad2 = load_build(mutate=("if not plan.is_signed():", "if False:"))
    plan = bad.merge_plans([plan_path], "frontdesk")  # no raise now
    # feed the (unsigned) merged plan through the verify whose signature check is off
    drift = bad2.verify(plan, "frontdesk", cands)
    assert drift is not None, "mutation must let an unsigned plan pass verify"


# ---------------------------------------------------------------------------
# GUARD 3: the content pin -- a skill edited after approval is refused
# ---------------------------------------------------------------------------
def test_changed_selected_skill_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ v1"}})
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(mod, crew, spec, out, select={"skills": {"faq"}})
    # edit the skill AFTER it was reviewed
    (src / "skills" / "faq" / "SKILL.md").write_text("# FAQ v2 (tampered)", encoding="utf-8")
    plan = mod.merge_plans([plan_path], "frontdesk")
    with pytest.raises(mod.ExportRefused, match="changed after it was approved"):
        mod.verify(plan, "frontdesk", mod.enumerate_all(crew, spec))


def test_MUTATION_content_pin(tmp_path):
    """Disable the pin comparison in verify; a laundered (edited) skill now passes."""
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ v1"}})
    out = tmp_path / "bundle"
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    plan_path = sign_plan(good, crew, spec, out, select={"skills": {"faq"}})
    (src / "skills" / "faq" / "SKILL.md").write_text("# FAQ v2 (tampered)", encoding="utf-8")
    plan = good.merge_plans([plan_path], "frontdesk")
    with pytest.raises(good.ExportRefused, match="changed after it was approved"):
        good.verify(plan, "frontdesk", good.enumerate_all(crew, spec))

    bad = load_build(
        mutate=(
            "if pinned != candidate.content_hash:",
            "if False and pinned != candidate.content_hash:",
        )
    )
    plan2 = bad.merge_plans([plan_path], "frontdesk")
    drift = bad.verify(plan2, "frontdesk", bad.enumerate_all(crew, spec))  # no raise
    assert drift is not None, "mutation must let laundered content pass verify"


# ---------------------------------------------------------------------------
# GUARD 4: credential content scan -- a secret refuses the build
# ---------------------------------------------------------------------------
def test_skill_with_credential_is_blocked_and_refused(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        skills={"leaky": {"SKILL.md": f"# Leaky\nkey = {FAKE_AWS_KEY}\n"}},
    )
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    cands = mod.enumerate_all(crew, spec)
    faq = next(c for c in cands["skills"] if c.id == "leaky")
    assert faq.blocked, "a skill carrying a credential must be blocked"
    plan_path = sign_plan(mod, crew, spec, out, select={"skills": {"leaky"}})
    with pytest.raises(mod.ExportRefused, match="cannot be included"):
        mod.verify(mod.merge_plans([plan_path], "frontdesk"), "frontdesk", cands)


def test_MUTATION_credential_scan(tmp_path):
    """Disable scan_text; the credential skill is no longer blocked and would ship."""
    src = make_crew(
        tmp_path / "home",
        skills={"leaky": {"SKILL.md": f"# Leaky\nkey = {FAKE_AWS_KEY}\n"}},
    )
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    assert next(c for c in good.enumerate_all(crew, spec)["skills"] if c.id == "leaky").blocked

    bad = load_build(mutate=("            if m:", "            if m and False:"))
    leaky = next(c for c in bad.enumerate_all(crew, spec)["skills"] if c.id == "leaky")
    assert not leaky.blocked, "mutation must stop the scanner blocking a credential skill"


# ---------------------------------------------------------------------------
# GUARD 5: credential-store filename refusal
# ---------------------------------------------------------------------------
def test_skill_with_env_file_is_blocked(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        skills={"withenv": {"SKILL.md": "# ok", ".env": "SECRET=hunter2"}},
    )
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    leaky = next(c for c in mod.enumerate_all(crew, spec)["skills"] if c.id == "withenv")
    assert leaky.blocked and "credential store" in leaky.blocked


def test_MUTATION_credential_filename(tmp_path):
    """Disable refused_by_name; a skill carrying a .env is no longer blocked."""
    src = make_crew(
        tmp_path / "home",
        skills={"withenv": {"SKILL.md": "# ok", ".env": "SECRET=hunter2"}},
    )
    good = load_build()
    crew = good.resolve_crew("frontdesk", src)
    spec = good.read_agent_spec(crew)
    assert next(c for c in good.enumerate_all(crew, spec)["skills"] if c.id == "withenv").blocked

    bad = load_build(
        mutate=(
            "return bool(_CREDENTIAL_NAME_RE.match(path.name))",
            "return False",
        )
    )
    leaky = next(c for c in bad.enumerate_all(crew, spec)["skills"] if c.id == "withenv")
    assert not leaky.blocked, "mutation must stop the name gate blocking a .env skill"


# ---------------------------------------------------------------------------
# spec normalisation
# ---------------------------------------------------------------------------
def test_file_prompt_without_target_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="file:///gone/persona.md")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused, match="persona"):
        mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


def test_missing_prompt_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", prompt="   ")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    with pytest.raises(mod.ExportRefused, match="no prompt"):
        mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)


def test_orphan_tool_ref_dropped_when_server_not_selected(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        tools=["@internal-tools/query", "@builtin", "fs_read"],
        allowed_tools=["@internal-tools/query", "fs_read"],
        mcp_servers={"internal-tools": {"command": "/usr/local/bin/internal", "args": []}},
    )
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    # do not select the MCP server -> its @ref is an orphan and must be dropped
    mod.build_bundle(crew, spec, mod.enumerate_all(crew, spec), None, out)
    agent = json.loads((out / "agent.json").read_text())
    assert "@internal-tools/query" not in agent["tools"]
    assert "@builtin" in agent["tools"]  # native group survives
    assert "@internal-tools/query" not in agent["allowedTools"]
    assert json.loads((out / "mcp.json").read_text()) == {"mcpServers": {}}


def test_selected_mcp_server_ships_secret_stripped(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        mcp_servers={"weather": {"command": "weather-mcp", "env": {"TOKEN": "abc123"}}},
    )
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(mod, crew, spec, out, select={"mcp": {"weather"}})
    report = mod.build_bundle(
        crew, spec, mod.enumerate_all(crew, spec), mod.merge_plans([plan_path], "frontdesk"), out
    )
    mcp = json.loads((out / "mcp.json").read_text())["mcpServers"]
    assert "weather" in mcp
    # env is supplementary and dropped wholesale on export (see _clean_mcp_server)
    assert "env" not in mcp["weather"]
    assert mcp["weather"]["command"] == "weather-mcp"
    assert any("dropped env" in n for n in report.notes)
    assert report.mcp_servers == ["weather"]


def test_container_owned_mcp_is_blocked(tmp_path):
    mod = load_build()
    src = make_crew(
        tmp_path / "home",
        mcp_servers={"kirocrew-core": {"command": "/abs/path/kirocrew", "args": ["core"]}},
    )
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    c = next(x for x in mod.enumerate_all(crew, spec)["mcp"] if x.id == "kirocrew-core")
    assert c.blocked


def test_plan_for_another_crew_is_refused(tmp_path):
    mod = load_build()
    src = make_crew(tmp_path / "home", name="frontdesk")
    out = tmp_path / "bundle"
    crew = mod.resolve_crew("frontdesk", src)
    spec = mod.read_agent_spec(crew)
    plan_path = sign_plan(mod, crew, spec, out, select=None)
    doc = json.loads(plan_path.read_text())
    doc["crew"] = "someone-else"
    plan_path.write_text(json.dumps(doc))
    with pytest.raises(mod.ExportRefused, match="written for crew"):
        mod.merge_plans([plan_path], "frontdesk")


# ---------------------------------------------------------------------------
# CLI end-to-end via subprocess: SMC_BUNDLE_JSON is the LAST line
# ---------------------------------------------------------------------------
def test_cli_build_prints_bundle_json_last_line(tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "bundle"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "packaging.build",
            "--crew",
            "frontdesk",
            "--out",
            str(out),
            "--source",
            str(src),
        ],
        cwd=str(CREW_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    last = proc.stdout.strip().splitlines()[-1]
    assert last.startswith("SMC_BUNDLE_JSON="), proc.stdout
    payload = json.loads(Path(last.split("=", 1)[1]).read_text())
    assert payload["crew_name"] == "frontdesk"
    assert payload["bundle_dir"] == str(out)
    assert payload["digest"].startswith("sha256:")
    assert payload["skill_count"] == 0
    assert payload["mcp_servers"] == []
    assert any(d["id"] == "faq" for d in payload["denied"])
    # The fingerprint is part of the machine contract because T4's gate cannot derive
    # it: it is computed from the pre-injection content, which only the build sees.
    assert len(payload["fingerprint"]) == 24
    assert set(payload) == {
        "crew_name",
        "bundle_dir",
        "digest",
        "fingerprint",
        "skill_count",
        "mcp_servers",
        "denied",
    }


def test_cli_plan_writes_template_without_bundle(tmp_path):
    src = make_crew(tmp_path / "home", skills={"faq": {"SKILL.md": "# FAQ"}})
    out = tmp_path / "work"
    out.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "packaging.build",
            "plan",
            "--crew",
            "frontdesk",
            "--out",
            str(out),
            "--source",
            str(src),
        ],
        cwd=str(CREW_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "curation-plan.json").is_file()
    assert not (out / "manifest.json").exists()  # no bundle written
    doc = json.loads((out / "curation-plan.json").read_text())
    assert doc["reviewed_by"] == "" and doc["reviewed_at"] == ""
    assert all(e["include"] is False for e in doc["skills"])
