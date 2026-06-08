import os
import re
import json
import time
import asyncio
import subprocess
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from ingestion.triage import IssueTriageEngine
from ingestion.pr_generator import PRGenerator
from indexing.indexer import RepositoryIndexer
from aci.tools import AgentComputerInterface
from verification.worktree import WorktreeManager
from verification.tester import AsyncTestSuiteRunner
from github_client import GitHubClient


# ── Schema ────────────────────────────────────────────────────────────────────

class FilePatch(BaseModel):
    """
    A single atomic file change — one entry in a multi-file patch set.
    A hypothesis can contain as many FilePatch entries as the fix requires.
    """
    target_file: str = Field(
        description="Relative path of the file to modify (must exist in the repo)."
    )
    start_line: int = Field(
        description="First line of the region to operate on."
    )
    end_line: int = Field(
        description="Last line of the region to operate on."
    )
    patch_mode: str = Field(
        default="replace",
        description=(
            "'replace': overwrite lines start_line..end_line with new_code. "
            "Use for bug fixes that modify existing logic. "
            "'insert_after': insert new_code AFTER line end_line without removing anything. "
            "Use for enhancements: new functions, new map entries, new validator registrations."
        )
    )
    description: str = Field(
        description="One-line human-readable description of what this file change does."
    )
    new_code: str = Field(
        description=(
            "The Go source code to write. "
            "For 'replace': the complete replacement for start_line..end_line. "
            "For 'insert_after': only the new lines to insert — do not repeat existing lines."
        )
    )


class FixHypothesis(BaseModel):
    """
    One complete fix strategy, potentially spanning multiple files.
    All patches in the list are applied in order to the same worktree.
    """
    title: str = Field(
        description="Short descriptive title of this fix approach."
    )
    patches: list[FilePatch] = Field(
        description=(
            "Ordered list of file changes that together implement this fix. "
            "Include ALL files that need to change — e.g. both the function definition file "
            "AND the registration map file. There is no limit on the number of patches."
        )
    )


class StrategyBlueprint(BaseModel):
    hypotheses: list[FixHypothesis] = Field(
        description="Exactly 2 distinct fix strategies to evaluate concurrently."
    )


# ── Utilities ─────────────────────────────────────────────────────────────────

def clean_llm_code_output(raw_text: str) -> str:
    """Strips markdown fences, conflict markers, and diff markers from LLM output."""
    text = raw_text.strip()
    code_block_match = re.search(r"```(?:go)?\s*\n(.*?)```", text, re.DOTALL)
    if code_block_match:
        text = code_block_match.group(1).strip()
    text = re.compile(r"^(<{7}|={7}|>{7}).*$", re.MULTILINE).sub("", text).strip()
    text = re.compile(r"^[+-]{3}\s+.*$", re.MULTILINE).sub("", text).strip()
    return text


def extract_error_line(error_text: str) -> int | None:
    """Extracts first error line number from gofmt/go vet output."""
    match = re.search(r":(\d+):\d+:", error_text)
    return int(match.group(1)) if match else None


def make_branch_name(issue_number, track_id: str, cycle: int, run_ts: int) -> str:
    """
    Generates a globally unique branch name that encodes:
      - issue number  → different issues never share a branch
      - track id      → ALPHA vs BETA always distinct
      - cycle         → Cycle 1 vs Cycle 2 retry never clash
      - run_ts        → Unix timestamp at pipeline start, unique per re-run

    Pattern: sentinel/issue-{N}/{track}-c{cycle}-{ts}
    Example: sentinel/issue-1543/track-alpha-c1-1749123456

    Re-running the same issue produces a different timestamp → zero collision.
    """
    issue_slug = "issue-" + str(issue_number) + "/" if issue_number else ""
    track_slug = re.sub(r"[^a-zA-Z0-9]", "-", track_id.lower())
    return "sentinel/" + issue_slug + track_slug + "-c" + str(cycle) + "-" + str(run_ts)


