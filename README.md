🛡️ The Sentinel Engine

A Deterministic, Highly-Parallel AI Coding Agent for Open-Source Software

The Sentinel Engine is a high-performance autonomous software engineering framework designed to resolve complex GitHub issues in Go repositories (e.g., Cobra, Gin).

Unlike traditional "terminal-wrapper" SWE agents that suffer from context bloat and destructive shell commands, Sentinel utilizes a strict, zero-dependency Python state machine. It leverages local SQLite AST caching, Git Worktrees, and the Google Gemini 1.5 API to test multiple architectural hypotheses in parallel.

🚀 Core Architecture & Features

The platform is divided into five token-optimized, deeply observable modules:

Intelligent Ingestion (src/ingestion)

Dynamically fetches issue telemetry directly from the GitHub REST API.

Utilizes Gemini 1.5 Pro to parse problem descriptions into structured JSON blueprints.

Zero-Dependency AST Cache (src/indexing)

Maps Go package symbols, structs, and function boundaries into a local SQLite database (.cache/state_cache.db).

Eliminates the need for bulky vector databases by resolving precise file/line coordinates instantly.

Safe Agent-Computer Interface (src/aci)

Constrains LLM file access to strict sliding-window line reading.

Enforces a defensive Pre-Lint Syntax Guardrail (gofmt) that intercepts and rejects structural LLM hallucinations before tests even run.

Concurrent Worktree Engine (src/verification)

Replaces slow, resource-heavy Docker containers with native Git Worktrees.

Spawns completely isolated physical directory tracks in under 50ms.

Executes go test and go vet matrices in parallel using Python's native asyncio.

Context-Aware Self-Healing (src/pipeline.py)

If a parallel track fails validation, the orchestrator intercepts the compiler trace.

Feeds the exact error and the previous code attempt back into a sub-second Gemini 1.5 Flash loop for automatic, targeted syntax repair.

🛠️ System Prerequisites

To run the concurrent verification pipelines locally, your host machine must have the following installed and available in the system PATH:

Python 3.11+

Git CLI

Go Compiler (1.20+)

Windows Quick Install:

winget install --id Git.Git -e --silent
winget install --id Python.Python.3.12 -e --silent
winget install --id GoLang.Go -e --silent


(Restart your terminal after installation to refresh environment variables)

📦 Installation & Setup

1. Clone the repository and initialize the workspace:

git clone https://github.com/yourusername/sentinel-agent.git
cd sentinel-agent


2. Set up an isolated Python Virtual Environment:

# Windows
python -m venv venv
.\venv\Scripts\Activate.ps1

# Linux / macOS
python3 -m venv venv
source venv/bin/activate


3. Install dependencies:

pip install -r requirements.txt


⚙️ Configuration

Create a .env file in the root directory by duplicating the provided .env.example:

# .env
GEMINI_API_KEY=your_google_gemini_api_key
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_your_github_classic_token
GITHUB_ISSUE_URL=https://github.com/spf13/cobra/issues/2194


🎯 Usage

To trigger the end-to-end execution matrix, simply run the master bootstrapper:

python src/main.py


The Execution Flow:

The engine reads the issue URL and generates two distinct fix strategies.

It clones/syncs the target repository locally.

It spawns TRACK_ALPHA and TRACK_BETA Git worktrees simultaneously.

Patches are applied and tested in parallel.

If both fail, Cycle 2 (Self-Healing) engages.

The winning branch is identified and verified for a Pull Request.

📂 Directory Structure

sentinel-agent/
├── .cache/                # SQLite local index tracking (Generated)
├── src/
│   ├── main.py            # Master pipeline bootstrapper
│   ├── pipeline.py        # Orchestration and self-healing state machine
│   ├── ingestion/         # GitHub API and triage parsing
│   ├── indexing/          # Lexical Go AST boundary parser
│   ├── aci/               # Guardrailed agent modification tools
│   └── verification/      # Git Worktree scaling and Async Test runners
├── worktrees/             # Isolated hypothesis branch directories (Dynamic)
├── .env.example
├── requirements.txt
└── README.md


🔒 Security & Sandboxing Note

The Sentinel Engine operates directly on the local filesystem using Git Worktrees. Ensure that any third-party repositories being tested by the agent do not contain malicious init scripts or compromised test files, as standard go test executions run with local user privileges.

Built with Python & Google GenAI.