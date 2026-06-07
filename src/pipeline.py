import os
import re
import json
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


class FixHypothesis(BaseModel):
    title: str = Field(description="A short descriptive title of this specific approach.")
    target_file: str = Field(description="The relative file path targeted for modification.")
    start_line: int = Field(description="The exact start line vector coordinate.")
    end_line: int = Field(description="The exact end line vector coordinate.")
    proposed_code: str = Field(description="The raw Go code block replacement patch text.")


class StrategyBlueprint(BaseModel):
    hypotheses: list[FixHypothesis] = Field(
        description="A list of 2 distinct code patch hypotheses to evaluate concurrently."
    )


def clean_llm_code_output(raw_text: str) -> str:
    """
    Strips markdown code fences, git conflict markers, and diff markers
    from LLM output, returning only raw Go source code.
    """
    text = raw_text.strip()
    code_block_match = re.search(r"```(?:go)?\s*\n(.*?)```", text, re.DOTALL)
    if code_block_match:
        text = code_block_match.group(1).strip()
    text = re.compile(r"^(<{7}|={7}|>{7}).*$", re.MULTILINE).sub("", text).strip()
    text = re.compile(r"^[+-]{3}\s+.*$", re.MULTILINE).sub("", text).strip()
    return text


def extract_error_line(error_text: str) -> int | None:
    """
    Parses gofmt / go vet error output to extract the first error line number.
    Handles patterns like: 'file.go:173:5: expected operand'
    """
    match = re.search(r":(\d+):\d+:", error_text)
    if match:
        return int(match.group(1))
    return None


