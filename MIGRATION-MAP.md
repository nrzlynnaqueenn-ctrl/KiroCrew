# Migration map: share-my-crew-app into aws-control

Frozen before dispatching parallel tracks. Source tree is
the standalone `share-my-crew-app` tree at `e4606f4`
(96 files, ~22,600 lines). Target is this worktree on
`feat/aws-control-crew-section`.

Every source file appears exactly once below, under PORT, ADAPT or SUPERSEDE.
A file with no row is a bug in this map, not a judgement call for whoever
finds it.

## The one thing that silently breaks

`setup.cfg [options.package_data]` globs builtin assets by FIXED subdirectory
name: `ui/`, `lib/`, `backend/`, `agents/`, `inject/`, `skills/`, `scripts/`,
and `*.md`. A new directory is **not shipped**, so everything works from a
source checkout and every file is missing on a pip or DMG install. This is the
failure the `apple_speech/*.swift` comment in that file describes.

So this migration adds one glob:

```
apps/builtins/*/crew/**/*
```

and a test asserting it, in the manner of
`test_vendored_llama_payload.py::test_package_data_declares_the_libs_explicitly`.
Track B owns both. Without the test the glob is a line nobody would notice
losing.

## Target layout

```
src/kiro_crew/apps/builtins/aws_control/
  backend/crews.py          DONE (9d8e8e89e) stack inventory via engine.run_aws
  backend/routes.py         DONE two GET routes
  crew/scripts/             the deploy driver and its guards
  crew/templates/           base.yaml, crew.yaml
  crew/packaging/           curation, bundle, fingerprint
  crew/runtime/             the container image source
  crew/*.md                 the two contracts that document live invariants
  skills/                   SKILL.md
website/src/apps/aws-control/
  CrewsPage.tsx             Track A
```

`crew/` rather than `crew_deploy/` because the directory holds the runtime too,
and a reader who opens `crew_deploy/runtime/` reasonably asks why a container's
source lives under a deploy directory.

## PORT — moves, logic unchanged

| from | to | note |
|---|---|---|
| `container/**` (25 files) | `crew/runtime/` | Runs INSIDE the deployed container, so the no-boto3 rule does not apply: that rule governs gateway code, and the container legitimately needs an S3 client in persistent mode. |
| `deploy/templates/base.yaml` | `crew/templates/` | |
| `deploy/templates/crew.yaml` | `crew/templates/` | Carries the `Memory` parameter and the `PersistentMemory` condition. |
| `deploy/smc-deploy.sh` | `crew/scripts/` | Ships as a shell script, with precedent: `deploy/skills/artifact-deploy/scripts/*.sh` is a whole set of them. Porting 2,000 lines to python would discard 147 gate tests and a driver proven on a real account, to gain nothing a reviewer asked for. |
| `deploy/tests/**` | `crew/scripts/tests/` | The 147 gate tests, the seam guard, the placeholder guard. These are the asset; they move with the driver. |
| `packaging/**` (5 files) | `crew/packaging/` | Pure python. Imported, so it needs `__init__.py`. |
| `skills/SKILL.md` | `aws_control/skills/` | `skills/` is already globbed. |
| `EPHEMERAL-CONTRACT.md` | `crew/` | Documents an invariant that is still live: what the two memory modes claim. |
| `PACKAGING-CONTRACT.md` | `crew/` | Same, for the crew-in-image seam. |
| `Dockerfile`, `Dockerfile.crew` | `crew/runtime/` | |

## ADAPT — logic survives, the shape changes

| from | to | what changes |
|---|---|---|
| `control/observe.py` stack half | `backend/crews.py` | **Done.** boto3 to `engine.run_aws`, plus an account-binding assertion the original did not need because it ran locally. |
| `control/observe.py` S3 half | `crew/conversations.py` | The conversation reader. Deferred deliberately: chatbot is the default mode and it has no S3 conversations, so shipping this now would be a reader for something no default deployment produces. Track B leaves a named stub and a note, not a half-port. |
| `backend/aws_read.py` | `backend/crews.py` sibling | ALB `RequestCount` on the crew's own target group, and the finding that API Gateway cannot answer per-crew traffic at all (`MetricsEnabled` unset, one REST API serves every crew). Keep `turns: null` with a `turnsBasis`: an upper bound must not be labelled as turns. |
| `backend/invoke.py` | `backend/crews.py` sibling | The SigV4 test-a-crew path. |
| `container/backup/layout.py` | moves with `container/` | Its `object_prefix` / `sessions_prefix` are the single source of truth the front process reads. Do not let a second copy appear during the move; `tests/test_transcript_key_agrees_with_sidecar.py` moves with it and holds that. |

## SUPERSEDE — deleted, with the thing that replaces it

| dropped | replaced by | why |
|---|---|---|
| `ui/**` (21 files, 4,367 lines) | `website/src/apps/aws-control/CrewsPage.tsx` | Vanilla `.mjs` behind `ui.entry`. No builtin uses that form; they all use React under `website/src/apps/`. The tile row, the alignment harness and `measure.mjs` do not survive, and their lesson does: the card grid this replaces them with already fixes card-to-card alignment with a fixed header height. |
| `backend/**` (11 files, 5,133 lines) minus the two adapted modules | `aws_control/backend/routes.py` | The app-backend plumbing (hooks, progress channel, job runner, `/pipe-check`) exists because the app was external. A builtin is registered by `BUILTIN_NAMES` and needs none of it. |
| `control/snapshot.py`, `control/crons/**`, `control/fetch.py` | the two GET routes | They wrote flat JSON files for the `.mjs` UI to read. A React page queries the route directly, so the snapshot layer, its atomic writer, its cron and `fetch-status.json` all go. This also closes a defect that was open: nothing in the old UI ever read `fetch-status.json`, so a stale snapshot and a broken fetcher looked identical. |
| `app.json` | `aws_control/app.json` | One app, one manifest. |
| `FETCHER-CONTRACT.md` | — | Froze the seam between `observe.py` and `snapshot.py`. Both sides are gone. |
| `BACKEND-API-CONTRACT.md` | — | Froze the external app-backend API. Superseded by the builtin's own routes. |
| `.gitignore` | the repo's own | |

## Track boundaries

**Track A owns** `website/src/apps/aws-control/**` and nothing else. It reads the
wire shape from `backend/crews.py`'s `crew_json`, which is already committed and
pinned by `test_the_wire_shape_carries_every_field_the_ui_reads`. If Track A needs
a field that does not exist, it says so rather than inventing one.

**Track B owns** `src/kiro_crew/apps/builtins/aws_control/crew/**`,
`aws_control/skills/**`, `setup.cfg`, and the moved tests. It must not touch
`website/`, `backend/crews.py` or `backend/routes.py`.

Neither track edits this file. If it is wrong, stop and say so.

## What must still be true at the end

- The full python suite passes, including the 147 gate tests in their new home.
- `vitest run` passes for the aws-control page.
- The dry run reaches 13 gate PASS in **both** memory modes from the new path.
- `package_data` ships `crew/**`, with a test that fails if the glob is removed.
- No file from the source tree is unaccounted for.
