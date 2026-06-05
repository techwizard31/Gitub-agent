import os
import shutil
import subprocess

class WorktreeManager:
    def __init__(self, base_repo_path: str):
        """
        Manages isolated workspace duplication.
        base_repo_path: The main directory of the target Go repository (e.g., './cobra')
        """
        self.base_repo = os.path.abspath(base_repo_path)
        self.worktrees_dir = os.path.abspath(os.path.join(self.base_repo, "..", "worktrees"))
        os.makedirs(self.worktrees_dir, exist_ok=True)

    def create_hypothesis_worktree(self, hypothesis_id: str, branch_name: str) -> str:
        """
        Spawns an independent physical folder mapped to a fresh Git feature branch.
        Forcefully evicts stale branch collisions to ensure multi-cycle idempotency.
        """
        target_path = os.path.normpath(os.path.join(self.worktrees_dir, f"wt_{hypothesis_id}"))
        
        # 1. Clean up any existing folder path locks from previous interrupted runs
        if os.path.exists(target_path):
            self.cleanup_worktree(hypothesis_id)

        # 2. DEFENSIVE EVICTION: Forcefully delete any stale local branch pointers
        # This prevents Cycle 2 from crashing on branch name collisions from Cycle 1
        subprocess.run(
            ["git", "branch", "-D", branch_name], 
            cwd=self.base_repo, 
            capture_output=True, 
            text=True
        )

        print(f"🌲 [Worktree] Creating isolated branch '{branch_name}' inside: {os.path.basename(target_path)}")
        
        try:
            # 3. Create a fresh worktree branch cleanly
            cmd = ["git", "worktree", "add", target_path, "-b", branch_name]
            subprocess.run(cmd, cwd=self.base_repo, capture_output=True, text=True, check=True)
            return target_path
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to initialize Git Worktree: {e.stderr.strip()}")

    def cleanup_worktree(self, hypothesis_id: str):
        """Force prunes git metadata maps and purges local storage footprints completely."""
        target_path = os.path.normpath(os.path.join(self.worktrees_dir, f"wt_{hypothesis_id}"))
        
        try:
            # Inform git to detach and unregister the tracking workspace
            subprocess.run(["git", "worktree", "remove", "--force", target_path], 
                           cwd=self.base_repo, capture_output=True, text=True)
            
            # Purge physical directory footprints on the filesystem
            if os.path.exists(target_path):
                shutil.rmtree(target_path, ignore_errors=True)
            
            # Force garbage collect detached worktree metadata nodes
            subprocess.run(["git", "worktree", "prune"], cwd=self.base_repo, capture_output=True)
        except Exception as e:
            print(f"⚠️ Non-critical cleanup exception handled: {e}")