"""``python -m packaging.build`` -- curate a local crew into a deployable bundle.

WHY THIS IS A PORT, NOT A COPY
------------------------------
``PACKAGING-CONTRACT.md`` (T1) says to port ``bundle.py`` + ``bundle_source.py``
from ``share-my-crew/build/serving/smc/`` and that those files "carry
``reviewed_by`` / ``reviewed_at`` and a content-hash recheck". Read in full,
they do NOT: ``serving/smc/bundle.py`` is the container's READER (it validates a
bundle at startup) and ``serving/smc/bundle_source.py`` is the S3 FETCH that the
top-level contract explicitly DELETES. Neither enumerates a crew, neither
curates, and neither carries a review signature or a content pin.

The deny-by-default producer the contract describes is
``share-my-crew/build/export/crew_export/`` -- ``candidates.py`` (enumeration,
everything starts excluded), ``plan.py`` (the ``reviewed_by`` / ``reviewed_at``
signature and the per-item sha256 content pin), ``spec.py`` (prompt inlining and
tool/MCP normalisation) and ``bundle.py`` (the layout writer and the digest the
contract points at: ``_bundle_digest``). This module ports THAT, because a port
of the named files would ship no curation at all -- and "a port that loosens
this is worse than no port".

The port is self-contained on purpose. ``crew_export`` imports
``kiro_crew.config.paths``, ``kiro_crew.knowledge.store``,
``kiro_crew.deploy.scan`` and ``kiro_crew.security``; NONE of those are importable
in this app's venv (it carries boto3 / fastapi / pydantic / pytest only, and no
PyYAML), so the curation plan is JSON rather than YAML and the credential
scanner is a self-contained subset of ``kiro_crew.deploy.scan`` -- see
``_HARD_PATTERNS`` and the report note about it.

THE DENY-BY-DEFAULT SEAM, PRESERVED
-----------------------------------
A skill or MCP server enters the bundle ONLY when a signed review says so and its
content still matches what was reviewed. Two guards, both from
``crew_export/plan.py``:

* **The signature.** ``reviewed_by`` and ``reviewed_at`` start blank; a review
  file that selects anything while either is blank is refused. There is no flag
  to skip review -- a flag fails open when forgotten. Running with no ``--allow``
  at all is a valid outcome: an empty-but-valid bundle (persona + tools, no
  private skills, no owner MCP servers), so the failure direction is
  under-sharing.
* **The content pin.** Every reviewed entry records the sha256 of the content it
  was written from, and the build re-checks that hash for each SELECTED entry. A
  skill or server edited after approval refuses the build and is named.
  Yesterday's approval cannot be laundered across today's content.

INTERFACE (PACKAGING-CONTRACT.md T1)
------------------------------------
    python -m packaging.build --crew <name> --out <dir> [--allow <path>]...
    python -m packaging.build plan  --crew <name> --out <dir> [--allow <path>]...

``build`` (the default verb) writes the four-entry layout into ``<dir>`` and
prints, as the LAST line, ``SMC_BUNDLE_JSON=<path>`` naming a JSON file with
``crew_name``, ``bundle_dir``, ``digest``, ``skill_count``, ``mcp_servers`` and
``denied``. ``plan`` prints the same decision set and writes a fresh
deny-by-default review template, WITHOUT writing a bundle.

``--crew`` names the crew; its source is a "crew home" holding
``agents/<name>.json`` and ``skills/``. ``--source`` overrides that root (a test
points it at a fixture); by default the agent spec resolves under
``$KIRO_HOME`` / ``~/.kiro`` and skills under ``$KIROCREW_HOME`` -- the same
locations Kiro Crew uses (``kiro_crew/config/paths.py:604`` ``kiro_agents_dir`` =
``kiro_home()/agents``, ``:510`` ``kiro_home``; ``config_dir()/skills`` per
``crew_export/candidates.py``). Never defaults to a temp dir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# The frozen layout the image copies in and the container reader validates.
BUNDLE_VERSION = 1
PLAN_VERSION = 1
PLAN_FILENAME = "curation-plan.json"

# MCP servers Kiro Crew resolves to an absolute path to a local binary; copying
# the definition ships a path that does not exist in the container. Ported from
# ``crew_export/candidates.py:_CONTAINER_OWNED_MCP``.
_CONTAINER_OWNED_MCP = frozenset(
    {"kirocrew-core", "kirocrew-cron", "kirocrew-computer", "kirocrew-dashboard"}
)

# `@builtin` names kiro-cli's own native tool group, not an MCP server, so a
# tool reference to it is never treated as dangling. Ported from
# ``serving/smc/bundle.py:BUILTIN_TOOL_GROUPS``.
_BUILTIN_TOOL_GROUPS = frozenset({"builtin"})

# Spec keys dropped on export. Ported from ``crew_export/spec.py:_DROPPED_KEYS``:
# an inherited security posture or a file outside the bundle is a silent policy
# change in the deployment.
_DROPPED_SPEC_KEYS = ("hooks", "includeMcpJson")


# ---------------------------------------------------------------------------
# Failure mode: refusal only. Ported from ``crew_export/errors.py``.
# ---------------------------------------------------------------------------
class ExportRefused(RuntimeError):
    """The export cannot proceed and no bundle was written.

    A warning the operator can scroll past is not a control, so every guard
    aborts rather than degrading -- the alternative is shipping a bundle wrong in
    the one direction that matters.
    """


# ===========================================================================
# Credential scanning -- refuse, never warn.
#
# Ported in INTENT from ``crew_export/scan.py``, which delegates to
# ``kiro_crew.deploy.scan`` for the canonical pattern set. That module is NOT
# importable in this venv, so the hard-credential patterns below are a
# self-contained subset. This is a real narrowing versus the source and is
# called out in the track report: a credential shape the canonical set knows and
# this subset does not would pass. The credential-NAME gate is ported verbatim.
# ===========================================================================
_HARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("vendor-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
)

# Filenames that are credential stores by convention, matched before any read.
# Ported verbatim from ``crew_export/scan.py:_CREDENTIAL_NAME_RE`` (that regex is
# self-contained; only the ``is_sensitive_path`` fallback, which needs
# ``kiro_crew.security``, is dropped).
_CREDENTIAL_NAME_RE = re.compile(r"""(?ix)
    ^(
        \.env(\..*)?
      | .*\.pem
      | .*\.p12
      | .*\.pfx
      | .*\.key
      | id_(rsa|dsa|ecdsa|ed25519)(\.pub)?
      | \.npmrc
      | \.netrc
      | \.pgpass
      | credentials(\.json)?
      | client_secret.*\.json
      | service[-_]account.*\.json
      | .*\.kdbx
      | \.htpasswd
    )$
    """)


@dataclass(frozen=True)
class Leak:
    origin: str
    kind: str
    line: int
    snippet: str

    def render(self) -> str:
        return f"{self.origin}:{self.line}: {self.kind}: {self.snippet}"


def refused_by_name(path: Path) -> bool:
    """True when a path is a credential store by its name alone.

    A ``.pem`` that happens not to match a content regex is still a private key,
    so the name is judged before the bytes are read.
    """
    return bool(_CREDENTIAL_NAME_RE.match(path.name))


def scan_text(text: str, origin: str) -> list[Leak]:
    """Hard credential findings in *text*. A finding aborts the build."""
    leaks: list[Leak] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _HARD_PATTERNS:
            m = pattern.search(line)
            if m:
                token = m.group(0)
                snippet = token[:4] + "…(%d chars)" % len(token)
                leaks.append(Leak(origin=origin, kind=kind, line=lineno, snippet=snippet))
    return leaks


# ===========================================================================
# Candidate enumeration -- everything starts excluded.
# Ported from ``crew_export/candidates.py`` (skills + mcp only: the app's
# four-entry layout has no workspace/ or knowledge/, so those categories, and
# the sqlite knowledge walk behind them, are deliberately not ported).
# ===========================================================================
@dataclass
class Candidate:
    kind: str  # "skills" | "mcp"
    id: str
    #: sha256 of the candidate's content; the pin the review records and the
    #: build re-checks. Empty only for a blocked candidate that was never read.
    content_hash: str
    note: str = ""
    #: Set when structurally ineligible (a credential store); refused if selected.
    blocked: str = ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_hash(root: Path) -> str:
    """A content hash over every file in a directory, path-and-content, sorted.

    Any byte or any filename changing changes the hash -- the property the
    content pin needs. Modelled on ``crew_export/candidates.py``'s skill
    ``tree_hash``, widened to hash every file rather than only ``SKILL.md`` so an
    edit to any file in the skill invalidates approval.
    """
    rows: list[list[str]] = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            rows.append([p.relative_to(root).as_posix(), _sha(p.read_bytes())])
    return _sha(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def skill_candidates(skills_root: Path) -> list[Candidate]:
    """Skill directories (each dir holding a ``SKILL.md``), deny-by-default.

    Skills are global on the owner's machine and many drive ``gh``, an AWS
    profile, Playwright or the loopback gateway -- none of which exist in a
    customer-facing container -- so selection is a deployment judgement and every
    skill starts excluded.
    """
    if not skills_root.is_dir():
        return []
    out: list[Candidate] = []
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        skill_dir = skill_md.parent
        rel = skill_dir.relative_to(skills_root).as_posix()
        # Credential store inside the skill => blocked, never includable.
        cred_file = next(
            (p for p in sorted(skill_dir.rglob("*")) if p.is_file() and refused_by_name(p)),
            None,
        )
        if cred_file is not None:
            out.append(
                Candidate(
                    kind="skills",
                    id=rel,
                    content_hash="",
                    blocked=f"contains a credential store: "
                    f"{cred_file.relative_to(skill_dir).as_posix()}",
                )
            )
            continue
        # A hard credential in any readable file blocks the skill too.
        hard_hit = ""
        for p in sorted(skill_dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            text = _read_text(p)
            if text is None:
                continue
            leaks = scan_text(text, f"skills/{rel}/{p.relative_to(skill_dir).as_posix()}")
            if leaks:
                hard_hit = f"contains a credential -- {leaks[0].render()}"
                break
        if hard_hit:
            out.append(Candidate(kind="skills", id=rel, content_hash="", blocked=hard_hit))
            continue
        out.append(Candidate(kind="skills", id=rel, content_hash=_tree_hash(skill_dir)))
    return out


def _canonical_server(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True, ensure_ascii=False)


def mcp_candidates(agent_spec: dict) -> list[Candidate]:
    """MCP servers declared by the crew's agent spec, deny-by-default.

    Ported from ``crew_export/candidates.py:mcp_candidates``: a server reasonable
    on the owner's laptop may be a customer-reachable side effect in production,
    so tool surface is a deployment decision and an empty ``mcp.json`` is the
    expected outcome, not a degraded one.
    """
    servers = agent_spec.get("mcpServers")
    if not isinstance(servers, dict):
        return []
    out: list[Candidate] = []
    for name, spec in sorted(servers.items()):
        if not isinstance(spec, dict):
            continue
        canonical = _canonical_server(spec)
        if name in _CONTAINER_OWNED_MCP:
            out.append(
                Candidate(
                    kind="mcp",
                    id=name,
                    content_hash=_sha(canonical.encode("utf-8")),
                    blocked="a Kiro Crew-managed server that resolves to an absolute "
                    "path on this machine; the container composes its own",
                )
            )
            continue
        leaks = scan_text(canonical, f"mcp/{name}")
        blocked = f"contains a credential -- {leaks[0].render()}" if leaks else ""
        out.append(
            Candidate(
                kind="mcp",
                id=name,
                content_hash=_sha(canonical.encode("utf-8")),
                blocked=blocked,
            )
        )
    return out


# ===========================================================================
# The crew source.
# ===========================================================================
@dataclass(frozen=True)
class ResolvedCrew:
    name: str
    agent_spec_path: Path
    skills_root: Path


def _default_kiro_home() -> Path:
    override = os.environ.get("KIRO_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kiro"


def _default_config_dir() -> Path:
    override = os.environ.get("KIROCREW_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kirocrew"


def resolve_crew(name: str, source: Path | None) -> ResolvedCrew:
    """Resolve a crew's agent spec and skills root.

    With ``--source`` (or ``$SMC_CREW_SOURCE``) the root holds ``agents/`` and
    ``skills/`` -- the shape a test fixture provides. Without it, the real
    locations are used: the agent spec under ``$KIRO_HOME``/``~/.kiro/agents``
    and skills under ``$KIROCREW_HOME``. Never a temp dir.
    """
    if source is not None:
        return ResolvedCrew(
            name=name,
            agent_spec_path=source / "agents" / f"{name}.json",
            skills_root=source / "skills",
        )
    return ResolvedCrew(
        name=name,
        agent_spec_path=_default_kiro_home() / "agents" / f"{name}.json",
        skills_root=_default_config_dir() / "skills",
    )


def read_agent_spec(crew: ResolvedCrew) -> dict:
    path = crew.agent_spec_path
    if not path.is_file():
        raise ExportRefused(
            f"no agent spec for crew {crew.name!r} at {path}. There is nothing to "
            f"deploy; check --crew / --source."
        )
    text = _read_text(path)
    if text is None:
        raise ExportRefused(f"agent spec {path} is not decodable as UTF-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExportRefused(f"agent spec {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExportRefused(f"agent spec {path} must be a JSON object")
    return parsed


def enumerate_all(crew: ResolvedCrew, agent_spec: dict) -> dict[str, list[Candidate]]:
    return {
        "skills": skill_candidates(crew.skills_root),
        "mcp": mcp_candidates(agent_spec),
    }


# ===========================================================================
# The curation plan (review file): deny-by-default, signature, content pin.
# Ported from ``crew_export/plan.py`` -- JSON instead of YAML (no PyYAML here).
# ===========================================================================
_KINDS = ("skills", "mcp")

_PLAN_INSTRUCTIONS = (
    "Everything below starts include:false. Flip include:true on the skills and "
    "MCP servers a customer may reach, fill in reviewed_by and reviewed_at, then "
    "pass this file to the build with --allow. Leaving it untouched is valid: you "
    "get a working crew with its persona and no private content. Do not hand-edit "
    "sha256 -- it pins each entry to the content you reviewed; if a SELECTED entry "
    "changes afterwards the build refuses and names it. A 'blocked' entry cannot "
    "be included at all."
)


@dataclass
class Plan:
    crew: str
    reviewed_by: str
    reviewed_at: str
    selections: dict[str, dict[str, bool]]
    pins: dict[str, dict[str, str]]

    def included(self, kind: str) -> set[str]:
        return {cid for cid, on in self.selections.get(kind, {}).items() if on}

    def is_signed(self) -> bool:
        return bool(self.reviewed_by.strip()) and bool(self.reviewed_at.strip())

    def selects_anything(self) -> bool:
        return any(self.included(kind) for kind in _KINDS)


@dataclass
class Drift:
    appeared: int = 0
    vanished: int = 0

    def describe(self) -> str:
        parts = []
        if self.appeared:
            parts.append(f"{self.appeared} new candidate(s) appeared (all excluded)")
        if self.vanished:
            parts.append(f"{self.vanished} candidate(s) no longer exist")
        return "; ".join(parts)


def write_plan(path: Path, crew: str, candidates: dict[str, list[Candidate]]) -> None:
    """Write a fresh deny-by-default review template."""
    body: dict[str, object] = {
        "plan_version": PLAN_VERSION,
        "crew": crew,
        "instructions": _PLAN_INSTRUCTIONS,
        "reviewed_by": "",
        "reviewed_at": "",
    }
    for kind in _KINDS:
        entries = []
        for c in candidates.get(kind, []):
            entry: dict[str, object] = {"id": c.id, "include": False, "sha256": c.content_hash}
            if c.note:
                entry["note"] = c.note
            if c.blocked:
                entry["blocked"] = c.blocked
            entries.append(entry)
        body[kind] = entries
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_plan(path: Path) -> Plan:
    if not path.is_file():
        raise ExportRefused(f"no curation plan at {path}. Run the plan command first.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ExportRefused(f"curation plan {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExportRefused(f"curation plan {path} is not an object")
    if raw.get("plan_version") != PLAN_VERSION:
        raise ExportRefused(
            f"curation plan version {raw.get('plan_version')!r} is not {PLAN_VERSION}; "
            f"regenerate it"
        )
    selections: dict[str, dict[str, bool]] = {}
    pins: dict[str, dict[str, str]] = {}
    for kind in _KINDS:
        entries = raw.get(kind) or []
        if not isinstance(entries, list):
            raise ExportRefused(f"curation plan section {kind!r} is not a list")
        sel: dict[str, bool] = {}
        pin: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or "id" not in entry:
                raise ExportRefused(f"malformed entry in {kind!r}: {entry!r}")
            cid = str(entry["id"])
            sel[cid] = bool(entry.get("include", False))
            pin[cid] = str(entry.get("sha256") or "")
        selections[kind] = sel
        pins[kind] = pin
    return Plan(
        crew=str(raw.get("crew", "")),
        reviewed_by=str(raw.get("reviewed_by") or ""),
        reviewed_at=str(raw.get("reviewed_at") or ""),
        selections=selections,
        pins=pins,
    )


def verify(plan: Plan, crew: str, candidates: dict[str, list[Candidate]]) -> Drift:
    """Refuse unless signed and every selected item is byte-for-byte as reviewed.

    Ported from ``crew_export/plan.py:verify``. Drift outside the selection is
    reported, never refused on: a file the operator did not choose cannot reach
    the bundle, so blocking on it is a false alarm.
    """
    if plan.crew != crew:
        raise ExportRefused(f"plan was written for crew {plan.crew!r}, not {crew!r}")
    if not plan.is_signed():
        raise ExportRefused(
            "curation plan is unreviewed: reviewed_by and reviewed_at are blank. "
            "Read the plan, choose what customers may reach, sign it, then build. "
            "There is deliberately no flag to skip this."
        )
    by_kind = {kind: {c.id: c for c in candidates.get(kind, [])} for kind in _KINDS}
    drift = Drift()
    for kind in _KINDS:
        live = set(by_kind[kind])
        planned = set(plan.selections.get(kind, {}))
        drift.appeared += len(live - planned)
        drift.vanished += len(planned - live)
        for cid in plan.included(kind):
            candidate = by_kind[kind].get(cid)
            if candidate is None:
                raise ExportRefused(f"plan selects {kind}/{cid!r}, which no longer exists")
            if candidate.blocked:
                raise ExportRefused(
                    f"plan selects {kind}/{cid!r}, which cannot be included: {candidate.blocked}"
                )
            pinned = plan.pins.get(kind, {}).get(cid, "")
            if not pinned:
                raise ExportRefused(
                    f"plan selects {kind}/{cid!r} with no recorded content hash, so "
                    f"what was approved cannot be established. Re-run the plan."
                )
            if pinned != candidate.content_hash:
                raise ExportRefused(
                    f"{kind}/{cid} changed after it was approved, so the approval no "
                    f"longer covers it.\n  reviewed: {pinned}\n  current:  "
                    f"{candidate.content_hash}\nRe-run the plan command and look again."
                )
    return drift


def merge_plans(paths: list[Path], crew: str) -> Plan | None:
    """Union the selections of one or more signed review files.

    Each file must match the crew and, if it selects anything, be signed;
    otherwise its selections are refused rather than silently ignored. Returns
    ``None`` when no ``--allow`` was given (pure deny-by-default: an empty
    bundle).
    """
    if not paths:
        return None
    merged_sel: dict[str, dict[str, bool]] = {k: {} for k in _KINDS}
    merged_pins: dict[str, dict[str, str]] = {k: {} for k in _KINDS}
    reviewers: list[str] = []
    reviewed_ats: list[str] = []
    for p in paths:
        plan = read_plan(p)
        if plan.crew != crew:
            raise ExportRefused(f"--allow {p} was written for crew {plan.crew!r}, not {crew!r}")
        if plan.selects_anything() and not plan.is_signed():
            raise ExportRefused(
                f"--allow {p} selects items but is unreviewed (reviewed_by / "
                f"reviewed_at are blank). Sign it or its selections are refused."
            )
        if plan.is_signed():
            reviewers.append(plan.reviewed_by)
            reviewed_ats.append(plan.reviewed_at)
        for kind in _KINDS:
            for cid, on in plan.selections.get(kind, {}).items():
                merged_sel[kind][cid] = merged_sel[kind].get(cid, False) or on
                pin = plan.pins.get(kind, {}).get(cid, "")
                if pin:
                    merged_pins[kind][cid] = pin
    return Plan(
        crew=crew,
        reviewed_by="; ".join(sorted(set(r for r in reviewers if r))),
        reviewed_at="; ".join(sorted(set(a for a in reviewed_ats if a))),
        selections=merged_sel,
        pins=merged_pins,
    )


# ===========================================================================
# Spec build -- inline the prompt, normalise tools/MCP.
# Ported from ``crew_export/spec.py`` and the reader guards in
# ``serving/smc/bundle.py`` (validate_prompt, validate_tool_refs).
# ===========================================================================
def _resolve_prompt_path(raw: str, agents_dir: Path) -> Path:
    target = raw[len("file://") :]
    path = Path(target)
    if not path.is_absolute():
        path = (agents_dir / target).resolve()
        try:
            path.relative_to(agents_dir.resolve())
        except ValueError:
            raise ExportRefused(f"prompt URI {raw!r} escapes the agents directory") from None
    posix = path.as_posix()
    for root in ("/proc", "/sys", "/dev"):
        if posix == root or posix.startswith(root + "/"):
            raise ExportRefused(f"prompt URI {raw!r} points at a pseudo-filesystem")
    if refused_by_name(path):
        raise ExportRefused(f"prompt URI {raw!r} points at a credential location")
    return path


def _inline_prompt(spec: dict, crew_name: str, agents_dir: Path, notes: list[str]) -> None:
    """Inline a ``file://`` prompt as literal text; refuse a missing persona.

    Kiro Crew writes an installed agent's prompt as ``file://<absolute host
    path>`` (``kiro_crew/agent.py:2166``). That path does not exist in the
    container, so a naively copied spec produces a crew that answers as nobody --
    and kiro-cli tolerates an empty prompt, so the failure is silent. Refused
    here (``serving/smc/bundle.py:validate_prompt`` refuses it at startup too).
    """
    raw = spec.get("prompt")
    if raw is None or not isinstance(raw, str) or not raw.strip():
        raise ExportRefused(
            f"agent.json for {crew_name!r} has no prompt. The prompt is the crew's "
            f"persona and kiro-cli tolerates an empty one, so a crew shipped this way "
            f"answers as nobody. Inline the persona as literal text."
        )
    if not raw.strip().lower().startswith("file://"):
        leaks = scan_text(raw, "prompt")
        if leaks:
            raise ExportRefused("the crew's prompt contains a credential: " + leaks[0].render())
        return
    path = _resolve_prompt_path(raw.strip(), agents_dir)
    if not path.is_file():
        raise ExportRefused(
            f"prompt file {path} does not exist, so the crew's persona cannot be "
            f"bundled. Exporting as-is would deploy a crew that answers as nobody."
        )
    text = _read_text(path)
    if text is None or not text.strip():
        raise ExportRefused(f"prompt file {path} is empty or not UTF-8 text")
    leaks = scan_text(text, f"prompt({path.name})")
    if leaks:
        raise ExportRefused("the crew's prompt contains a credential: " + leaks[0].render())
    spec["prompt"] = text
    notes.append(f"inlined prompt from {path} ({len(text)} chars)")


def _clean_mcp_server(name: str, server: dict, notes: list[str]) -> dict:
    """Strip secret-bearing material from one server before it ships.

    ``env`` and ``headers`` are SUPPLEMENTARY and are dropped WHOLESALE, not
    scanned-and-kept. Two reasons this is stricter than
    ``crew_export/spec.py:_clean_mcp_server`` (which keeps benign env): the plan's
    own operator-facing note says "env, headers stripped on export", so keeping
    them contradicts what the owner was told; and a bespoke token format the
    scanner does not recognise would otherwise ship. Dropping them leaves a server
    that fails loudly at connect time -- the safe direction -- and the deployment
    re-supplies whatever the container genuinely needs. This tightening is called
    out in the track report.

    ``args`` and ``url`` are LOAD-BEARING: a credential there refuses the export
    rather than being edited out, because a server minus one arg connects and
    misbehaves. (Ported unchanged from spec.py.)
    """
    out = dict(server)
    for field_name in ("env", "headers"):
        block = out.get(field_name)
        if isinstance(block, dict) and block:
            out.pop(field_name)
            notes.append(
                f"mcp/{name}: dropped {field_name} ({len(block)} entr(y/ies); "
                f"supplementary and can bear a credential, so re-supply via the "
                f"deployment if needed)"
            )
    for field_name in ("args", "url"):
        value = out.get(field_name)
        if not value:
            continue
        if scan_text(json.dumps(value, ensure_ascii=False), f"mcp/{name}/{field_name}"):
            raise ExportRefused(
                f"MCP server {name!r} carries a credential in {field_name!r}. That "
                f"field cannot be stripped without breaking the server, so the export "
                f"refuses. Move the value into an env var or a vault reference and re-plan."
            )
    return out


@dataclass
class SpecResult:
    spec: dict
    mcp: dict
    notes: list[str] = field(default_factory=list)


def build_spec(
    crew: ResolvedCrew, agent_spec: dict, selected_mcp: set[str], agents_dir: Path
) -> SpecResult:
    """Produce the bundle's ``agent.json`` and ``mcp.json`` from a source spec."""
    notes: list[str] = []
    spec = json.loads(json.dumps(agent_spec))  # detach from the source mapping

    if spec.get("name") != crew.name:
        notes.append(f"renamed spec {spec.get('name')!r} -> {crew.name!r}")
    spec["name"] = crew.name

    _inline_prompt(spec, crew.name, agents_dir, notes)

    for key in _DROPPED_SPEC_KEYS:
        if key in spec:
            spec.pop(key)
            notes.append(f"dropped {key!r}: it is a deployment decision, not the owner's")

    # MCP: keep only what curation approved, cleaned of secret material.
    raw_servers = agent_spec.get("mcpServers")
    source_servers: dict = raw_servers if isinstance(raw_servers, dict) else {}
    mcp: dict[str, dict] = {}
    for name in sorted(selected_mcp):
        server = source_servers.get(name)
        if not isinstance(server, dict):
            raise ExportRefused(
                f"plan selects MCP server {name!r}, which the spec no longer declares"
            )
        mcp[name] = _clean_mcp_server(name, server, notes)
    dropped = sorted(set(source_servers) - set(mcp))
    if dropped:
        notes.append(f"MCP servers not selected: {', '.join(dropped)}")

    # Both files are emitted from this one dict so they cannot drift within a build
    # (crew_export/spec.py records the bug where they did). agent.json stays
    # installable as-is.
    if mcp:
        spec["mcpServers"] = mcp
    else:
        spec.pop("mcpServers", None)

    # tools: a `@server` reference to a server curation removed leaves the crew
    # holding a tool that points at nothing (kiro-cli drops it silently at mount
    # time). `@builtin` is kiro-cli's native group and is NOT an orphan.
    removed_servers = set(source_servers) - set(mcp)

    def _is_orphan(entry: str) -> bool:
        if not entry.startswith("@"):
            return False
        server = entry[1:].split("/", 1)[0]
        return server not in _BUILTIN_TOOL_GROUPS and server in removed_servers

    tools = spec.get("tools")
    if isinstance(tools, list):
        kept = [str(e) for e in tools if not _is_orphan(str(e))]
        orphans = [str(e) for e in tools if _is_orphan(str(e))]
        spec["tools"] = kept
        if orphans:
            notes.append("removed tool references with no surviving server: " + ", ".join(orphans))

    # allowedTools cannot inflate past the surviving tools: a grant for a tool the
    # bundle no longer carries is dropped.
    final_tools = set(spec.get("tools") or [])
    inherited = [t for t in (spec.get("allowedTools") or []) if isinstance(t, str)]
    granted = sorted(t for t in inherited if t in final_tools)
    if sorted(inherited) != granted:
        notes.append(f"allowedTools narrowed to surviving tools ({len(granted)} kept)")
    spec["allowedTools"] = granted

    rendered = json.dumps(spec, indent=2, ensure_ascii=False)
    if scan_text(rendered, "agent.json"):
        raise ExportRefused("the agent spec contains a credential after cleaning")

    return SpecResult(spec=spec, mcp=mcp, notes=notes)


