import os
import sys
import asyncio
import subprocess
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from pipeline import SentinelPipeline
from github_client import GitHubClient


def check_system_environment() -> bool:
    missing = []
    for tool in ["go", "git"]:
        try:
            subprocess.check_output([tool, "version"], stderr=subprocess.STDOUT)
        except FileNotFoundError:
            missing.append(tool)
    if missing:
        print("❌ Missing system dependencies: " + ", ".join(missing))
        print("💡 Ensure both 'go' and 'git' are installed and available in your PATH.")
        return False
    return True


def parse_issue_url(issue_url: str) -> tuple[str, str, int]:
    """Extracts (owner, repo, issue_number) from a GitHub issue URL."""
    import re
    match = re.search(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)", issue_url)
    if not match:
        raise ValueError("Cannot parse owner/repo/issue from URL: " + issue_url)
    return match.group(1), match.group(2), int(match.group(3))


def setup_mock_go_repo(path: str):
    """Minimal mock repo for offline dev/testing (SENTINEL_USE_MOCK_REPO=true)."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Sentinel"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "agent@sentinel.ai"], cwd=path, capture_output=True)
    subprocess.run(["go", "mod", "init", "github.com/spf13/cobra"], cwd=path, capture_output=True)

    with open(os.path.join(path, "cobra.go"), "w") as f:
        f.write(
            'package cobra\nimport "errors"\n\n'
            'func ExecuteCommand(val int) (int, error) {\n'
            '\t// Bug: Missing boundary checks!\n'
            '\treturn 100 / val, nil\n}\n'
        )
    with open(os.path.join(path, "cobra_test.go"), "w") as f:
        f.write(
            'package cobra\nimport "testing"\n\n'
            'func TestExecuteCommand(t *testing.T) {\n'
            '\tres, err := ExecuteCommand(2)\n'
            '\tif err != nil || res != 50 { t.Error("Fail") }\n'
            '\t_, err = ExecuteCommand(0)\n'
            '\tif err == nil { t.Error("Expected division error") }\n}\n'
        )
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, capture_output=True)
    print("✅ Mock repository seeded at '" + path + "'")


def main():
    print("=" * 70)
    print("🛡️  THE SENTINEL ENGINE: AUTOMATED AGENTIC PIPELINE PLATFORM")
    print("=" * 70)

    if not check_system_environment():
        sys.exit(1)

    load_dotenv()
    gemini_key    = os.getenv("GEMINI_API_KEY")
    github_token  = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    issue_url     = os.getenv("GITHUB_ISSUE_URL")
    use_mock      = os.getenv("SENTINEL_USE_MOCK_REPO", "false").lower() == "true"

    if not all([gemini_key, github_token, issue_url]):
        print("❌ Error: Missing credentials in .env — check .env.example.")
        sys.exit(1)

    # ── Parse issue URL ───────────────────────────────────────────────────────
    try:
        upstream_owner, repo_name, issue_number = parse_issue_url(issue_url)
    except ValueError as e:
        print("❌ " + str(e))
        sys.exit(1)

    src_dir      = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(src_dir)
    local_path   = os.path.join(project_root, repo_name)

    # ── Mock mode (offline dev) ───────────────────────────────────────────────
    if use_mock:
        print("⚙️  SENTINEL_USE_MOCK_REPO=true — seeding local sandbox...")
        setup_mock_go_repo(local_path)
        fork_username = "mock-user"
    else:
        # ── Real flow: fork → clone fork ──────────────────────────────────────
        gh = GitHubClient(github_token)

        # 1. Who are we?
        fork_username = gh.get_authenticated_user()
        print("👤 Authenticated as: " + fork_username)

        # 2. Fork the upstream repo into our account
        gh.fork_repository(upstream_owner, repo_name)

        # 3. Wait for GitHub to finish creating the fork
        gh.wait_for_fork_ready(fork_username, repo_name)

        # 4. Clone our fork locally
        ok = gh.clone_fork(fork_username, repo_name, local_path)
        if not ok:
            print("❌ Could not clone fork. Cannot proceed.")
            sys.exit(1)

    # ── Run the Sentinel pipeline ─────────────────────────────────────────────
    pipeline = SentinelPipeline(
        gemini_key=gemini_key,
        github_token=github_token,
        local_repo_path=local_path,
        repo_name=repo_name,
        upstream_owner=upstream_owner,
        fork_username=fork_username,
    )

    asyncio.run(pipeline.run_pipeline(issue_url))


if __name__ == "__main__":
    main()