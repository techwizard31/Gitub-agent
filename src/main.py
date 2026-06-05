import os
import sys
import asyncio
import subprocess
from dotenv import load_dotenv

# Ensure the src folder is in the system path to prevent local module resolution errors
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from pipeline import SentinelPipeline

def check_system_environment() -> bool:
    """Verifies core CLI compilation tools exist on the host Windows machine."""
    try:
        subprocess.check_output(["go", "version"], stderr=subprocess.STDOUT)
        subprocess.check_output(["git", "--version"], stderr=subprocess.STDOUT)
        return True
    except FileNotFoundError:
        print("❌ Core system dependency missing. Ensure both 'go' and 'git' are added to your Windows Environment PATH variables.")
        return False

def setup_mock_go_repo(path: str):
    """Generates a dummy git-initialized package to run verification races locally."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Sentinel"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "agent@sentinel.ai"], cwd=path, capture_output=True)
    subprocess.run(["go", "mod", "init", "github.com/spf13/cobra"], cwd=path, capture_output=True)
    
    # Inject standard code files containing a basic logical bug (missing zero-division check)
    code_content = (
        "package cobra\n"
        "import \"errors\"\n\n"
        "func ExecuteCommand(val int) (int, error) {\n"
        "\t// Bug: Missing boundary checks!\n"
        "\treturn 100 / val, nil\n"
        "}\n"
    )
    with open(os.path.join(path, "cobra.go"), "w", encoding="utf-8") as f:
        f.write(code_content)
        
    test_content = (
        "package cobra\n"
        "import \"testing\"\n\n"
        "func TestExecuteCommand(t *testing.T) {\n"
        "\tres, err := ExecuteCommand(2)\n"
        "\tif err != nil || res != 50 { t.Error(\"Fail\") }\n"
        "\t_, err = ExecuteCommand(0)\n"
        "\tif err == nil { t.Error(\"Expected division error\") }\n"
        "}\n"
    )
    with open(os.path.join(path, "cobra_test.go"), "w", encoding="utf-8") as f:
        f.write(test_content)
    
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, capture_output=True)

def main():
    print("=" * 70)
    print("🛡️  THE SENTINEL ENGINE: AUTOMATED AGENTIC PIPELINE PLATFORM")
    print("=" * 70)
    
    if not check_system_environment():
        sys.exit(1)
        
    load_dotenv()
    gemini_key = os.getenv("GEMINI_API_KEY")
    github_token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
    issue_url = os.getenv("GITHUB_ISSUE_URL")

    if not all([gemini_key, github_token, issue_url]):
        print("❌ Error: Missing credentials inside your local .env configuration.")
        print("💡 Solution: Check .env.example template specifications.")
        sys.exit(1)

    # If a local checkout path doesn't exist yet, we seed the mock environment 
    mock_repo = "./cobra"
    if not os.path.exists(mock_repo):
        print(f"💡 Target clone path '{mock_repo}' not found. Seeding a local sandbox runtime model...")
        setup_mock_go_repo(mock_repo)

    # Instantiate and trigger the pipeline state runner loop
    pipeline = SentinelPipeline(
        gemini_key=gemini_key,
        github_token=github_token,
        local_repo_path=mock_repo,
        repo_name="cobra"
    )

    # Execute the asynchronous orchestration graph engine safely
    asyncio.run(pipeline.run_pipeline(issue_url))

if __name__ == "__main__":
    main()