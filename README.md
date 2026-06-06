# 🛡️ The Sentinel Engine

> An autonomous, parallel AI coding agent that resolves GitHub issues in open-source Go repositories — end to end, from issue URL to merged-ready Pull Request.

---

## Table of Contents

1. [What This Is](#what-this-is)
2. [How It Works — The Full Pipeline](#how-it-works--the-full-pipeline)
3. [Architecture Deep Dive](#architecture-deep-dive)
4. [Directory Structure](#directory-structure)
5. [Prerequisites](#prerequisites)
6. [Installation & Setup](#installation--setup)
7. [Configuration](#configuration)
8. [Running the Engine](#running-the-engine)
9. [Understanding the Output](#understanding-the-output)
10. [Cost Per Run](#cost-per-run)
11. [Design Decisions](#design-decisions)
12. [Supported Repositories](#supported-repositories)
13. [Troubleshooting](#troubleshooting)

---

## What This Is

The Sentinel Engine is a deterministic, zero-dependency Python state machine that autonomously resolves GitHub issues in open-source Go repositories. Given nothing but a GitHub issue URL and API credentials, it:

1. Reads and structurally understands the issue using Gemini 2.5 Pro
2. Forks the target repository into your GitHub account
3. Indexes the Go codebase locally into a SQLite symbol cache
4. Generates two distinct fix strategies in parallel, each in an isolated Git Worktree
5. Applies each patch, runs `go vet` and `go test` to verify correctness
6. If both fail, feeds the compiler errors back to Gemini for automatic repair (Self-Healing Loop)
7. Commits the winning patch, pushes it to your fork, and opens a real Pull Request

**The entire process is zero-touch.** You run one command. The PR link is printed at the end.

---

## How It Works — The Full Pipeline

Here is a step-by-step walkthrough of exactly what happens when you run the engine:

```
python src/main.py
```

### Phase 0 — Environment Check
The engine verifies that `go` and `git` are installed and accessible in your system PATH. If either is missing, the engine exits with a clear error message before doing anything else.

### Phase 1 — GitHub Fork Setup
Using your Personal Access Token, the engine:
- Calls `GET /user` to resolve your GitHub username from the token
- Calls `POST /repos/{owner}/{repo}/forks` to fork the target repository into your account
- Polls `GET /repos/{you}/{repo}` every 3 seconds until GitHub confirms the fork is ready (forks are created asynchronously)
- Clones **your fork** locally using a token-authenticated HTTPS URL — `git` never prompts for a password

This is the correct open-source contribution model. You never push directly to the upstream repo. All changes go to your fork, and the PR is opened from `your-fork:branch → upstream:main`.

### Phase 2 — Issue Triage (Gemini 2.5 Pro)
The engine fetches the full issue body from the GitHub REST API and passes it to Gemini 2.5 Pro with a strict Pydantic schema. The model returns a structured JSON object containing:

```json
{
  "symptom": "Nil pointer panic when command context is nil",
  "target_package": "cobra",
  "potential_files": ["command.go"],
  "reproduction_steps": "Create a parent command with PersistentPreRun that sets context...",
  "is_breaking_change_risk": false
}
```

This structured output — not free-form text — is what drives every downstream decision. No hallucinated file paths. No vague descriptions.

### Phase 3 — AST Symbol Indexing (SQLite Cache)
The engine walks the cloned repository and lexically parses every `.go` file (excluding test files) to extract:
- Function names and signatures
- Method names and receivers
- Struct and interface definitions
- Exact start and end line numbers for each symbol

All of this is stored in a local SQLite database (`.cache/state_cache.db`). On subsequent runs against the same Git commit, the index is loaded instantly from cache — no re-parsing.

This eliminates the need for a vector database. The agent knows *exactly* which file and which lines to look at, rather than doing fuzzy semantic search.

### Phase 4 — Strategy Planning (Gemini 2.5 Pro)
Using the structured triage result and the top symbol matches from the SQLite cache, the engine asks Gemini 2.5 Pro to design exactly **two distinct fix strategies**. The output is again a strict Pydantic schema:

```json
{
  "hypotheses": [
    {
      "title": "Inherit Context Recursively from Parent Command",
      "target_file": "command.go",
      "start_line": 232,
      "end_line": 237,
      "proposed_code": "..."
    },
    {
      "title": "Propagate Context via Target Command in Execution Loop",
      "target_file": "command.go",
      "start_line": 951,
      "end_line": 958,
      "proposed_code": "..."
    }
  ]
}
```

Having two distinct hypotheses means the engine hedges its bets — two different interpretations of the fix run simultaneously.

### Phase 5 — Parallel Execution (Git Worktrees + asyncio)
For each hypothesis, the engine:

1. Creates an isolated **Git Worktree** — a separate physical directory on disk, each on its own branch (`sentinel/track_alpha`, `sentinel/track_beta`). Worktrees share the same `.git` directory but have completely independent working trees. This means both tracks can modify files and run tests simultaneously without interfering with each other.

2. Reads the current source code at the target line range using the Agent-Computer Interface (ACI), giving the LLM precise context — not the entire file.

3. Calls **Gemini 2.5 Flash** to generate the actual replacement Go code for those lines.

4. Applies the patch via the ACI's range-bound injector, which splices the new code into the exact line range and runs `gofmt -e` immediately to catch syntax errors before tests even run.

5. Runs `go vet ./...` and `go test -short ./...` **concurrently** using Python's `asyncio.gather()`.

Both tracks run simultaneously. Neither blocks the other.

### Phase 6 — Self-Healing Loop (Gemini 2.5 Flash)
If both tracks fail verification, the engine does not give up. It:

1. Captures the exact compiler/linter error from each failed track
2. Feeds it back to Gemini 2.5 Flash along with the previous failed code attempt
3. Asks for a targeted correction with the error as context
4. Deploys a second parallel cycle (Cycle 2) with the healed code

This means the engine can recover from LLM output that compiles but has a logic error, or that has a simple syntax mistake — automatically, without human intervention.

### Phase 7 — PR Generation (Gemini 2.5 Pro)
Once a winning track is identified (one that passes all tests), the engine generates a full Pull Request using Gemini 2.5 Pro with another Pydantic schema. The output includes:
- A conventional-commit-style PR title (e.g., `fix(cobra): propagate parent context to child commands`)
- A structured Markdown body with: Summary, Problem, Solution, Changes table, Testing section, Checklist

### Phase 8 — Push & Open PR
The engine:
1. Commits the patch to the winning branch in the worktree
2. Fetches that branch back into the main local repo
3. Pushes the branch to **your fork** on GitHub
4. Opens a cross-repo PR via `POST /repos/{upstream}/{repo}/pulls` with `head: "you:branch"` and `base: "main"`
5. Prints the live PR URL

---

## Architecture Deep Dive

```
┌─────────────────────────────────────────────────────────────────┐
│                        main.py                                  │
│  ┌──────────────┐   ┌──────────────────────────────────────┐   │
│  │ Env Check    │   │        GitHubClient                  │   │
│  │ go, git      │   │  fork → wait → clone fork            │   │
│  └──────────────┘   └──────────────────────────────────────┘   │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                    SentinelPipeline.run_pipeline()
                              │
          ┌───────────────────┼────────────────────┐
          │                   │                    │
   ┌──────▼──────┐   ┌────────▼────────┐  ┌───────▼────────┐
   │   Triage    │   │  AST Indexer    │  │  PR Generator  │
   │  (Gemini    │   │  (SQLite cache  │  │  (Gemini 2.5   │
   │   2.5 Pro)  │   │   go file walk) │  │   Pro → MD)    │
   └─────────────┘   └─────────────────┘  └────────────────┘
          │
   ┌──────▼──────────────────────────────────┐
   │         Strategy Planning               │
   │         (Gemini 2.5 Pro → JSON)         │
   └──────┬──────────────────────┬───────────┘
          │                      │
   ┌──────▼──────┐        ┌──────▼──────┐
   │ TRACK_ALPHA │        │  TRACK_BETA │   ← asyncio.gather()
   │ Worktree A  │        │  Worktree B │   ← fully isolated
   │             │        │             │
   │ Gemini 2.5  │        │ Gemini 2.5  │
   │ Flash →     │        │ Flash →     │
   │ ACI patch → │        │ ACI patch → │
   │ go vet/test │        │ go vet/test │
   └──────┬──────┘        └──────┬──────┘
          │    (if both fail)    │
          └──────────┬───────────┘
                     │
          ┌──────────▼───────────┐
          │   Self-Healing Loop  │
          │ (Gemini 2.5 Flash +  │
          │  compiler error log) │
          └──────────┬───────────┘
                     │ winning track
          ┌──────────▼───────────┐
          │   git commit →       │
          │   git push fork →    │
          │   GitHub PR API      │
          └──────────────────────┘
```

### Module Reference

| File | Responsibility |
|------|----------------|
| `src/main.py` | Entry point. Env check, fork setup, pipeline boot |
| `src/pipeline.py` | Orchestrator. Runs all phases, manages parallel tracks |
| `src/github_client.py` | GitHub REST API: fork, clone, push, open PR |
| `src/ingestion/triage.py` | GitHub issue fetch + Gemini structured triage |
| `src/ingestion/vision.py` | Extracts and analyzes image attachments in issues |
| `src/ingestion/pr_generator.py` | Generates PR title + Markdown body via Gemini |
| `src/indexing/indexer.py` | Go AST symbol parser + SQLite cache |
| `src/aci/tools.py` | Safe file reader + range-bound patch applicator + gofmt check |
| `src/verification/worktree.py` | Git Worktree lifecycle (create, cleanup) |
| `src/verification/tester.py` | Async `go vet` + `go test` runner |

### LLM Model Strategy

The engine uses a deliberate two-tier model approach:

| Task | Model | Reason |
|------|-------|--------|
| Issue triage (structured JSON) | `gemini-2.5-pro` | Requires deep reasoning to extract structured constraints from free-form bug reports |
| Fix planning (two hypotheses) | `gemini-2.5-pro` | Complex multi-step analysis of AST symbols + bug context to produce precise line targets |
| Code generation (patch writing) | `gemini-2.5-flash` | High-velocity, runs twice in parallel — Flash is fast and cost-efficient for targeted code rewrites |
| Self-healing (error correction) | `gemini-2.5-flash` | Runs in a tight loop with compiler feedback — speed matters here |
| PR generation (Markdown body) | `gemini-2.5-pro` | Needs to produce coherent, professional prose that accurately references code changes |

---

## Directory Structure

```
sentinel-agent/
│
├── src/
│   ├── main.py                  # Entry point
│   ├── pipeline.py              # Core orchestration state machine
│   ├── github_client.py         # GitHub API: fork, clone, push, open PR
│   │
│   ├── ingestion/
│   │   ├── triage.py            # Issue parsing + Gemini structured triage
│   │   ├── vision.py            # Image attachment analysis
│   │   └── pr_generator.py      # PR title + body generation
│   │
│   ├── indexing/
│   │   └── indexer.py           # Go AST parser + SQLite symbol cache
│   │
│   ├── aci/
│   │   └── tools.py             # Agent-Computer Interface (read + patch files)
│   │
│   └── verification/
│       ├── worktree.py          # Git Worktree manager
│       └── tester.py            # Async go vet + go test runner
│
├── .cache/                      # SQLite index (auto-generated, gitignored)
│   └── state_cache.db
│
├── worktrees/                   # Temporary isolated branches (auto-cleaned)
│
├── .env                         # Your credentials (never commit this)
├── .env.example                 # Template
├── requirements.txt
└── README.md
```

---

## Prerequisites

Before running the engine, you need the following installed on your machine.

### 1. Python 3.11 or higher

Check your version:
```bash
python --version
```

Install if needed:
- **Windows**: `winget install --id Python.Python.3.12 -e`
- **macOS**: `brew install python@3.12`
- **Linux**: `sudo apt install python3.12`

### 2. Git

Check:
```bash
git --version
```

Install:
- **Windows**: `winget install --id Git.Git -e`
- **macOS**: `brew install git`
- **Linux**: `sudo apt install git`

### 3. Go Compiler (1.20 or higher)

Check:
```bash
go version
```

Install from https://go.dev/dl/ or:
- **Windows**: `winget install --id GoLang.Go -e`
- **macOS**: `brew install go`
- **Linux**: `sudo apt install golang-go`

> ⚠️ After installing Go on Windows, restart your terminal so the PATH updates take effect.

### 4. API Keys (see Configuration section below)

- A **Google Gemini API key** (free tier available at https://aistudio.google.com)
- A **GitHub Personal Access Token** with `repo` scope

---

## Installation & Setup

### Step 1 — Clone this repository

```bash
git clone https://github.com/yourusername/sentinel-agent.git
cd sentinel-agent
```

### Step 2 — Create a Python virtual environment

**Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

You should see `(venv)` in your terminal prompt after activation.

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `google-genai` — official Google Generative AI SDK
- `python-dotenv` — loads credentials from `.env` file
- `pydantic` — structured data validation for LLM outputs

That's it. No Docker, no vector databases, no heavy ML dependencies.

---

## Configuration

### Step 1 — Create your `.env` file

Copy the example file:
```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```env
GEMINI_API_KEY=your_gemini_api_key_here
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_your_github_token_here
GITHUB_ISSUE_URL=https://github.com/spf13/cobra/issues/2194

# Set to "true" to skip GitHub forking and use a local mock repo (offline dev only)
SENTINEL_USE_MOCK_REPO=false
```

### Step 2 — Get a Gemini API Key

1. Go to https://aistudio.google.com
2. Sign in with your Google account
3. Click **Get API Key** → **Create API Key**
4. Copy the key into `GEMINI_API_KEY` in your `.env`

> The free tier is sufficient for testing. Each full run costs approximately **₹30** (see Cost section).

### Step 3 — Get a GitHub Personal Access Token

1. Go to https://github.com/settings/tokens
2. Click **Generate new token (classic)**
3. Give it a name like `sentinel-engine`
4. Under **Scopes**, check **`repo`** (this covers forking, pushing, and opening PRs)
5. Click **Generate token**
6. Copy the token (starts with `ghp_`) into `GITHUB_PERSONAL_ACCESS_TOKEN` in your `.env`

> ⚠️ You only see the token once. Copy it immediately before closing the page.

### Step 4 — Choose a GitHub Issue

Set `GITHUB_ISSUE_URL` to any issue from the supported repositories (see [Supported Repositories](#supported-repositories)). Example:

```env
GITHUB_ISSUE_URL=https://github.com/spf13/cobra/issues/2194
```

**Tips for choosing a good issue:**
- Pick a small, well-described bug (not a feature request)
- Issues with stack traces or reproduction steps work best
- Avoid issues marked `needs-discussion` or `breaking-change`

---

## Running the Engine

Make sure your virtual environment is active (`(venv)` in your prompt), then:

```bash
python src/main.py
```

### What you will see

```
======================================================================
🛡️  THE SENTINEL ENGINE: AUTOMATED AGENTIC PIPELINE PLATFORM
======================================================================
👤 Authenticated as: your-github-username
🍴 Forking spf13/cobra → your-github-username/cobra ...
✅ Fork created: https://github.com/your-github-username/cobra
⏳ Waiting for fork to initialise on GitHub...
✅ Fork is ready.
📥 Cloning fork: https://github.com/your-github-username/cobra → .../cobra
✅ Fork cloned successfully.

====== STARTING SENTINEL ENGINE EXECUTION MATRIX ======

🔎 Parsing target endpoint: https://github.com/spf13/cobra/issues/2194
🌐 Fetching runtime data from GitHub REST API for Issue #2194...
🤖 Analyzing issue with Gemini 2.5 Pro (structured triage)...
🎯 Target Acquired: Closes #2193

📦 Checking index telemetry for execution context: cobra [ad460ea8]
🔍 Index Cache Miss. Commencing complete code structural mapping...
✅ Code structural index sync completed. Registered 312 active code symbols.

🤖 Generating multi-track fix blueprints with Gemini 2.5 Pro...
📊 Blueprint locked. Track ALPHA: 'Inherit Context from Parent' | Track BETA: 'Propagate via Execution Loop'

🏎️  Deploying Cycle 1 tracks simultaneously across independent worktrees...
🌲 [Worktree] Creating isolated branch 'sentinel/track_alpha'...
🌲 [Worktree] Creating isolated branch 'sentinel/track_beta'...
📝 [TRACK_ALPHA] Applying patch at command.go...
📝 [TRACK_BETA] Applying patch at command.go...
⚡ [Matrix:TRACK_ALPHA] Launching parallel Go check suite execution...
⚡ [Matrix:TRACK_BETA] Launching parallel Go check suite execution...
  ✅ Branch 'sentinel/track_alpha' saved to main repo — ready to push.

🏁 --- CONCURRENT CONFLICT EVALUATION RUNTIME METRICS ---
Result Vector -> Track: TRACK_ALPHA | Status: 🟩 PASSED ALL VERIFICATIONS
Result Vector -> Track: TRACK_BETA | Status: 🟩 PASSED ALL VERIFICATIONS

🏆 Winning Branch Verified: sentinel/track_alpha
📝 Generating pull request title and body with Gemini 2.5 Pro...

======================================================================
📋  GENERATED PULL REQUEST
======================================================================
🏷️  TITLE: fix(cobra): propagate parent context to child commands
🌿  BRANCH: sentinel/track_alpha
📝  BODY: ...
======================================================================

🚀 Pushing branch 'sentinel/track_alpha' to your-github-username/cobra ...
✅ Branch pushed successfully.
📬 Opening PR: your-username:sentinel/track_alpha → spf13/cobra:main
✅ PR opened: https://github.com/spf13/cobra/pull/XXXX

======================================================================
🏁  SENTINEL ENGINE — RUN COMPLETE
======================================================================

  📁  Upstream repo  : spf13/cobra
  🍴  Your fork      : your-github-username/cobra
  🔖  Issue fixed    : #2194  →  https://github.com/spf13/cobra/issues/2194
  🌿  Branch         : sentinel/track_alpha
  📄  File patched   : command.go
  🏷️   PR title       : fix(cobra): propagate parent context to child commands

  ✅  Pull request opened successfully!
  🔗  https://github.com/spf13/cobra/pull/XXXX

  ── Inspect the patch locally ─────────────────────────────
  $ cd .../cobra
  $ git log sentinel/track_alpha --oneline -3
  $ git diff HEAD~1 HEAD

======================================================================
```

### Offline / Development Mode

If you want to test the engine without making real GitHub API calls or forking actual repos, set:

```env
SENTINEL_USE_MOCK_REPO=true
```

This seeds a local minimal Go repository with a known bug (division by zero without a zero-check) and runs the full pipeline against it. Useful for verifying the engine works before spending API quota.

---

## Understanding the Output

### The two tracks (ALPHA and BETA)

The engine always generates and tests two fix hypotheses in parallel. This is intentional — different engineers often have different valid approaches to the same bug. By racing two strategies, the engine:
- Has a higher chance of at least one passing tests on the first cycle
- Can compare approaches and select the one that passes
- Uses the ALPHA track's result if both pass (ALPHA is always preferred)

### The Self-Healing Loop

If both tracks fail, you will see:
```
🚨 Cycle 1 Failed. Intercepting diagnostic logs for Self-Healing loop...
🏎️  Deploying Cycle 2 (Healed Track Run) concurrently...
```

This means Gemini's first attempt at the code had issues (syntax errors, logic mistakes, type mismatches). The engine captures the exact `go vet` or `go test` error output and feeds it back to Gemini 2.5 Flash with the previous attempt, asking for a targeted correction. Cycle 2 is another parallel run with this corrected code.

### If all tracks fail

```
❌ System Regression: All parallel self-healing tracks exhausted.
```

This means even after self-healing, no valid fix was found. This typically happens with complex bugs that require deeper architectural changes than a surgical line-range patch can address. In this case, choose a simpler, more isolated issue and try again.

### The SQLite cache

After the first run, you'll see a `.cache/` directory appear with `state_cache.db`. On subsequent runs against the same repo at the same Git commit:
```
⚡ Index Cache Hit! Codebase map loaded instantly from local storage.
```
This makes re-runs significantly faster since the Go AST parsing step is skipped.

---

## Cost Per Run

Each full run makes approximately **5 Gemini API calls**:

| Call | Model | Purpose |
|------|-------|---------|
| 1 | gemini-2.5-pro | Issue triage → structured JSON |
| 2 | gemini-2.5-pro | Fix planning → 2 hypotheses |
| 3 | gemini-2.5-flash | Code generation for TRACK_ALPHA |
| 4 | gemini-2.5-flash | Code generation for TRACK_BETA |
| 5 | gemini-2.5-pro | PR title + body generation |

If Cycle 2 (Self-Healing) triggers, 2 additional Flash calls are made (one per track).

**Estimated cost per run: ~₹25–35 (~$0.30–0.40 USD)**

The Pro calls dominate cost since triage and planning involve large prompts with full issue context and symbol data. The Flash calls for code generation are fast and cheap.

> The free tier of the Gemini API (Google AI Studio) includes enough quota for several test runs. For production use, billing must be enabled on your Google Cloud project.

---

## Design Decisions

### Why Git Worktrees instead of Docker?

Docker containers take 2–10 seconds to spin up, require the Docker daemon, and add significant complexity. Git Worktrees create an isolated directory in under 50ms with zero extra tooling. Since we're testing Go code (which compiles natively), native Worktrees are the right tool.

### Why SQLite instead of a vector database?

Vector databases (Pinecone, Chroma, etc.) do semantic similarity search. For this use case, we don't need semantic search — we need exact file paths and line numbers. A lexical Go AST parser gives us precise symbol boundaries that a vector embedding cannot. SQLite is a single file, has zero network dependencies, and is instant on cache hit.

### Why Pydantic schemas for all LLM outputs?

Free-form LLM text output is unpredictable — the model might wrap JSON in markdown, add explanations, or change field names. By enforcing a Pydantic schema via the Gemini API's `response_schema` parameter, the output is guaranteed to have the expected structure. This eliminates an entire class of parsing errors.

### Why two fix hypotheses in parallel?

A single hypothesis creates a binary outcome: it works or it doesn't. Two parallel hypotheses mean that even if the first strategy is wrong, the second may succeed — doubling the first-pass success rate with only a marginal increase in cost (Flash calls are cheap).

### Why gemini-2.5-pro for planning and gemini-2.5-flash for code generation?

Planning requires deep multi-step reasoning: parse the bug report, correlate it with symbol data, and produce precise line-number targets. This is exactly what Pro is built for. Code generation is a targeted, constrained task with a small context window — Flash handles it faster and cheaper.

---

## Supported Repositories

The engine is designed for and tested against these repositories:

| Repository | URL | Notes |
|-----------|-----|-------|
| `spf13/cobra` | https://github.com/spf13/cobra | CLI framework |
| `gin-gonic/gin` | https://github.com/gin-gonic/gin | HTTP web framework |
| `go-playground/validator` | https://github.com/go-playground/validator | Struct validation |
| `golangci/golangci-lint` | https://github.com/golangci/golangci-lint | Linter runner |

Any Go repository will technically work since the AST indexer uses raw regex parsing, not import-specific tooling. However, the four repositories above are the ones the engine was built and validated against.

---

## Troubleshooting

### `❌ Missing system dependencies: go`
Go is not in your PATH. Install Go from https://go.dev/dl/ and restart your terminal.

### `❌ Clone failed: Repository not found`
Check that your GitHub token has `repo` scope. Go to https://github.com/settings/tokens and verify.

### `❌ Parsing Failure: Model output could not be parsed`
Gemini returned malformed JSON. This is rare with the Pydantic schema enforcement. Re-run the engine — it typically resolves on retry.

### `⚠️ git pull returned non-zero (local changes present?)`
Your local fork clone has uncommitted changes from a previous run. This is non-fatal — the engine continues with the existing state. To clean it up manually:
```bash
cd cobra  # or whichever repo
git checkout .
git clean -fd
```

### `❌ Failed to open PR: Validation Failed`
GitHub rejected the PR. Common causes:
- A PR for this branch already exists (check your fork on GitHub)
- The branch name contains characters GitHub doesn't accept

### The engine seems to hang after "Waiting for fork to initialise"
GitHub occasionally takes longer than 60 seconds for large repos. The engine will print a warning and proceed anyway. If the clone then fails, wait 30 seconds and re-run.

### SQLite cache is stale after switching issues
The cache is keyed by `(repo_name, git_commit_hash)`. If you switch to a different issue on the same repo without pulling new commits, the same cache is reused — this is correct behaviour. If you've pulled new commits, the cache auto-refreshes.

---

*Built with Python & Google Gemini. Zero extra dependencies. One command to go from issue URL to Pull Request.*