def _build_file_inventory(repo_path: str) -> str:
    """
    Returns a compact list of all real .go source files in the repo with line counts.
    Gives the planner ground-truth paths so it never hallucinates file names.
    """
    entries = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("vendor", "testdata")]
        for fname in sorted(files):
            if not fname.endswith(".go") or fname.endswith("_test.go"):
                continue
            full = os.path.join(root, fname)
            rel  = os.path.relpath(full, repo_path).replace(os.sep, "/")
            try:
                with open(full, encoding="utf-8", errors="ignore") as f:
                    lc = sum(1 for _ in f)
                entries.append(rel + "  (" + str(lc) + " lines)")
            except OSError:
                pass
    if not entries:
        return "No Go source files found."
    if len(entries) > 40:
        entries = entries[:40] + ["... (truncated)"]
    return "\n".join(entries)


def _validate_and_correct_patches(patches: list[dict], repo_path: str) -> list[dict]:
    """
    For each patch, verify target_file exists in the repo.
    If not, attempt to find the closest real file by basename match.
    Drops patches that cannot be resolved.
    """
    good = []
    for p in patches:
        target = p.get("target_file", "")
        full   = os.path.join(repo_path, target.replace("/", os.sep))
        if os.path.isfile(full):
            good.append(p)
            continue
        # Try basename match
        base = os.path.basename(target)
        found = False
        for root, _, files in os.walk(repo_path):
            if base in files:
                real_rel = os.path.relpath(
                    os.path.join(root, base), repo_path
                ).replace(os.sep, "/")
                print("   ↳ Correcting hallucinated path '" + target
                      + "' → '" + real_rel + "'")
                p["target_file"] = real_rel
                good.append(p)
                found = True
                break
        if not found:
            print("   ↳ Dropping patch — file not found: '" + target + "'")
    return good


# ── Pipeline ──────────────────────────────────────────────────────────────────

