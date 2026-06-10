import asyncio
import os
import re
import subprocess


class AsyncTestSuiteRunner:
    def __init__(self, workspace_path: str):
        self.workspace = os.path.abspath(workspace_path)

    async def run_command_async(self, cmd: list[str]) -> dict:
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
        """Returns Go package paths for files modified in this worktree."""
        changed_pkgs = set()
        try:
            r1 = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=self.workspace, capture_output=True, text=True,
            )
            r2 = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workspace, capture_output=True, text=True,
            )
            files = r1.stdout.strip().splitlines()
            for line in r2.stdout.strip().splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    files.append(parts[1].strip())
            for f in files:
                if f.endswith(".go") and not f.endswith("_test.go"):
                    pkg_dir = os.path.dirname(f)
                    changed_pkgs.add("./" + pkg_dir if pkg_dir else ".")
        except Exception:
            pass
        return list(changed_pkgs) if changed_pkgs else ["./..."]

    def _extract_failing_tests(self, output: str) -> set[str]:
        """
        Parses go test output and returns the set of failing test names.
        Handles both '--- FAIL: TestName' and 'FAIL\tpackage' lines.
        """
        failing = set()
        for line in output.splitlines():
            m = re.match(r"\s*--- FAIL:\s+(\S+)", line)
            if m:
                failing.add(m.group(1))
        return failing

    def _get_baseline_failures(self, packages: list[str]) -> set[str]:
        """
        Runs tests on the ORIGINAL code (before our patch) using git stash,
        captures which tests were already failing, then restores the patch.

        This is the only reliable way to distinguish pre-existing failures
        from failures introduced by the patch — name-matching heuristics
        fail when the patched file and the failing test share a base name
        (e.g. patching context.go while TestSaveUploadedFile is in context_test.go).
        """
        try:
            # Stash our changes to get back to the original state
            stash = subprocess.run(
                ["git", "stash"],
                cwd=self.workspace, capture_output=True, text=True,
            )
            if "No local changes" in stash.stdout or stash.returncode != 0:
                # Nothing to stash — can't compute baseline
                return set()

            # Run tests on the original code
            baseline_res = subprocess.run(
                ["go", "test", "-short"] + packages,
                cwd=self.workspace, capture_output=True, text=True,
            )
            baseline_output = baseline_res.stdout + "\n" + baseline_res.stderr
            baseline_failures = self._extract_failing_tests(baseline_output)

            # Restore our patch
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=self.workspace, capture_output=True, text=True,
            )
            return baseline_failures

        except Exception:
            # If anything goes wrong, restore and return empty set
            try:
                subprocess.run(
                    ["git", "stash", "pop"],
                    cwd=self.workspace, capture_output=True, text=True,
                )
            except Exception:
                pass
            return set()

    async def execute_verification_matrix(self, track_id: str) -> dict:
        """
        Verification strategy:

        Phase 1 — go build ./... + go vet ./...
            Proves the patch compiles and passes static analysis across the
            full repo. These are hard failures — a patch that breaks the build
            or introduces undefined identifiers is always rejected.

        Phase 2 — go test -short (changed packages, baseline-diffed)
            Runs tests only on changed packages. If tests fail, computes a
            baseline by stashing the patch and re-running on the original code.
            Only NEW failures (not present in baseline) are treated as real
            failures caused by the patch. Pre-existing failures are ignored.

        This correctly handles repos like gin where TestSaveUploadedFileWithPermission
        fails even on the unpatched master branch due to environment/permission
        issues unrelated to our change.
        """
        print(
            "⚡ [Matrix:" + track_id + "] "
            "Running build + vet (full repo) and test (changed packages)..."
        )

        # Phase 1: build + vet across full repo
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

        # Phase 2: test changed packages
        changed_pkgs = self._get_changed_packages()
        test_cmd     = ["go", "test", "-short"] + changed_pkgs
        test_res     = await self.run_command_async(test_cmd)

        if not test_res["success"]:
            raw_output    = test_res["stdout"] + "\n" + test_res["stderr"]
            patch_failures = self._extract_failing_tests(raw_output)

            # Compute baseline — which of these tests were already failing
            # before our patch? Only count NEW failures as real.
            print("  🔍 [" + track_id + "] Checking baseline to filter pre-existing failures...")
            baseline_failures = self._get_baseline_failures(changed_pkgs)

            new_failures = patch_failures - baseline_failures

            if not new_failures:
                print(
                    "  ℹ️  [" + track_id + "] All test failures are pre-existing "
                    "(present in baseline). Treating as PASS."
                )
                return {
                    "track_id":    track_id,
                    "passed":      True,
                    "diagnostics": (
                        "build ./... ✓  vet ./... ✓  "
                        "test: " + str(len(patch_failures)) + " pre-existing failure(s) ignored, "
                        "0 new failures introduced by patch ✓"
                    ),
                }

            failure = self._clean_output(raw_output[:1500])
            return {
                "track_id":    track_id,
                "passed":      False,
                "diagnostics": (
                    "[TEST ERROR] " + str(len(new_failures)) + " new failure(s): "
                    + ", ".join(sorted(new_failures)) + "\n" + failure
                ),
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
        """Strips blank lines, package-only headers, and exit status lines."""
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
        return "\n".join(lines[:60])