import json
import time
import subprocess
import urllib.request
import urllib.error
import os


class GitHubClient:
    """
    Wraps all GitHub REST API calls needed for the fork-based contribution flow:

      1. get_authenticated_user()  → who owns the token
      2. fork_repository()         → POST /repos/{owner}/{repo}/forks
      3. wait_for_fork_ready()     → poll until the fork's default branch exists
      4. clone_fork()              → git clone https://<token>@github.com/<you>/<repo>
      5. push_branch()             → git push origin <branch>
      6. open_pull_request()       → POST /repos/{owner}/{repo}/pulls

    All network calls use only stdlib urllib — zero extra dependencies.
    The token is embedded in clone URLs so git never prompts for credentials.
    """

    API_BASE = "https://api.github.com"

    def __init__(self, token: str):
        self.token = token
        self._username: str | None = None  # cached after first call

    # ── Internal HTTP helpers ──────────────────────────────────────────────────

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Makes an authenticated GitHub API call. Raises RuntimeError on failure."""
        url = self.API_BASE + path
        data = json.dumps(body).encode() if body else None

        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "token " + self.token)
        req.add_header("Accept", "application/vnd.github.v3+json")
        req.add_header("User-Agent", "Sentinel-Agent-Engine")
        if data:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="ignore")
            raise RuntimeError(
                "GitHub API " + method + " " + path
                + " → HTTP " + str(e.code) + ": " + body_text
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_authenticated_user(self) -> str:
        """
        Returns the GitHub username that owns the token.
        Cached after the first call — only one network round-trip ever made.
        """
        if self._username:
            return self._username
        data = self._request("GET", "/user")
        self._username = data["login"]
        return self._username

    def fork_repository(self, owner: str, repo: str) -> dict:
        """
        Forks owner/repo into the authenticated user's account.
        If the fork already exists GitHub returns the existing fork — idempotent.

        Returns the fork metadata dict (contains clone_url, default_branch, etc).
        """
        username = self.get_authenticated_user()
        print("🍴 Forking " + owner + "/" + repo + " → " + username + "/" + repo + " ...")

        fork_data = self._request("POST", "/repos/" + owner + "/" + repo + "/forks")

        print("✅ Fork created: " + fork_data["html_url"])
        return fork_data

    def wait_for_fork_ready(self, username: str, repo: str, timeout: int = 60) -> bool:
        """
        GitHub forks are created asynchronously. This polls the fork's default
        branch ref until it exists, confirming the fork is fully initialised.

        Retries every 3 seconds up to `timeout` seconds. Returns True on success.
        """
        print("⏳ Waiting for fork to initialise on GitHub...")
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                data = self._request("GET", "/repos/" + username + "/" + repo)
                # GitHub forks are ready once they have a default_branch populated.
                # The `empty_repo` field does NOT exist in the API response — checking
                # for default_branch presence alone is the correct signal.
                if data.get("default_branch"):
                    print("✅ Fork is ready.")
                    return True
            except RuntimeError:
                pass  # 404 while fork is still being created — keep polling

            time.sleep(3)

        print("⚠️  Fork did not become ready within " + str(timeout) + "s. Proceeding anyway.")
        return False

    def clone_fork(self, username: str, repo: str, target_path: str) -> bool:
        """
        Clones the user's fork locally using a token-authenticated HTTPS URL.
        The token is embedded so git never prompts for a password.

        If the directory already looks like the correct fork, skips cloning
        and pulls instead (idempotent re-runs).
        """
        # Authenticated URL: git never prompts for credentials
        clone_url = (
            "https://" + self.token + "@github.com/" + username + "/" + repo + ".git"
        )
        display_url = "https://github.com/" + username + "/" + repo  # safe to print

        if os.path.isdir(os.path.join(target_path, ".git")):
            # Check if it's already pointing at our fork
            remote = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=target_path, capture_output=True, text=True
            )
            if username in remote.stdout:
                print("📂 Fork already cloned at '" + target_path + "'. Pulling latest...")
                subprocess.run(["git", "pull", "--ff-only"], cwd=target_path, capture_output=True)
                return True
            else:
                print("⚠️  Directory exists but points to a different remote. Re-cloning...")
                import shutil
                shutil.rmtree(target_path)

        print("📥 Cloning fork: " + display_url + " → " + target_path)
        os.makedirs(target_path, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--depth=1", clone_url, target_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print("❌ Clone failed: " + result.stderr.strip())
            return False

        # Also add the upstream remote so diffs against original repo work cleanly
        upstream_url = "https://github.com/" + username + "/" + repo + ".git"
        subprocess.run(
            ["git", "remote", "add", "upstream",
             "https://github.com/" + repo + ".git"],
            cwd=target_path, capture_output=True
        )

        print("✅ Fork cloned successfully.")
        return True

    def push_branch(self, repo_path: str, branch: str, token: str, username: str, repo: str) -> bool:
        """
        Pushes a local branch to the user's fork on GitHub.
        Uses the token-authenticated remote URL so no credential prompts occur.
        """
        push_url = (
            "https://" + token + "@github.com/" + username + "/" + repo + ".git"
        )
        print("🚀 Pushing branch '" + branch + "' to " + username + "/" + repo + " ...")
        result = subprocess.run(
            ["git", "push", push_url, branch + ":" + branch, "--force"],
            cwd=repo_path, capture_output=True, text=True
        )
        if result.returncode == 0:
            print("✅ Branch pushed successfully.")
            return True
        else:
            print("❌ Push failed: " + result.stderr.strip())
            return False

    def open_pull_request(
        self,
        upstream_owner: str,
        repo: str,
        username: str,
        branch: str,
        title: str,
        body: str,
        issue_number: int,
    ) -> dict | None:
        """
        Opens a PR from username/repo:branch → upstream_owner/repo:main (or master).

        Returns the PR data dict on success (contains html_url), or None on failure.
        """
        # Detect the upstream default branch
        try:
            upstream_data = self._request("GET", "/repos/" + upstream_owner + "/" + repo)
            base_branch = upstream_data.get("default_branch", "main")
        except RuntimeError:
            base_branch = "main"

        pr_body = {
            "title": title,
            "body": body,
            "head": username + ":" + branch,   # your fork's branch
            "base": base_branch,                # upstream's main/master
            "maintainer_can_modify": True,
        }

        print("📬 Opening PR: " + username + ":" + branch + " → " + upstream_owner + "/" + repo + ":" + base_branch)

        try:
            pr_data = self._request(
                "POST",
                "/repos/" + upstream_owner + "/" + repo + "/pulls",
                body=pr_body,
            )
            print("✅ PR opened: " + pr_data["html_url"])
            return pr_data
        except RuntimeError as e:
            # A common case: PR already exists for this branch
            if "already exists" in str(e) or "A pull request already exists" in str(e):
                print("⚠️  A PR for this branch already exists on GitHub.")
            else:
                print("❌ Failed to open PR: " + str(e))
            return None