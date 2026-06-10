# The Sentinel Engine

An autonomous Python pipeline that takes a GitHub issue URL and produces a merge-ready Pull Request for open-source Go repositories. You provide credentials and run one command; the engine forks the repo, triages the issue, plans and applies a fix, verifies it with the Go toolchain, and opens a PR on your fork.

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [What It Does Not Do](#what-it-does-not-do)
3. [Pipeline Overview](#pipeline-overview)
4. [Issue Admission Gate](#issue-admission-gate)
5. [Fix Execution (Small vs Medium)](#fix-execution-small-vs-medium)
6. [Architecture](#architecture)
7. [Directory Structure](#directory-structure)
8. [Prerequisites](#prerequisites)
9. [Installation](#installation)
10. [Configuration](#configuration)
11. [Running the Engine](#running-the-engine)
12. [Understanding the Output](#understanding-the-output)
13. [Cost Per Run](#cost-per-run)
14. [Design Decisions](#design-decisions)
15. [Troubleshooting](#troubleshooting)

---

## What It Does

Given a GitHub issue URL, a Gemini API key, and a GitHub Personal Access Token, the engine:

1. Forks the target repository into your GitHub account and clones it locally
2. Fetches the issue and runs structured triage with Gemini 2.5 Pro
3. **Admits or rejects** the issue based on bug-fix suitability (see [Issue Admission Gate](#issue-admission-gate))
4. Indexes all Go symbols (functions, methods, structs) into a local SQLite cache keyed by git commit
5. Plans one or two fix hypotheses (depending on complexity tier) with exact file and symbol targets
6. Applies patches in isolated Git worktrees with per-patch verification and automatic error recovery
7. Runs `go build`, `go vet`, and `go test` on the patched code
8. On failure, replans with a memory of what already failed
9. Commits the winning patch, pushes to your fork, and opens a cross-repo PR upstream

The PR URL is printed at the end of a successful run.

**Validated example:** [spf13/cobra #2193](https://github.com/spf13/cobra/issues/2193) — context propagation bug in `ExecuteC` — fixed and PR opened automatically.

---

## What It Does Not Do

To set expectations clearly:

- **No feature or enhancement work** — only surgical bug fixes in existing code (`replace` patches; no new APIs or files)
- **No security/CVE issues** — rejected at admission
- **No architectural rewrites** — issues requiring 3+ files or large refactors are rejected
- **No image/screenshot analysis** — issue images are not processed
- **No cross-run learning** — each `python src/main.py` invocation starts fresh
- **No Docker** — native Git worktrees and the local Go toolchain only
- **No vector database** — symbol lookup is exact, via SQLite

---

## Pipeline Overview

```
python src/main.py
```

| Phase | What happens |
|-------|----------------|
| **0 — Env check** | Verifies `go` and `git` are on PATH |
| **1 — Fork & clone** | Forks upstream into your account, waits until ready, clones your fork locally |
| **2 — Triage** | Fetches issue title, body, labels; Gemini 2.5 Pro returns structured `TriageAnalysis` JSON |
| **3 — Index** | Walks `.go` files, extracts symbols + line ranges → `.cache/state_cache.db` |
| **3b — Admission** | Code-level pre-flight: labels, confidence, symbol resolution, file existence |
| **4 — Plan** | Gemini 2.5 Pro produces `StrategyBlueprint` (1 or 2 hypotheses with `FilePatch` lists) |
| **5 — Execute** | Parallel worktree tracks apply patches with micro-heal + verify-after-patch |
| **6 — Replan** | If all tracks fail tests, replan with `FailureMemory` (1× for small, up to 2× for medium) |
| **7 — PR** | Gemini 2.5 Pro generates conventional-commit title + Markdown body |
| **8 — Push & open PR** | Pushes branch to your fork, opens `your-fork:branch → upstream:main` |

### Fork model

The engine never pushes to upstream directly. It forks into your account, patches your fork, and opens a PR from `your-username:branch` to `upstream-owner:main`.

---

## Issue Admission Gate

Before any fix planning runs, the engine decides whether the issue is worth attempting. This keeps cost low (~$0.05) on unsuitable issues and focuses effort on fixable bugs.

### Admitted: small and medium bugs

| Tier | Criteria | Execution |
|------|----------|-----------|
| **Small** | 1 file, 1 symbol, stack trace or clear repro, confidence ≥ 0.85 | 1 track, max 1 patch, 1 replan |
| **Medium** | 1–2 files, clear bug (not feature), confidence ≥ 0.70 | 2 tracks (ALPHA + BETA), max 2 patches, up to 2 replans |

### Rejected automatically

| Reason | Signals |
|--------|---------|
| Feature / proposal | `feat:` title, labels `type/proposal`, `enhancement`, `proposal` |
| Security | Label `security`, CVE/auth/crypto content |
| Architecture | 3+ files, rewrite language, `is_architectural` flag |
| Unclear direction | Labels `needs-discussion`, `question`, open debate |
| New code required | New functions, files, or public API as primary deliverable |
| Symbol not found | Anchor function/method cannot be resolved in the SQLite index |
| Low confidence | Below tier threshold |

When rejected, the run exits after triage with a message like:

```
ADMISSION REJECTED: Title indicates feature/proposal: 'feat: add SSE support'
```

Example: [gin #4661](https://github.com/gin-gonic/gin/issues/4661) (SSE feature proposal) is rejected; [cobra #2193](https://github.com/spf13/cobra/issues/2193) (one-line logic bug) is admitted.

### Triage output schema

```json
{
  "symptom": "Subcommand receives stale context on second ExecuteContext call",
  "target_package": "cobra",
  "potential_files": ["command.go"],
  "reproduction_steps": "Execute subcommand twice with different contexts...",
  "is_breaking_change_risk": false,
  "complexity": "small",
  "confidence": 1.0,
  "anchor_symbol": "ExecuteC",
  "target_file": "command.go",
  "requires_new_code": false,
  "is_security_related": false,
  "is_architectural": false,
  "maintainer_decision_unclear": false,
  "estimated_files": 1,
  "has_clear_repro": true,
  "admitted": true,
  "reject_reason": ""
}
```

---

## Fix Execution (Small vs Medium)

### Patch model

Each fix hypothesis contains an ordered list of `FilePatch` entries:

```json
{
  "title": "Propagate context to subcommand in ExecuteC",
  "patches": [
    {
      "target_file": "command.go",
      "start_line": 1084,
      "end_line": 1170,
      "anchor_symbol": "ExecuteC",
      "patch_mode": "replace",
      "description": "Always assign cmd.ctx = c.ctx before execute",
      "new_code": "..."
    }
  ]
}
```

Rules enforced in code:

- **`replace` only** — modifies existing logic; `insert_after` patches are dropped
- **Max 1 replace per file** per hypothesis — prevents stale line-number drift from duplicate patches on the same file
- **Symbol re-anchoring** — before each patch, `anchor_symbol` is resolved fresh from SQLite so line numbers stay current after prior edits
- **Stop on failure** — if a patch cannot be healed, remaining patches are skipped and touched files are reverted with `git checkout --`

### Per-patch verification and micro-heal

After each patch is applied:

1. `gofmt -e` (inside ACI; reverts file on syntax failure)
2. `go build` on the changed package

If verification fails, a micro-heal loop runs before giving up:

| Tier | Flash heal attempts | Pro escalation |
|------|---------------------|----------------|
| Small | Up to 3 | 1 final attempt with Gemini 2.5 Pro |
| Medium | Up to 2 | 1 final attempt with Gemini 2.5 Pro |

Heal prompts use structured error classification (syntax, undefined, type) and read ±5 lines around the compiler error line for precision.

### Full test suite

After all patches pass per-patch verification:

- `go build ./...`
- `go vet ./...`
- `go test -short` on changed packages (with baseline diffing — pre-existing failures in the upstream repo are ignored)

### Replanning (not same-blueprint retry)

If tracks fail the full test suite, the engine does **not** retry the identical blueprint. It calls Gemini 2.5 Pro again with a `FailureMemory` list:

```json
{
  "track": "TRACK_ALPHA",
  "file": "command.go",
  "symbol": "ExecuteC",
  "error": "BUILD FAIL: ...",
  "code_attempted": "...",
  "cycle": 1
}
```

The replanner must produce a **different** approach than all recorded failures.

### LLM call budget

Hard caps prevent runaway cost:

| Tier | Max LLM calls |
|------|---------------|
| Small | 10 |
| Medium | 14 |

### Branch naming

Branches are unique per run:

```
sentinel/issue-{N}/{track}-c{cycle}-{unix_timestamp}
```

Example: `sentinel/issue-2193/track-alpha-c1-1781081207`

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  main.py                                                        │
│    env check → GitHubClient (fork, clone) → SentinelPipeline    │
└────────────────────────────┬────────────────────────────────────┘
                             │
              SentinelPipeline.run_pipeline()
                             │
     ┌───────────────────────┼────────────────────────┐
     │                       │                        │
┌────▼─────┐          ┌──────▼──────┐          ┌──────▼──────┐
│  Triage  │          │   Indexer   │          │ PR Generator│
│ Pro JSON │          │ SQLite AST  │          │  Pro → MD   │
│ + gate   │          │   symbols   │          └─────────────┘
└────┬─────┘          └──────┬──────┘
     │                       │
     └───────────┬───────────┘
                 │
        ┌────────▼────────┐
        │ Strategy Plan   │  Gemini 2.5 Pro → StrategyBlueprint
        └────────┬────────┘
                 │
    ┌────────────┴────────────┐
    │ small: 1 track          │
    │ medium: ALPHA + BETA    │  asyncio.gather (medium only)
    └────────────┬────────────┘
                 │
        ┌────────▼────────────────────────┐
        │  Worktree + ACI per track       │
        │  re-anchor → patch → micro-heal │
        │  → go build → (next patch)      │
        │  → go vet + go test             │
        └────────┬────────────────────────┘
                 │ on failure
        ┌────────▼────────┐
        │ Replan + retry  │  FailureMemory → Pro
        └────────┬────────┘
                 │ winner
        ┌────────▼────────┐
        │ commit → push   │
        │ → open PR       │
        └─────────────────┘
```

### Module reference

| File | Responsibility |
|------|----------------|
| `src/main.py` | Entry point: env check, fork setup, boots pipeline |
| `src/pipeline.py` | Orchestrator: admission, planning, tracks, micro-heal, replan |
| `src/github_client.py` | GitHub REST API: fork, clone, push, open PR |
| `src/ingestion/triage.py` | Issue fetch, structured triage, `run_admission_checks()` |
| `src/ingestion/pr_generator.py` | PR title + multi-patch Markdown body |
| `src/indexing/indexer.py` | Go symbol parser, SQLite cache, `resolve_symbol()` |
| `src/aci/tools.py` | Safe file read, range-bound patch, `gofmt` + `go build` verify |
| `src/verification/worktree.py` | Git worktree create/cleanup |
| `src/verification/tester.py` | Async `go build` + `go vet` + `go test` with baseline diffing |

### LLM model usage

| Task | Model |
|------|-------|
| Issue triage + admission fields | `gemini-2.5-pro` |
| Fix planning + replanning | `gemini-2.5-pro` |
| Code generation + micro-heal (Flash attempts) | `gemini-2.5-flash` |
| Micro-heal Pro escalation | `gemini-2.5-pro` |
| PR title and body | `gemini-2.5-pro` |

---

## Directory Structure

```
Gitub-agent/
├── src/
│   ├── main.py
│   ├── pipeline.py
│   ├── github_client.py
│   ├── ingestion/
│   │   ├── triage.py
│   │   └── pr_generator.py
│   ├── indexing/
│   │   └── indexer.py
│   ├── aci/
│   │   └── tools.py
│   └── verification/
│       ├── worktree.py
│       └── tester.py
├── .cache/                  # SQLite index (auto-created, gitignored)
│   └── state_cache.db
├── worktrees/               # Temporary worktrees (auto-cleaned)
├── .env                     # Credentials (never commit)
├── .env.example
├── requirements.txt
└── README.md
```

After the first real run, a clone of the target repo also appears at `./{repo-name}/` (e.g. `./cobra/`).

---

## Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| Python | 3.11+ | `python --version` |
| Git | any recent | `git --version` |
| Go | 1.20+ | `go version` |
| Gemini API key | — | https://aistudio.google.com |
| GitHub PAT | `repo` scope | https://github.com/settings/tokens |

---

## Installation

```bash
git clone <this-repo-url>
cd Gitub-agent

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Dependencies: `google-genai`, `python-dotenv`, `pydantic`.

---

## Configuration

Copy the example and fill in your values:

```bash
cp .env.example .env
```

```env
GEMINI_API_KEY=your_gemini_api_key_here
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_your_github_token_here
GITHUB_ISSUE_URL=https://github.com/spf13/cobra/issues/2193

# Optional: seed a local mock Go repo instead of forking (still needs token + issue URL)
SENTINEL_USE_MOCK_REPO=false
```

### Choosing a good issue

Pick **small or medium bug fixes** in any open-source Go repository:

- Stack trace or minimal reproduction in the issue body
- Fix is a guard, condition, or logic correction in existing code
- Title starts with `BUG:` or `fix:` rather than `feat:`

**Good examples:**

| Issue | Why |
|-------|-----|
| [cobra #2193](https://github.com/spf13/cobra/issues/2193) | Small — one method, one-line logic fix, clear repro |
| [cobra #2104](https://github.com/spf13/cobra/issues/2104) | Medium — `command.go`, repro gist, no open PR |

**Reject examples (engine will exit early):**

| Issue | Why |
|-------|-----|
| [gin #4661](https://github.com/gin-gonic/gin/issues/4661) | Feature proposal — new SSE methods |
| Issues labeled `security`, `type/proposal`, `needs-discussion` | Admission gate |

---

## Running the Engine

```bash
python src/main.py
```

### Example successful output (abbreviated)

```
Authenticated as: your-username
Forking spf13/cobra → your-username/cobra ...
Fork cloned successfully.

Analyzing issue with Gemini 2.5 Pro (structured triage + admission)...
Issue admitted as SMALL bug (confidence: 1.0)

Generating fix blueprint(s) with Gemini 2.5 Pro...
   Track ALPHA: 'Propagate Context to Subcommand in ExecuteC' (1 patch)

Deploying cycle 1 track(s)...
[TRACK_ALPHA][patch 1/1] → command.go [ExecuteC] lines 1084-1170
PASSED ALL VERIFICATIONS

PR opened: https://github.com/spf13/cobra/pull/XXXX
```

### Mock mode

```env
SENTINEL_USE_MOCK_REPO=true
```

Skips fork/clone and seeds a minimal local Go repo with a division-by-zero bug. Still requires `GEMINI_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`, and `GITHUB_ISSUE_URL` in `.env`.

---

## Understanding the Output

### Admission rejected

```
ADMISSION REJECTED: <reason>
Exiting early — no planning or patching will run.
```

The issue is unsuitable for this engine. Pick a different bug-fix issue.

### Small vs medium tracks

- **Small bugs** run a single `TRACK_ALPHA` — one hypothesis, one patch, lower cost
- **Medium bugs** run `TRACK_ALPHA` and `TRACK_BETA` in parallel — two different strategies; if both pass, ALPHA wins

### All tracks exhausted

```
System Regression: All tracks and replans exhausted.
LLM calls used: 8/10
```

No valid fix was found within the budget. Try a simpler issue or one with a clearer stack trace.

### SQLite cache

Indexed per `(repo_name, git_commit_hash)`. On cache hit:

```
Index Cache Hit! Codebase map loaded instantly from local storage.
```

Pull new upstream commits to trigger a re-index.

---

## Cost Per Run

Approximate Gemini API usage:

| Scenario | LLM calls | ~Cost |
|----------|-----------|-------|
| Rejected at admission gate | 1 (triage only) | ~$0.05 |
| Small bug, first-try success | 5–6 | ~$0.30 |
| Small bug with micro-heal + replan | up to 10 (cap) | ~$0.50 |
| Medium bug, dual track success | 6–8 | ~$0.35 |
| Medium bug, full recovery | up to 14 (cap) | ~$0.65 |

Pro calls (triage, planning, replan, PR, escalation) dominate cost. Flash calls (code gen, heal) are cheap and parallelizable.

---

## Design Decisions

**Git worktrees over Docker** — Isolated directories in milliseconds, no daemon, native `go test` on the host.

**SQLite over vectors** — Bug fixes need exact file paths and line numbers, not semantic similarity. A regex-based Go symbol parser + SQLite gives precise `resolve_symbol()` lookups.

**Pydantic schemas on every LLM call** — Gemini `response_schema` guarantees parseable JSON for triage, blueprints, and PR drafts.

**Admission gate before planning** — Rejects features and ambiguous issues for ~$0.05 instead of spending $0.30+ on a doomed patch attempt.

**Symbol anchoring + verify-after-patch** — Re-resolving line numbers before each patch and running `go build` after each edit prevents cascading failures from stale planner coordinates.

**Tiered execution** — Small bugs get depth (more heal attempts, focused single track). Medium bugs get breadth (two parallel tracks, two files max).

**Baseline-aware tests** — Upstream repos often have pre-existing test flakes. The tester stashes the patch, records baseline failures, and only flags new failures.

---

## Troubleshooting

### `Missing system dependencies: go`
Install Go from https://go.dev/dl/ and restart your terminal.

### `ADMISSION REJECTED: Anchor symbol 'X' not found`
The triage model guessed a wrong symbol name. Issues that cite exact file + line numbers (e.g. `command.go at lines 1111`) work best. The engine attempts to resolve symbols from line hints in the issue body.

### `Missing credentials in .env`
All three variables are required: `GEMINI_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `GITHUB_ISSUE_URL`.

### `Clone failed: Repository not found`
Verify your GitHub token has the `repo` scope.

### `Parsing Failure: malformed JSON`
Rare with schema enforcement. Re-run; lower temperature is already set.

### `Branch transfer failed` (but PR still opened)
The patch committed inside the worktree but local branch fetch had a transient issue. If push succeeded and the PR URL printed, the run succeeded.

### Stale local fork after a previous run

```bash
cd cobra   # or your repo directory
git checkout .
git clean -fd
git pull origin main
```

### SQLite cache after pulling new commits

The cache keys on commit hash. A new commit triggers automatic re-indexing on the next run.

---

*Python + Google Gemini. One command from issue URL to Pull Request.*