class SentinelPipeline:
    def __init__(
        self,
        gemini_key: str,
        github_token: str,
        local_repo_path: str,
        repo_name: str,
        upstream_owner: str = "",
        fork_username: str = "",
    ):
        self.client         = genai.Client(api_key=gemini_key)
        self.triage_engine  = IssueTriageEngine(gemini_key, github_token)
        self.pr_generator   = PRGenerator(gemini_key)
        self.indexer        = RepositoryIndexer()
        self.github         = GitHubClient(github_token)
        self.github_token   = github_token
        self.repo_path      = os.path.abspath(local_repo_path)
        self.repo_name      = repo_name
        self.upstream_owner = upstream_owner
        self.fork_username  = fork_username
        self._issue_number: int | None = None
        self._run_ts: int = int(time.time())   # fixed at pipeline start, same across all tracks

    # ── Branch commit + transfer ──────────────────────────────────────────────

    def _commit_and_transfer_branch(
        self,
        wt_workspace: str,
        branch_name: str,
        hypothesis: FixHypothesis,
    ) -> bool:
        """
        Stages ALL modified files, commits, then transfers the branch to the
        main repo so it survives worktree cleanup and can be pushed to the fork.

        Transfer strategy:
          Primary:  git fetch <normalized_path> branch:branch
          Fallback: git branch -f <branch> <commit_hash>  (Windows compatibility)
        """
        # Build commit message listing every file that was patched
        files_changed = "\n".join(
            "  - " + p.target_file + " (" + p.description + ")"
            for p in hypothesis.patches
        )
        commit_msg = (
            "fix: " + hypothesis.title + "\n\n"
            "Applied by Sentinel Engine.\n"
            "Files changed:\n" + files_changed
        )

        add_res = subprocess.run(
            ["git", "add", "-A"], cwd=wt_workspace, capture_output=True, text=True
        )
        if add_res.returncode != 0:
            print("  ⚠️  git add -A failed: " + add_res.stderr.strip())
            return False

        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=wt_workspace, capture_output=True, text=True
        )
        if not staged.stdout.strip():
            print("  ⚠️  Nothing staged — patches produced no file changes.")
            return False

        print("  📄 Files staged: " + staged.stdout.strip().replace("\n", ", "))

        commit_res = subprocess.run(
            ["git", "-c", "user.name=Sentinel", "-c", "user.email=agent@sentinel.ai",
             "commit", "-m", commit_msg],
            cwd=wt_workspace, capture_output=True, text=True
        )
        if commit_res.returncode != 0:
            print("  ⚠️  git commit failed: " + commit_res.stderr.strip())
            return False

        print("  ✅ Patch committed on branch '" + branch_name + "'.")

        hash_res = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=wt_workspace, capture_output=True, text=True
        )
        if hash_res.returncode != 0:
            print("  ⚠️  Could not read commit hash.")
            return False
        commit_hash = hash_res.stdout.strip()

        # Primary transfer: git fetch with forward-slash path (Windows safe)
        wt_normalized = wt_workspace.replace(os.sep, "/")
        fetch_res = subprocess.run(
            ["git", "fetch", wt_normalized, branch_name + ":" + branch_name],
            cwd=self.repo_path, capture_output=True, text=True
        )
        if fetch_res.returncode == 0:
            print("  ✅ Branch '" + branch_name + "' transferred to main repo.")
            return True

        # Fallback: point branch directly at commit hash
        create_res = subprocess.run(
            ["git", "branch", "-f", branch_name, commit_hash],
            cwd=self.repo_path, capture_output=True, text=True
        )
        if create_res.returncode == 0:
            print("  ✅ Branch '" + branch_name + "' created via hash fallback.")
            return True

        print("  ❌ Branch transfer failed.")
        print("     fetch: " + fetch_res.stderr.strip())
        print("     branch -f: " + create_res.stderr.strip())
        return False

    # ── Self-healing prompt builder ───────────────────────────────────────────

    def _build_heal_prompt(
        self,
        patch: FilePatch,
        bad_code: str,
        error_log: str,
        aci: AgentComputerInterface,
        all_patches: list | None = None,
    ) -> str:
        """
        Builds a targeted repair prompt for a single failed FilePatch.

        Shows:
        - The broken code that was actually written
        - The exact gofmt / go vet / go test error
        - Wide context around the error line
        - A summary of ALL other patches in this hypothesis (cross-file context)
          so the LLM knows what function names / identifiers other patches are using

        The cross-file context is critical for errors like "undefined: isDNSLabel"
        where the function definition patch and the registration patch must use
        identical identifiers.
        """
        error_line = extract_error_line(error_log)

        if error_line:
            ctx_start = max(1, error_line - 10)
            ctx_end   = error_line + 10
            context   = aci.view_file_range(patch.target_file, ctx_start, ctx_end)
            ctx_label = (
                "FILE AROUND ERROR LINE " + str(error_line)
                + " (lines " + str(ctx_start) + "-" + str(ctx_end) + "):"
            )
        else:
            ctx_start = max(1, patch.start_line - 5)
            ctx_end   = patch.end_line + 5
            context   = aci.view_file_range(patch.target_file, ctx_start, ctx_end)
            ctx_label = (
                "FILE AT TARGET RANGE (lines "
                + str(ctx_start) + "-" + str(ctx_end) + "):"
            )

        if "SYNTAX FAIL" in error_log or "expected operand" in error_log or "gofmt" in error_log.lower():
            guidance = (
                "This is a Go SYNTAX error (caught by gofmt). "
                "Check for unmatched parentheses, missing commas in struct literals, "
                "or malformed function signatures. Count braces carefully."
            )
        else:
            guidance = (
                "This is a Go COMPILER or VET error. "
                "Check for undefined identifiers, wrong function signatures, "
                "type mismatches, or missing imports. "
                "If the error says 'undefined: X', ensure your code defines or imports X "
                "with the EXACT same name used in the other patches listed below."
            )

        mode_instruction = (
            "Insert the corrected code AFTER line " + str(patch.end_line) + ". "
            "Do NOT reproduce existing lines."
            if patch.patch_mode == "insert_after"
            else "Replace lines " + str(patch.start_line)
            + " to " + str(patch.end_line) + " entirely."
        )

        # Build cross-file context so the LLM knows what names other patches use
        cross_file_ctx = ""
        if all_patches and len(all_patches) > 1:
            other_patches = [p for p in all_patches if p.target_file != patch.target_file
                             or p.description != patch.description]
            if other_patches:
                cross_file_ctx = (
                    "\nOTHER PATCHES IN THIS HYPOTHESIS (identifiers must match exactly):\n"
                    + "\n".join(
                        "  - " + p.target_file + ": " + p.description
                        for p in other_patches
                    )
                    + "\nEnsure any function names, type names, or variable names you define "
                    "in THIS patch are spelled exactly as referenced in the patches above.\n"
                )

        return (
            "You are a Go compiler expert correcting a failed patch.\n\n"
            "FILE: " + patch.target_file + "\n"
            "PATCH DESCRIPTION: " + patch.description + "\n"
            "PATCH MODE: " + patch.patch_mode + "\n"
            "TARGET LINES: " + str(patch.start_line) + " to " + str(patch.end_line) + "\n\n"
            "YOUR PREVIOUS BROKEN CODE:\n" + bad_code + "\n\n"
            "ERROR RECEIVED:\n" + error_log.strip() + "\n\n"
            "ERROR TYPE GUIDANCE: " + guidance + "\n"
            + cross_file_ctx + "\n"
            + ctx_label + "\n" + context + "\n\n"
            "TASK: " + mode_instruction + "\n\n"
            "OUTPUT RULES:\n"
            "- Raw Go source code ONLY. No markdown, no explanations.\n"
            "- Match indentation exactly as shown in context.\n"
            "- Must pass gofmt and go vet with zero errors."
        )

    # ── Core track executor ───────────────────────────────────────────────────

    async def _apply_single_patch(
        self,
        patch: FilePatch,
        aci: AgentComputerInterface,
        track_id: str,
        retry_bad_code: str = "",
        retry_error_log: str = "",
        all_patches: list | None = None,
    ) -> tuple[bool, str, str]:
        """
        Generates and applies a single FilePatch.
        Returns (success: bool, patch_result_msg: str, code_written: str).
        """
        current_code = aci.view_file_range(
            patch.target_file,
            max(1, patch.start_line - 5),
            patch.end_line + 5,
        )

        # ── Code generation ──────────────────────────────────────────────────
        if not retry_error_log:
            if patch.patch_mode == "insert_after":
                task = (
                    "PATCH MODE: INSERT\n"
                    "Insert NEW code AFTER line " + str(patch.end_line) + ".\n"
                    "Do NOT reproduce or modify existing lines.\n"
                    "Output ONLY the new lines to insert."
                )
            else:
                task = (
                    "PATCH MODE: REPLACE\n"
                    "Write the COMPLETE replacement for lines "
                    + str(patch.start_line) + " to " + str(patch.end_line) + ".\n"
                    "Your output replaces those lines entirely."
                )

            prompt = (
                "You are a Principal Go Software Engineer.\n\n"
                "TASK: " + patch.description + "\n"
                "FILE: " + patch.target_file + "\n"
                "TARGET LINES: " + str(patch.start_line) + "-" + str(patch.end_line) + "\n\n"
                "CURRENT CODE AT TARGET LOCATION:\n" + current_code + "\n\n"
                + task + "\n\n"
                "RULES:\n"
                "- Output ONLY raw Go source code. No markdown fences, no explanations.\n"
                "- Preserve exact indentation from the context above.\n"
                "- Must pass gofmt, go vet, and go test."
            )
        else:
            prompt = self._build_heal_prompt(
                patch=patch,
                bad_code=retry_bad_code,
                error_log=retry_error_log,
                aci=aci,
                all_patches=all_patches,
            )

        gen_res = self.client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )
        code_written = clean_llm_code_output(gen_res.text)

        # ── Apply ────────────────────────────────────────────────────────────
        if patch.patch_mode == "insert_after":
            result = aci.insert_after_line(patch.target_file, patch.end_line, code_written)
        else:
            result = aci.apply_code_patch(
                patch.target_file, patch.start_line, patch.end_line, code_written
            )

        success = "⚠️" not in result
        return success, result, code_written

    async def execute_hypothesis_track(
        self,
        track_id: str,
        hypothesis: FixHypothesis,
        retry_context: dict | None = None,
    ) -> dict:
        """
        Runs one complete hypothesis track:
        1. Creates an isolated Git Worktree on a collision-free branch
        2. Applies ALL patches in hypothesis.patches sequentially to that worktree
        3. Runs go vet + go test across the entire worktree
        4. If all pass: commits every changed file and transfers branch to main repo
        5. Returns result dict with pass/fail + per-patch diagnostics

        retry_context (Cycle 2) is a dict with:
          - error_log: diagnostics string from Cycle 1
          - bad_codes: dict mapping patch index → the broken code from Cycle 1
        """
        cycle       = 2 if retry_context else 1
        branch_name = make_branch_name(self._issue_number, track_id, cycle, self._run_ts)

        wt_manager  = WorktreeManager(self.repo_path)
        wt_workspace = None

        try:
            wt_workspace = wt_manager.create_hypothesis_worktree(track_id, branch_name)
            aci = AgentComputerInterface(base_workspace_path=wt_workspace)

            # ── Apply all patches sequentially ────────────────────────────────
            all_patch_success = True
            first_failure_msg = ""
            bad_codes: dict[int, str] = {}   # index → code written, for Cycle 2

            for idx, patch in enumerate(hypothesis.patches):
                label = "[" + track_id + "][patch " + str(idx + 1) + "/" + str(len(hypothesis.patches)) + "]"
                print("📝 " + label + " Applying (" + patch.patch_mode + ") → " + patch.target_file)

                retry_bad  = (retry_context or {}).get("bad_codes", {}).get(idx, "")
                # Pass the full error log to EVERY patch in Cycle 2 — not just patch 0.
                # A go vet error like "undefined: isDNSLabel" is caused by a bad function
                # definition (patch 0) but surfaces when compiling the registration (patch 1).
                # Every patch needs the full diagnostic context to self-heal correctly.
                retry_err  = (retry_context or {}).get("error_log", "")

                ok, msg, code = await self._apply_single_patch(
                    patch, aci, track_id,
                    retry_bad_code=retry_bad,
                    retry_error_log=retry_err,
                    all_patches=hypothesis.patches,
                )
                bad_codes[idx] = code

                if not ok:
                    print("  ⚠️  Patch " + str(idx + 1) + " failed: " + msg[:120])
                    if all_patch_success:  # capture only the first failure message
                        first_failure_msg = msg
                    all_patch_success = False
                    # Do NOT break — continue applying remaining patches.
                    # Even if patch N fails, patch N+1 may succeed independently
                    # and all bad_codes need to be collected for Cycle 2 self-healing.

            if not all_patch_success:
                return {
                    "track_id":   track_id,
                    "passed":     False,
                    "diagnostics": first_failure_msg,
                    "bad_codes":  bad_codes,
                    "branch":     branch_name,
                    "hypothesis": hypothesis,
                }

            # ── Verify the whole worktree ─────────────────────────────────────
            tester     = AsyncTestSuiteRunner(wt_workspace)
            matrix_res = await tester.execute_verification_matrix(track_id)

            if matrix_res["passed"]:
                self._commit_and_transfer_branch(wt_workspace, branch_name, hypothesis)
            else:
                # CRITICAL: overwrite first_failure_msg with the real go vet/test output.
                # Patch-level gofmt errors only affect one patch and are already in
                # bad_codes. But cross-patch errors (e.g. "undefined: isDNSLabel") only
                # surface here during compilation — Cycle 2 MUST see this as the error_log
                # so every patch's heal prompt knows what the full failure was.
                first_failure_msg = matrix_res.get("diagnostics", "")

            matrix_res["branch"]     = branch_name
            matrix_res["hypothesis"] = hypothesis
            matrix_res["bad_codes"]  = bad_codes
            # Ensure the error_log stored in the result is the real compilation error,
            # not an empty string. Cycle 2 reads result["diagnostics"] as its error_log.
            if not matrix_res["passed"] and first_failure_msg:
                matrix_res["diagnostics"] = first_failure_msg
            return matrix_res

        except Exception as e:
            return {
                "track_id":   track_id,
                "passed":     False,
                "diagnostics": "Track Runtime Exception: " + str(e),
                "bad_codes":  {},
                "branch":     branch_name,
                "hypothesis": hypothesis,
            }
        finally:
            if wt_workspace and wt_manager:
                wt_manager.cleanup_worktree(track_id)

    # ── Main pipeline ─────────────────────────────────────────────────────────

    async def run_pipeline(self, issue_url: str):
        print("====== STARTING SENTINEL ENGINE EXECUTION MATRIX ======\n")

        # Phase 1: Issue Triage
        triage_data        = self.triage_engine.process_issue(issue_url)
        analysis           = triage_data["analysis"]
        self._issue_number = triage_data.get("meta", {}).get("issue_number")
        print("🎯 Target Acquired: " + triage_data["raw_title"] + "\n")

        # Phase 2: AST Symbol Index
        self.indexer.index_repository(self.repo_path, self.repo_name)

        search_terms = set()
        search_terms.add(self.repo_name)
        search_terms.add(analysis.get("target_package", ""))
        for f in analysis.get("potential_files", []):
            base = os.path.splitext(os.path.basename(f))[0]
            if base:
                search_terms.add(base)
        symptom_words = re.findall(r"[A-Za-z]{4,}", analysis.get("symptom", ""))
        search_terms.update(symptom_words[:3])

        all_symbols, seen_names = [], set()
        for term in search_terms:
            if not term:
                continue
            for s in self.indexer.lookup_symbol(self.repo_name, term):
                key = s.get("file_path", "") + "::" + s.get("symbol_name", "")
                if key not in seen_names:
                    seen_names.add(key)
                    all_symbols.append(s)

        file_inventory = _build_file_inventory(self.repo_path)
        symbol_ctx = json.dumps(all_symbols[:10], indent=2) if all_symbols else "No symbols found."

        # Phase 3: Strategy Planning
        print("🤖 Generating multi-track fix blueprints with Gemini 2.5 Pro...")
        planner_prompt = (
            "You are a Principal Go Software Engineer designing bug fix strategies.\n\n"
            "ISSUE SYMPTOM: " + str(analysis.get("symptom")) + "\n"
            "REPRODUCTION NOTES: " + str(analysis.get("reproduction_steps")) + "\n\n"
            "INDEXED SYMBOLS (file_path, symbol_name, start_line, end_line):\n"
            + symbol_ctx + "\n\n"
            "REAL FILES IN REPO (use ONLY these in target_file — never invent paths):\n"
            + file_inventory + "\n\n"
            "SCHEMA: Each hypothesis has a 'patches' list — one FilePatch per file that needs "
            "changing. A single hypothesis CAN and SHOULD include patches for multiple files "
            "when the fix requires it (e.g. both a function definition file AND a registration "
            "map file). There is no limit on the number of patches per hypothesis.\n\n"
            "CRITICAL RULES:\n"
            "1. target_file must be a path from the REAL FILES list. Never invent paths.\n"
            "2. start_line and end_line must be real line numbers from the symbol vectors.\n"
            "3. patch_mode rules:\n"
            "   'replace' — modifying EXISTING logic (bug fixes, changing existing code)\n"
            "   'insert_after' — adding NEW code that doesn't exist yet "
            "(new functions, new map entries, new validator registrations, new imports)\n"
            "4. When adding a new validator: you MUST include at least two patches:\n"
            "   a) One patch that adds the validator FUNCTION (insert_after, in baked_in.go or similar)\n"
            "   b) One patch that registers it in the validator MAP (insert_after, inside bakedInValidators)\n"
            "5. description must clearly state what this individual file change does.\n"
            "Output valid JSON matching the StrategyBlueprint schema with exactly 2 hypotheses."
        )

        response = self.client.models.generate_content(
            model="gemini-2.5-pro",
            contents=planner_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StrategyBlueprint,
                temperature=0.2,
            ),
        )

        try:
            blueprint = json.loads(response.text)
        except json.JSONDecodeError:
            m = re.search(r"(\{.*\})", response.text, re.DOTALL)
            if m:
                try:
                    blueprint = json.loads(m.group(1))
                except json.JSONDecodeError:
                    print("❌ Parsing Failure: malformed JSON.")
                    return False
            else:
                print("❌ Parsing Failure: no JSON in model output.")
                return False

        hypotheses_raw = blueprint.get("hypotheses", [])
        if len(hypotheses_raw) < 2:
            print("❌ Planning Error: fewer than 2 hypotheses returned.")
            return False

        # Validate + correct file paths in every patch across all hypotheses
        for h in hypotheses_raw:
            h["patches"] = _validate_and_correct_patches(h.get("patches", []), self.repo_path)
            if not h["patches"]:
                print("⚠️  Hypothesis '" + h.get("title", "?") + "' has no valid patches — dropping.")

        hypotheses_raw = [h for h in hypotheses_raw if h.get("patches")]
        if len(hypotheses_raw) < 2:
            print("❌ Planning Error: fewer than 2 valid hypotheses after path validation.")
            return False

        hypotheses_raw = hypotheses_raw[:2]

        print(
            "📊 Blueprint locked.\n"
            "   Track ALPHA: '" + hypotheses_raw[0]["title"] + "' ("
            + str(len(hypotheses_raw[0]["patches"])) + " file patch(es))\n"
            "   Track BETA:  '" + hypotheses_raw[1]["title"] + "' ("
            + str(len(hypotheses_raw[1]["patches"])) + " file patch(es))"
        )

        h_alpha = FixHypothesis(**hypotheses_raw[0])
        h_beta  = FixHypothesis(**hypotheses_raw[1])

        # Phase 4: Cycle 1 — Parallel Race
        print("\n🏎️  Deploying Cycle 1 tracks simultaneously across independent worktrees...")
        c1_results = await asyncio.gather(
            self.execute_hypothesis_track("TRACK_ALPHA", h_alpha),
            self.execute_hypothesis_track("TRACK_BETA",  h_beta),
        )
        winning_track = next((r for r in c1_results if r["passed"]), None)

        # Phase 5: Cycle 2 — Self-Healing (if both Cycle 1 tracks fail)
        results = c1_results
        if not winning_track:
            print("\n🚨 Cycle 1 Failed. Launching Self-Healing Cycle 2...")
            print("🏎️  Deploying Cycle 2 concurrently...")

            c2_results = await asyncio.gather(
                self.execute_hypothesis_track(
                    "TRACK_ALPHA",
                    FixHypothesis(**hypotheses_raw[0]),
                    retry_context={
                        "error_log": c1_results[0]["diagnostics"],
                        "bad_codes": c1_results[0].get("bad_codes", {}),
                    },
                ),
                self.execute_hypothesis_track(
                    "TRACK_BETA",
                    FixHypothesis(**hypotheses_raw[1]),
                    retry_context={
                        "error_log": c1_results[1]["diagnostics"],
                        "bad_codes": c1_results[1].get("bad_codes", {}),
                    },
                ),
            )
            results       = c2_results
            winning_track = next((r for r in c2_results if r["passed"]), None)

        # Phase 6: Results Summary
        print("\n🏁 --- CONCURRENT CONFLICT EVALUATION RUNTIME METRICS ---")
        for res in results:
            status = "🟩 PASSED ALL VERIFICATIONS" if res["passed"] else "🟥 FAILED SUITE"
            print("Result Vector -> Track: " + res["track_id"] + " | Status: " + status)
            if not res["passed"]:
                clean_diag = res["diagnostics"].strip().replace("\n", " ")
                print("   ↳ Diagnostics: " + clean_diag[:200] + "...")

        if not winning_track:
            print("\n❌ System Regression: All parallel self-healing tracks exhausted.")
            return False

        # Phase 7: PR Generation
        print("\n🏆 Winning Branch Verified: " + winning_track["branch"])
        print("📝 Generating pull request title and body with Gemini 2.5 Pro...\n")

        issue_meta   = triage_data.get("meta", {})
        issue_number = issue_meta.get("issue_number", "?")
        branch       = winning_track["branch"]
        hypothesis   = winning_track["hypothesis"]
        changed_files = ", ".join(p.target_file for p in hypothesis.patches)

        pr_result = self.pr_generator.generate(
            triage_data=triage_data,
            winning_hypothesis=hypothesis,
            repo_name=self.repo_name,
            branch_name=branch,
            test_diagnostics=winning_track.get(
                "diagnostics", "All verification checkpoints passed cleanly."
            ),
        )
        self.pr_generator.print_pr_summary(pr_result)

        pr_title = pr_result["title"]
        pr_body  = pr_result["body"]
        pr_url   = None
        W        = 70

        # Phase 8: Push + Open PR
        if self.fork_username and self.upstream_owner:
            pushed = self.github.push_branch(
                repo_path=self.repo_path,
                branch=branch,
                token=self.github_token,
                username=self.fork_username,
                repo=self.repo_name,
            )
            if pushed:
                pr_data = self.github.open_pull_request(
                    upstream_owner=self.upstream_owner,
                    repo=self.repo_name,
                    username=self.fork_username,
                    branch=branch,
                    title=pr_title,
                    body=pr_body,
                    issue_number=issue_number,
                )
                if pr_data:
                    pr_url = pr_data.get("html_url", "")

        # Final summary
        print("=" * W)
        print("🏁  SENTINEL ENGINE — RUN COMPLETE")
        print("=" * W)
        print("")
        print("  📁  Upstream repo  : " + self.upstream_owner + "/" + self.repo_name)
        print("  🍴  Your fork      : " + self.fork_username + "/" + self.repo_name)
        print("  🔖  Issue fixed    : #" + str(issue_number) + "  →  " + issue_url)
        print("  🌿  Branch         : " + branch)
        print("  📄  Files patched  : " + changed_files)
        print("  🏷️   PR title       : " + pr_title)
        print("")
        if pr_url:
            print("  ✅  Pull request opened successfully!")
            print("  🔗  " + pr_url)
        else:
            print("  ⚠️   PR was not auto-opened (push failed or mock mode).")
            print("  💡  To open it manually:")
            print("      cd " + self.repo_path)
            print("      git push origin " + branch)
            compare = (
                "https://github.com/" + self.upstream_owner + "/" + self.repo_name
                + "/compare/" + branch + "?expand=1"
            )
            print("      " + compare)
        print("")
        print("  ── Inspect the patch locally ─────────────────────────────")
        print("  $ cd " + self.repo_path)
        print("  $ git log " + branch + " --oneline -3")
        print("  $ git diff HEAD~1 HEAD")
        print("")
        print("=" * W)

        return pr_result