# ===========================================================================
# Bundle writer + digest. Ported from ``crew_export/bundle.py``.
# ===========================================================================
def prompt_fingerprint(crew_name: str, pre_digest: str) -> str:
    """A value only the packaged prompt can produce.

    Every other gate can pass while the wrong crew answers. The image digest proves
    the right ARTIFACT is deployed; the container's checks prove the right bundle is
    INSTALLED; the crew address proves the request named this crew. None of them
    proves the answer came from the packaged PROMPT, and the first live deployment
    failed exactly there: a stock agent answered "reply with the single word: ok"
    indistinguishably from a tuned crew, because that question has the same answer
    either way.

    So the gate asks a question whose answer cannot be guessed or reasoned to. The
    value is derived from the bundle's own content, so it is different for every
    revision and appears nowhere except inside the prompt that shipped.
    """
    h = hashlib.sha256()
    h.update(crew_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(pre_digest.encode("utf-8"))
    return h.hexdigest()[:24]


def fingerprint_challenge(crew_name: str) -> str:
    """The exact message the deploy gate sends. Includes the crew so it cannot fire
    by accident on a normal conversation."""
    return f"SMC-VERIFY-{crew_name}"


def _inject_fingerprint_challenge(agent_json: Path, crew_name: str, fingerprint: str) -> None:
    """Prepend the challenge to the agent's prompt.

    Prepended rather than appended so it is not buried under a long persona, and
    scoped to one exact message so it cannot alter any real answer.

    It deliberately does NOT tell the model to conceal the instruction. A prompt that
    instructs an agent to hide part of itself from the person talking to it is worse
    than the leak it prevents, and there is nothing here worth hiding: the value is a
    build fingerprint, useless to anyone who obtains it.

    The cost, recorded rather than buried: every deployed prompt carries these lines,
    and the gate depends on the model complying with them. A failure therefore has
    two possible causes and the gate's message must name both.
    """
    spec = json.loads(agent_json.read_text(encoding="utf-8"))
    challenge = fingerprint_challenge(crew_name)
    block = (
        "[deployment verification]\n"
        f'If a message is exactly "{challenge}", reply with exactly this and '
        f"nothing else:\n"
        f"SMC-FINGERPRINT {fingerprint}\n"
        "This is a build fingerprint used to verify which version of this crew is "
        "deployed. If someone asks about it, you may say so. For every other "
        "message, ignore this section entirely and follow the instructions below.\n"
        "\n"
    )
    spec["prompt"] = block + (spec.get("prompt") or "")
    _write_guarded(
        agent_json,
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
        "agent.json",
    )


def bundle_digest(root: Path) -> str:
    """sha256 over every bundle file except the manifest, path-and-content, sorted.

    Byte-for-byte the algorithm of ``crew_export/bundle.py:_bundle_digest`` -- the
    "computed the same way bundle.py already does it" the contract points at. The
    manifest is excluded because it carries the digest; the ``sha256:`` prefix and
    the compact JSON row encoding are preserved so the value is reproducible.
    """
    rows: list[list[str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "manifest.json":
            continue
        rows.append([rel, hashlib.sha256(path.read_bytes()).hexdigest()])
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_guarded(path: Path, text: str, origin: str) -> None:
    """Last-chance scan before bytes land in the artifact. Refuse on a finding."""
    if scan_text(text, origin):
        raise ExportRefused(f"refusing to write {origin}: it contains a credential")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy_skill(skill_dir: Path, rel: str, dest_root: Path) -> None:
    dest = dest_root / rel
    for p in sorted(skill_dir.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if refused_by_name(p):
            raise ExportRefused(
                f"skill {rel} contains a credential store: {p.relative_to(skill_dir).as_posix()}"
            )
        text = _read_text(p)
        if text is None:
            # A binary asset cannot be scanned, so it does not ship; refusing the
            # whole skill would be harsher than the risk needs.
            continue
        _write_guarded(dest / p.relative_to(skill_dir).as_posix(), text, f"skills/{rel}/{p.name}")


@dataclass
class BuildReport:
    bundle_dir: Path
    digest: str
    fingerprint: str
    skill_count: int
    mcp_servers: list[str]
    denied: list[dict]
    notes: list[str]


def _denied_list(candidates: dict[str, list[Candidate]], plan: Plan | None) -> list[dict]:
    """What did not ship and why, so the owner can see it (SMC_BUNDLE_JSON.denied)."""
    out: list[dict] = []
    for kind in _KINDS:
        included = plan.included(kind) if plan else set()
        for c in candidates.get(kind, []):
            if c.id in included:
                continue
            if c.blocked:
                reason = c.blocked
            elif plan is None:
                reason = "no curation plan supplied (deny-by-default)"
            else:
                reason = "not marked reviewed in the plan (deny-by-default)"
            out.append({"kind": kind, "id": c.id, "reason": reason})
    return out


def build_bundle(
    crew: ResolvedCrew,
    agent_spec: dict,
    candidates: dict[str, list[Candidate]],
    plan: Plan | None,
    out_dir: Path,
) -> BuildReport:
    """Write the four-entry bundle for *crew*, or refuse and leave nothing behind."""
    included_mcp = plan.included("mcp") if plan else set()
    included_skills = plan.included("skills") if plan else set()

    result = build_spec(crew, agent_spec, included_mcp, crew.agent_spec_path.parent)

    staging = out_dir.parent / (out_dir.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        _write_guarded(
            staging / "agent.json",
            json.dumps(result.spec, indent=2, ensure_ascii=False) + "\n",
            "agent.json",
        )
        _write_guarded(
            staging / "mcp.json",
            json.dumps({"mcpServers": result.mcp}, indent=2, ensure_ascii=False) + "\n",
            "mcp.json",
        )
        skills_dst = staging / "skills"
        skills_dst.mkdir(exist_ok=True)  # MUST exist even when empty
        for cid in sorted(included_skills):
            skill_dir = crew.skills_root / cid
            if not skill_dir.is_dir():
                raise ExportRefused(f"selected skill has gone: {cid}")
            _copy_skill(skill_dir, cid, skills_dst)

        # The fingerprint is derived from the content BEFORE it is injected, which is
        # what breaks the circularity: a value computed from the finished bundle
        # cannot be part of that bundle. `digest` in the manifest is still the FINAL
        # content digest, so the container's recompute check is unaffected (the
        # manifest itself is excluded from the digest).
        fingerprint = prompt_fingerprint(crew.name, bundle_digest(staging))
        _inject_fingerprint_challenge(staging / "agent.json", crew.name, fingerprint)

        digest = bundle_digest(staging)
        _write_guarded(
            staging / "manifest.json",
            json.dumps(
                {
                    "bundle_version": BUNDLE_VERSION,
                    "crew_name": crew.name,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "digest": digest,
                    "fingerprint": fingerprint,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            "manifest.json",
        )
        if out_dir.exists():
            shutil.rmtree(out_dir)
        staging.rename(out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    skill_count = len([p for p in (out_dir / "skills").iterdir() if p.is_dir()])
    return BuildReport(
        bundle_dir=out_dir,
        digest=digest,
        fingerprint=fingerprint,
        skill_count=skill_count,
        mcp_servers=sorted(result.mcp),
        denied=_denied_list(candidates, plan),
        notes=result.notes,
    )


# ===========================================================================
# CLI
# ===========================================================================
def _decision_set(candidates: dict[str, list[Candidate]], plan: Plan | None) -> dict:
    included = {kind: sorted(plan.included(kind)) if plan else [] for kind in _KINDS}
    return {"included": included, "denied": _denied_list(candidates, plan)}


def _print_decision(decision: dict) -> None:
    for kind in _KINDS:
        ids = decision["included"][kind]
        print(f"  include {kind:<7} {len(ids)}: {', '.join(ids) or '(none)'}")
    print(f"  denied {len(decision['denied'])}:")
    for d in decision["denied"]:
        print(f"    - {d['kind']}/{d['id']}: {d['reason']}")


def _cmd_plan(crew_name: str, out: Path, allow: list[Path], source: Path | None) -> int:
    crew = resolve_crew(crew_name, source)
    agent_spec = read_agent_spec(crew)
    candidates = enumerate_all(crew, agent_spec)

    plan_path = out / PLAN_FILENAME
    if not plan_path.is_file():
        write_plan(plan_path, crew.name, candidates)
        print(f"wrote deny-by-default review template: {plan_path}")
        print("Everything is excluded. Nothing ships until you sign it and pass it with --allow.")
    else:
        print(f"review template already present: {plan_path} (left as-is)")

    plan = merge_plans(allow, crew.name)
    if plan is not None:
        verify(plan, crew.name, candidates)  # refuse an unsigned/laundered --allow early
    print("decision set (no bundle written):")
    _print_decision(_decision_set(candidates, plan))
    return 0


def _cmd_build(crew_name: str, out: Path, allow: list[Path], source: Path | None) -> int:
    crew = resolve_crew(crew_name, source)
    agent_spec = read_agent_spec(crew)
    candidates = enumerate_all(crew, agent_spec)

    plan = merge_plans(allow, crew.name)
    if plan is not None:
        drift = verify(plan, crew.name, candidates)
    else:
        drift = Drift()

    report = build_bundle(crew, agent_spec, candidates, plan, out)

    payload = {
        "crew_name": crew.name,
        "bundle_dir": str(report.bundle_dir),
        "digest": report.digest,
        "fingerprint": report.fingerprint,
        "skill_count": report.skill_count,
        "mcp_servers": report.mcp_servers,
        "denied": report.denied,
    }
    json_path = out.parent / f"{out.name}.smc-bundle.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Human-readable progress first; the machine marker is the LAST line.
    print(f"bundle:  {report.bundle_dir}")
    print(f"digest:  {report.digest}")
    print(f"skills:  {report.skill_count}")
    print(f"mcp:     {', '.join(report.mcp_servers) or '(none)'}")
    if report.denied:
        print(f"denied:  {len(report.denied)} (see SMC_BUNDLE_JSON)")
    if drift.describe():
        print(f"note:    since the plan was written, {drift.describe()}")
    for note in report.notes:
        print(f"  - {note}")
    if not report.skill_count and not report.mcp_servers:
        print("Nothing private was selected: a valid bundle with the crew's persona only.")
    print(f"SMC_BUNDLE_JSON={json_path}")
    return 0


def _source_from(args_source: str | None) -> Path | None:
    raw = args_source or os.environ.get("SMC_CREW_SOURCE")
    return Path(raw).expanduser() if raw else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m packaging.build",
        description="Curate a local crew into a deployable bundle (deny-by-default).",
    )

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--crew", required=True, help="crew name")
        p.add_argument("--out", type=Path, required=True, help="bundle output directory")
        p.add_argument(
            "--allow",
            type=Path,
            action="append",
            default=[],
            metavar="PATH",
            help="a signed curation plan whose selected skills/MCP servers may ship "
            "(repeatable). Omit for an empty-but-valid bundle.",
        )
        p.add_argument(
            "--source",
            default=None,
            help="crew home holding agents/<name>.json and skills/ (defaults to the "
            "real Kiro Crew locations; $SMC_CREW_SOURCE also honoured).",
        )

    sub = parser.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="print the decision set and write a review template")
    _add_common(p_plan)
    p_build = sub.add_parser("build", help="write the bundle (the default verb)")
    _add_common(p_build)

    # `build` is the default verb: if the first token is neither a subcommand nor
    # a top-level help flag, inject it. Done here rather than by putting the shared
    # required args on the top parser, which would make argparse demand them before
    # the subcommand token and reject `plan --crew ...`.
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("plan", "build", "-h", "--help"):
        pass
    else:
        raw = ["build"] + raw

    args = parser.parse_args(raw)
    source = _source_from(args.source)
    try:
        if args.cmd == "plan":
            return _cmd_plan(args.crew, args.out, args.allow, source)
        return _cmd_build(args.crew, args.out, args.allow, source)
    except ExportRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
