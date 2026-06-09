import asyncio
import os
import re


class AsyncTestSuiteRunner:
    def __init__(self, workspace_path: str):
        self.workspace = os.path.abspath(workspace_path)

    async def run_command_async(self, cmd: list[str]) -> dict:
        """Executes a command asynchronously, returns structured result."""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            return {
                "command":     " ".join(cmd),
                "return_code": process.returncode,
                "stdout":      stdout.decode(errors="ignore").strip(),
                "stderr":      stderr.decode(errors="ignore").strip(),
                "success":     process.returncode == 0,
            }
        except Exception as e:
            return {
                "command":     " ".join(cmd),
                "return_code": -1,
                "stdout":      "",
                "stderr":      str(e),
                "success":     False,
            }

    def _get_changed_packages(self) -> list[str]:
        """
        Returns the unique set of Go package paths that have modified .go files
        in the workspace, relative to the module root.

        We run tests only on changed packages rather than ./... for two reasons:
        1. Many open-source repos have complex test setups, env requirements, or
           flaky integration tests that are unrelated to our patch.
        2. go test ./... on a large repo like gin takes 30-60s and may fail for
           reasons that have nothing to do with correctness of the patch.

        Running only the affected package(s) is faster, more focused, and avoids
        false negatives from pre-existing test failures in unrelated packages.
        """
        import subprocess
        changed_pkgs = set()
        try:
            # Get list of modified files relative to HEAD
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
            )
            modified = result.stdout.strip().splitlines()

            # Also catch untracked/staged changes not yet committed
            result2 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
            )
            for line in result2.stdout.strip().splitlines():
                # porcelain format: "XY filename"
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    modified.append(parts[1].strip())

            for f in modified:
                if f.endswith(".go") and not f.endswith("_test.go"):
                    pkg_dir = os.path.dirname(f)
                    # Convert to Go package path format
                    pkg_path = "./" + pkg_dir if pkg_dir else "."
                    changed_pkgs.add(pkg_path)

        except Exception:
            pass

        return list(changed_pkgs) if changed_pkgs else ["./..."]

    async def execute_verification_matrix(self, track_id: str) -> dict:
        """
        Runs verification in two phases:

        Phase 1 — go build ./... + go vet ./...
            Both run across the entire repo. build proves compilation succeeds.
            vet catches undefined identifiers, type errors, and bad signatures.
            These are always run on ./... because compile errors are global.

        Phase 2 — go test -short on CHANGED PACKAGES ONLY
            Tests run only on the packages that contain patched files.
            This avoids false negatives from pre-existing failures in unrelated
            packages, and avoids needing the full repo test environment to be set up.
            -short skips integration/slow tests that need external services.

        A patch is considered passing if build + vet succeed AND tests pass
        on the changed packages. This is the same bar a human reviewer applies
        before submitting a PR.
        """
        print(
            "⚡ [Matrix:" + track_id + "] "
            "Running build + vet (full repo) and test (changed packages)..."
        )

        # Phase 1: build + vet concurrently across full repo
        build_task = self.run_command_async(["go", "build", "./..."])
        vet_task   = self.run_command_async(["go", "vet",   "./..."])
        build_res, vet_res = await asyncio.gather(build_task, vet_task)

        if not build_res["success"]:
            failure = self._clean_output(build_res["stderr"] or build_res["stdout"])
            return {
                "track_id":    track_id,
                "passed":      False,
                "diagnostics": "[BUILD ERROR]\n" + failure,
            }

        if not vet_res["success"]:
            failure = self._clean_output(vet_res["stderr"] or vet_res["stdout"])
            return {
                "track_id":    track_id,
                "passed":      False,
                "diagnostics": "[VET ERROR]\n" + failure,
            }

        # Phase 2: test only changed packages
        changed_pkgs = self._get_changed_packages()
        test_cmd     = ["go", "test", "-short"] + changed_pkgs
        test_res     = await self.run_command_async(test_cmd)

        if not test_res["success"]:
            failure = self._clean_output(test_res["stdout"][:1500])
            return {
                "track_id":    track_id,
                "passed":      False,
                "diagnostics": "[TEST ERROR] packages=" + str(changed_pkgs) + "\n" + failure,
            }

        return {
            "track_id":    track_id,
            "passed":      True,
            "diagnostics": (
                "All verification checkpoints passed. "
                "build ./... ✓  vet ./... ✓  test " + str(changed_pkgs) + " ✓"
            ),
        }

    def _clean_output(self, raw: str) -> str:
        """
        Strips noisy lines from go tool output that add length without information:
        - Blank lines
        - Lines that are just a package path with no error (e.g. '# github.com/...')
        - 'exit status N' lines
        """
        lines = []
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("# ") and ":" not in stripped:
                continue
            if re.match(r"^exit status \d+$", stripped):
                continue
            lines.append(line)
        return "\n".join(lines[:60])   # hard cap: never feed more than 60 lines upstream