def make_branch_name(issue_number, track_id: str, cycle: int = 1) -> str:
    """
    Generates a collision-free branch name encoding issue number, track, and cycle.
    Pattern: sentinel/issue-{N}/{track}-c{cycle}
    Example: sentinel/issue-1543/track-alpha-c1
    """
    issue_slug = "issue-" + str(issue_number) + "/" if issue_number else ""
    track_slug = re.sub(r"[^a-zA-Z0-9]", "-", track_id.lower())
    return "sentinel/" + issue_slug + track_slug + "-c" + str(cycle)


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
        self.client = genai.Client(api_key=gemini_key)
        self.triage_engine = IssueTriageEngine(gemini_key, github_token)
        self.pr_generator = PRGenerator(gemini_key)
        self.indexer = RepositoryIndexer()
        self.github = GitHubClient(github_token)
        self.github_token = github_token
        self.repo_path = os.path.abspath(local_repo_path)
        self.repo_name = repo_name
        self.upstream_owner = upstream_owner
        self.fork_username = fork_username
        self._issue_number: int | None = None

    def _commit_and_transfer_branch(
        self,
        wt_workspace: str,
        branch_name: str,
        hypothesis: FixHypothesis,
    ) -> bool:
        """
        Stages all modified files, commits the patch inside the worktree, then
        transfers the branch to the main repo so it survives worktree cleanup.

        Transfer strategy:
          Primary:  git fetch <normalized_path> branch:branch
          Fallback: git branch -f <branch> <commit_hash>  (Windows git compatibility)
        """
        commit_msg = (
            "fix: " + hypothesis.title + "\n\n"
            "Applied by Sentinel Engine.\n"
            "Modified: " + hypothesis.target_file
            + " (lines " + str(hypothesis.start_line)
            + "-" + str(hypothesis.end_line) + ")"
        )

        add_res = subprocess.run(
            ["git", "add", "-A"],
            cwd=wt_workspace, capture_output=True, text=True
        )
        if add_res.returncode != 0:
            print("  ⚠️  git add -A failed: " + add_res.stderr.strip())
            return False

        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=wt_workspace, capture_output=True, text=True
        )
        if not staged.stdout.strip():
            print("  ⚠️  Nothing staged — patch produced no file changes.")
            return False

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
            ["git", "rev-parse", "HEAD"],
            cwd=wt_workspace, capture_output=True, text=True
        )
        if hash_res.returncode != 0:
            print("  ⚠️  Could not read commit hash: " + hash_res.stderr.strip())
            return False
        commit_hash = hash_res.stdout.strip()

        # Primary: git fetch with normalized path (forward slashes for Windows)
        wt_normalized = wt_workspace.replace(os.sep, "/")
        fetch_res = subprocess.run(
            ["git", "fetch", wt_normalized, branch_name + ":" + branch_name],
            cwd=self.repo_path, capture_output=True, text=True
        )
        if fetch_res.returncode == 0:
            print("  ✅ Branch '" + branch_name + "' transferred to main repo.")
            return True

        # Fallback: point a branch directly at the commit hash
        create_res = subprocess.run(
            ["git", "branch", "-f", branch_name, commit_hash],
            cwd=self.repo_path, capture_output=True, text=True
        )
        if create_res.returncode == 0:
            print("  ✅ Branch '" + branch_name + "' created via hash transfer fallback.")
            return True

        print("  ❌ Both branch transfer methods failed.")
        print("     fetch: " + fetch_res.stderr.strip())
        print("     branch -f: " + create_res.stderr.strip())
        return False

    def _build_heal_prompt(
        self,
        hypothesis: FixHypothesis,
        bad_code: str,
        error_log: str,
        aci: "AgentComputerInterface",
    ) -> str:
        """
        Builds a targeted self-healing prompt that shows:
        - The bad code that was actually written
        - The exact gofmt/vet error message
        - A wider context window centred on the error line
        This gives the LLM everything it needs to make a precise correction.
        """
        # Extract the error line number so we can show context around it
        error_line = extract_error_line(error_log)

        if error_line:
            # Wide context window centred on the error line
            ctx_start = max(1, error_line - 10)
            ctx_end   = error_line + 10
            error_context = aci.view_file_range(hypothesis.target_file, ctx_start, ctx_end)
            context_label = (
                "FILE CONTENT AROUND ERROR (lines "
                + str(ctx_start) + "-" + str(ctx_end) + "):"
            )
        else:
            # Fallback: show the original target range
            ctx_start = max(1, hypothesis.start_line - 5)
            ctx_end   = hypothesis.end_line + 5
            error_context = aci.view_file_range(hypothesis.target_file, ctx_start, ctx_end)
            context_label = (
                "FILE CONTENT AT TARGET RANGE (lines "
                + str(ctx_start) + "-" + str(ctx_end) + "):"
            )

        # Classify the error type so the LLM gets the right instruction
        if "SYNTAX FAIL" in error_log or "expected operand" in error_log or "gofmt" in error_log.lower():
            error_type_instruction = (
                "This is a Go SYNTAX error caught by gofmt before compilation.\n"
                "Common causes: unmatched parentheses, missing commas in struct literals, "
                "misplaced closing braces, or malformed function signatures.\n"
                "Carefully check bracket and parenthesis balance."
            )
        else:
            error_type_instruction = (
                "This is a Go COMPILER or VET error.\n"
                "Check for: undefined identifiers, wrong function signatures, "
                "type mismatches, or missing imports."
            )

        return (
            "You are a Go compiler expert. Your previous code attempt produced an error.\n"
            "You must output a CORRECTED replacement.\n\n"
            "FIX STRATEGY: " + hypothesis.title + "\n"
            "TARGET FILE: " + hypothesis.target_file + "\n"
            "TARGET LINES TO REPLACE: " + str(hypothesis.start_line)
            + " to " + str(hypothesis.end_line) + "\n\n"
            "YOUR PREVIOUS (BROKEN) CODE:\n"
            + bad_code + "\n\n"
            "ERROR RECEIVED:\n"
            + error_log.strip() + "\n\n"
            "ERROR TYPE GUIDANCE:\n"
            + error_type_instruction + "\n\n"
            + context_label + "\n"
            + error_context + "\n\n"
            "OUTPUT RULES:\n"
            "- Output ONLY the corrected raw Go source code for lines "
            + str(hypothesis.start_line) + "-" + str(hypothesis.end_line) + ".\n"
            "- Do NOT include markdown fences, explanations, or diff markers.\n"
            "- Preserve indentation exactly as shown in the context above.\n"
            "- The code must pass gofmt and go vet with zero errors."
        )

    async def execute_hypothesis_track(
        self,
        track_id: str,
        hypothesis: FixHypothesis,
        retry_context: dict | None = None,
    ) -> dict:
        """
        Runs one isolated hypothesis track end-to-end.

        retry_context (for Cycle 2) is a dict with:
          - error_log:  the diagnostics string from Cycle 1
          - bad_code:   the exact code that was written in Cycle 1 (may differ
                        from hypothesis.proposed_code if ACI rejected it early)
        """
        cycle = 2 if retry_context else 1
        branch_name = make_branch_name(self._issue_number, track_id, cycle)

        wt_manager = WorktreeManager(self.repo_path)
        wt_workspace = None

        try:
            wt_workspace = wt_manager.create_hypothesis_worktree(track_id, branch_name)
            aci = AgentComputerInterface(base_workspace_path=wt_workspace)

            current_code = aci.view_file_range(
                hypothesis.target_file,
                max(1, hypothesis.start_line - 5),
                hypothesis.end_line + 5,
            )

            # ── Generate patch code ───────────────────────────────────────────
            if not retry_context:
                # Cycle 1: fresh generation
                gen_prompt = (
                    "You are a Principal Go Software Engineer performing a precise surgical bug fix.\n\n"
                    "FIX STRATEGY: " + hypothesis.title + "\n"
                    "TARGET FILE: " + hypothesis.target_file + "\n"
                    "TARGET LINES: " + str(hypothesis.start_line)
                    + " to " + str(hypothesis.end_line) + "\n\n"
                    "CURRENT CODE AT TARGET LOCATION:\n" + current_code + "\n\n"
                    "TASK: Write the replacement Go code for lines "
                    + str(hypothesis.start_line) + "-" + str(hypothesis.end_line) + ".\n"
                    "- Output ONLY the raw Go source code to replace those lines.\n"
                    "- Do NOT include markdown fences, explanations, or diff markers.\n"
                    "- Preserve indentation and integrate cleanly with surrounding code.\n"
                    "- The replacement must pass gofmt, go vet, and go test."
                )
                gen_res = self.client.models.generate_content(
                    model="gemini-2.5-flash", contents=gen_prompt,
                )
                code_to_patch = clean_llm_code_output(gen_res.text)

            else:
                # Cycle 2: self-healing with full error context
                heal_prompt = self._build_heal_prompt(
                    hypothesis=hypothesis,
                    bad_code=retry_context["bad_code"],
                    error_log=retry_context["error_log"],
                    aci=aci,
                )
                heal_res = self.client.models.generate_content(
                    model="gemini-2.5-flash", contents=heal_prompt,
                )
                code_to_patch = clean_llm_code_output(heal_res.text)

            # ── Apply patch ───────────────────────────────────────────────────
            print("📝 [" + track_id + "] Applying patch at " + hypothesis.target_file + "...")
            patch_res = aci.apply_code_patch(
                file_path=hypothesis.target_file,
                start_line=hypothesis.start_line,
                end_line=hypothesis.end_line,
                new_content=code_to_patch,
            )

            # Always record what was actually written — Cycle 2 needs this
            hypothesis.proposed_code = code_to_patch

            if "⚠️" in patch_res:
                # ACI pre-lint (gofmt) rejected the patch — pass the real error
                # and the actual bad code to the caller so Cycle 2 can heal it
                return {
                    "track_id":   track_id,
                    "passed":     False,
                    "diagnostics": patch_res,
                    "bad_code":   code_to_patch,   # ← the code that caused the error
                    "branch":     branch_name,
                    "hypothesis": hypothesis,
                }

            # ── Run verification suite ────────────────────────────────────────
            tester = AsyncTestSuiteRunner(wt_workspace)
            matrix_res = await tester.execute_verification_matrix(track_id)

            if matrix_res["passed"]:
                self._commit_and_transfer_branch(wt_workspace, branch_name, hypothesis)

            matrix_res["branch"]     = branch_name
            matrix_res["hypothesis"] = hypothesis
            matrix_res["bad_code"]   = code_to_patch
            return matrix_res

        except Exception as e:
            return {
                "track_id":   track_id,
                "passed":     False,
                "diagnostics": "Track Runtime Exception: " + str(e),
                "bad_code":   hypothesis.proposed_code if hypothesis else "",
                "branch":     branch_name,
                "hypothesis": hypothesis,
            }
        finally:
            if wt_workspace and wt_manager:
                wt_manager.cleanup_worktree(track_id)

    async def run_pipeline(self, issue_url: str):
        print("====== STARTING SENTINEL ENGINE EXECUTION MATRIX ======\n")

        # Phase 1: Issue Triage
        triage_data = self.triage_engine.process_issue(issue_url)
        analysis = triage_data["analysis"]
        self._issue_number = triage_data.get("meta", {}).get("issue_number")
        print("🎯 Target Acquired: " + triage_data["raw_title"] + "\n")

        # Phase 2: AST Cache Check
        self.indexer.index_repository(self.repo_path, self.repo_name)
        search_hint = (
            analysis.get("potential_files", [""])[0]
            if analysis.get("potential_files") else ""
        )
        symbols = self.indexer.lookup_symbol(self.repo_name, search_hint)
        symbol_ctx = (
            json.dumps(symbols[:5], indent=2) if symbols
            else "No explicit cached symbol coordinates found."
        )

        # Phase 3: Strategy Planning
        print("🤖 Generating multi-track fix blueprints with Gemini 2.5 Pro...")
        planner_prompt = (
            "You are a Principal Go Software Engineer. Analyze this bug triage report "
            "and codebase coordinates, and design exactly 2 distinct bug-fix strategies.\n\n"
            "ISSUE SYMPTOM: " + str(analysis.get("symptom")) + "\n"
            "REPRODUCTION NOTES: " + str(analysis.get("reproduction_steps")) + "\n\n"
            "LOCAL REPOSITORY AST SYMBOL VECTORS:\n" + symbol_ctx + "\n\n"
            "Output your solution as valid JSON matching the StrategyBlueprint schema. "
            "Ensure the lines to replace are highly focused."
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
            json_match = re.search(r"(\{.*\})", response.text, re.DOTALL)
            if json_match:
                try:
                    blueprint = json.loads(json_match.group(1))
                except json.JSONDecodeError:
                    print("❌ Parsing Failure: JSON structure found but malformed.")
                    return False
            else:
                print("❌ Parsing Failure: Model output could not be parsed.")
                return False

        hypotheses = blueprint.get("hypotheses", [])
        if len(hypotheses) < 2:
            print("❌ Planning Error: Failed to generate 2 distinct tracks.")
            return False

        print(
            "📊 Blueprint locked. "
            "Track ALPHA: '" + hypotheses[0]["title"] + "' | "
            "Track BETA: '"  + hypotheses[1]["title"] + "'"
        )

        # Phase 4: Cycle 1 — Parallel Race
        print("\n🏎️  Deploying Cycle 1 tracks simultaneously across independent worktrees...")
        c1_results = await asyncio.gather(
            self.execute_hypothesis_track("TRACK_ALPHA", FixHypothesis(**hypotheses[0])),
            self.execute_hypothesis_track("TRACK_BETA",  FixHypothesis(**hypotheses[1])),
        )
        winning_track = next((r for r in c1_results if r["passed"]), None)

        # Phase 5: Cycle 2 — Self-Healing
        results = c1_results
        if not winning_track:
            print("\n🚨 Cycle 1 Failed. Launching Self-Healing Cycle 2...")
            print("🏎️  Deploying Cycle 2 (Healed Track Run) concurrently...")

            # Pass retry_context with both the error AND the actual bad code
            # that was written — not just hypothesis.proposed_code which may
            # still hold the original blueprint value
            c2_results = await asyncio.gather(
                self.execute_hypothesis_track(
                    "TRACK_ALPHA",
                    FixHypothesis(**hypotheses[0]),
                    retry_context={
                        "error_log": c1_results[0]["diagnostics"],
                        "bad_code":  c1_results[0].get("bad_code", hypotheses[0].get("proposed_code", "")),
                    },
                ),
                self.execute_hypothesis_track(
                    "TRACK_BETA",
                    FixHypothesis(**hypotheses[1]),
                    retry_context={
                        "error_log": c1_results[1]["diagnostics"],
                        "bad_code":  c1_results[1].get("bad_code", hypotheses[1].get("proposed_code", "")),
                    },
                ),
            )
            results = c2_results
            winning_track = next((r for r in c2_results if r["passed"]), None)

        # Phase 6: Results Summary
        print("\n🏁 --- CONCURRENT CONFLICT EVALUATION RUNTIME METRICS ---")
        for res in results:
            status = "🟩 PASSED ALL VERIFICATIONS" if res["passed"] else "🟥 FAILED SUITE"
            print("Result Vector -> Track: " + res["track_id"] + " | Status: " + status)
            if not res["passed"]:
                clean_diag = res["diagnostics"].strip().replace("\n", " ")
                print("   ↳ Diagnostics: " + clean_diag[:200] + "...")

        # Phase 7 + 8: PR Generation → Push → Open PR
        if not winning_track:
            print("\n❌ System Regression: All parallel self-healing tracks exhausted.")
            return False

        print("\n🏆 Winning Branch Verified: " + winning_track["branch"])
        print("📝 Generating pull request title and body with Gemini 2.5 Pro...\n")

        issue_meta   = triage_data.get("meta", {})
        issue_number = issue_meta.get("issue_number", "?")
        branch       = winning_track["branch"]
        changed_file = winning_track["hypothesis"].target_file

        pr_result = self.pr_generator.generate(
            triage_data=triage_data,
            winning_hypothesis=winning_track["hypothesis"],
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

        print("=" * W)
        print("🏁  SENTINEL ENGINE — RUN COMPLETE")
        print("=" * W)
        print("")
        print("  📁  Upstream repo  : " + self.upstream_owner + "/" + self.repo_name)
        print("  🍴  Your fork      : " + self.fork_username + "/" + self.repo_name)
        print("  🔖  Issue fixed    : #" + str(issue_number) + "  →  " + issue_url)
        print("  🌿  Branch         : " + branch)
        print("  📄  File patched   : " + changed_file)
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