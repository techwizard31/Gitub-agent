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
    Defensive parser that extracts raw source code from LLM responses,
    stripping markdown code blocks, conversational filler, and conflict markers.
    """
    text = raw_text.strip()

    # Strip markdown code fences (```go ... ``` or ``` ... ```)
    code_block_match = re.search(r"```(?:go)?\s*\n(.*?)```", text, re.DOTALL)
    if code_block_match:
        text = code_block_match.group(1).strip()

    # Strip git conflict markers if LLM hallucinated a diff
    conflict_pattern = re.compile(r"^(<{7}|={7}|>{7}).*$", re.MULTILINE)
    text = conflict_pattern.sub("", text).strip()

    # Strip unified diff markers
    diff_pattern = re.compile(r"^[+-]{3}\s+.*$", re.MULTILINE)
    text = diff_pattern.sub("", text).strip()

    return text


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

    def _commit_patch_to_branch(self, wt_workspace: str, branch_name: str, hypothesis: FixHypothesis):
        """
        Commits the applied patch inside the worktree, then fetches that branch
        back into the main repo so it survives worktree cleanup and can be pushed.
        """
        commit_msg = (
            "fix: " + hypothesis.title + "\n\n"
            "Applied by Sentinel Engine.\n"
            "Modified: " + hypothesis.target_file +
            " (lines " + str(hypothesis.start_line) + "-" + str(hypothesis.end_line) + ")"
        )

        subprocess.run(
            ["git", "add", hypothesis.target_file],
            cwd=wt_workspace, capture_output=True
        )
        result = subprocess.run(
            ["git", "-c", "user.name=Sentinel", "-c", "user.email=agent@sentinel.ai",
             "commit", "-m", commit_msg],
            cwd=wt_workspace, capture_output=True, text=True
        )
        if result.returncode != 0:
            print("  ⚠️  git commit in worktree failed: " + result.stderr.strip())
            return

        # Fetch branch from worktree into the main repo
        fetch_result = subprocess.run(
            ["git", "fetch", wt_workspace, branch_name + ":" + branch_name],
            cwd=self.repo_path, capture_output=True, text=True
        )
        if fetch_result.returncode == 0:
            print("  ✅ Branch '" + branch_name + "' saved to main repo — ready to push.")
        else:
            print("  ⚠️  Could not fetch branch to main repo: " + fetch_result.stderr.strip())

    async def execute_hypothesis_track(
        self,
        track_id: str,
        hypothesis: FixHypothesis,
        retry_error_log: str = "",
    ) -> dict:
        """
        Runs a single isolated hypothesis track:
        1. Creates a fresh Git Worktree branch
        2. Asks Gemini to generate (or heal) code for the patch
        3. Applies the patch via ACI
        4. Runs go vet + go test via AsyncTestSuiteRunner
        5. If passing, commits the patch and fetches branch to main repo
        6. Returns the full result dict with pass/fail diagnostics
        """
        safe_branch_name = re.sub(r"[^a-zA-Z0-9_\-]", "-", "sentinel/" + track_id.lower())

        wt_manager = WorktreeManager(self.repo_path)
        wt_workspace = None

        try:
            wt_workspace = wt_manager.create_hypothesis_worktree(track_id, safe_branch_name)
            aci = AgentComputerInterface(base_workspace_path=wt_workspace)

            # --- Step 1: Read the current file content at the target range ---
            current_code = aci.view_file_range(
                hypothesis.target_file,
                max(1, hypothesis.start_line - 5),
                hypothesis.end_line + 5,
            )

            # --- Step 2: Generate or self-heal the patch code ---
            if not retry_error_log:
                gen_prompt = (
                    "You are a Principal Go Software Engineer performing a precise surgical bug fix.\n\n"
                    "FIX STRATEGY: " + hypothesis.title + "\n"
                    "TARGET FILE: " + hypothesis.target_file + "\n"
                    "TARGET LINES: " + str(hypothesis.start_line) + " to " + str(hypothesis.end_line) + "\n\n"
                    "CURRENT CODE AT TARGET LOCATION:\n" + current_code + "\n\n"
                    "TASK: Write the replacement Go code for lines "
                    + str(hypothesis.start_line) + "-" + str(hypothesis.end_line) + ".\n"
                    "- Output ONLY the raw Go source code to replace those lines.\n"
                    "- Do NOT include markdown fences, explanations, or diff markers.\n"
                    "- Preserve indentation and ensure the code integrates cleanly with its surroundings.\n"
                    "- The replacement must compile and pass go vet."
                )
                # gemini-2.5-flash: fast, cost-efficient code generation for patch tracks
                gen_res = self.client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=gen_prompt,
                )
                code_to_patch = clean_llm_code_output(gen_res.text)

            else:
                # Cycle 2: Self-healing — feed the error log back with the previous attempt
                heal_prompt = (
                    "You are a Go compiler expert performing targeted error correction.\n\n"
                    "FIX STRATEGY: " + hypothesis.title + "\n"
                    "TARGET FILE: " + hypothesis.target_file + "\n"
                    "TARGET LINES: " + str(hypothesis.start_line) + " to " + str(hypothesis.end_line) + "\n\n"
                    "PREVIOUS CODE ATTEMPT:\n" + hypothesis.proposed_code + "\n\n"
                    "COMPILER / LINTER ERROR TRACE RECEIVED:\n" + retry_error_log + "\n\n"
                    "CURRENT CODE CONTEXT AT TARGET LOCATION:\n" + current_code + "\n\n"
                    "INSTRUCTIONS:\n"
                    "1. Correct the syntax errors, missing commas, mismatched parentheses, or malformed blocks.\n"
                    "2. Ensure the code block integrates cleanly with the surrounding structures.\n"
                    "3. Do NOT include git conflict markers or unified diff markers.\n"
                    "4. Output ONLY the raw replacement Go code."
                )
                # gemini-2.5-flash: rapid code repairs in the healing loop
                heal_res = self.client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=heal_prompt,
                )
                code_to_patch = clean_llm_code_output(heal_res.text)

            print("📝 [" + track_id + "] Applying patch at " + hypothesis.target_file + "...")
            patch_res = aci.apply_code_patch(
                file_path=hypothesis.target_file,
                start_line=hypothesis.start_line,
                end_line=hypothesis.end_line,
                new_content=code_to_patch,
            )

            # Persist modified code version back into tracking history for diagnostics
            hypothesis.proposed_code = code_to_patch

            if "⚠️" in patch_res:
                return {
                    "track_id": track_id,
                    "passed": False,
                    "diagnostics": patch_res,
                    "branch": safe_branch_name,
                    "hypothesis": hypothesis,
                }

            tester = AsyncTestSuiteRunner(wt_workspace)
            matrix_res = await tester.execute_verification_matrix(track_id)

            if matrix_res["passed"]:
                # Commit the verified patch and bring branch into the main repo
                self._commit_patch_to_branch(wt_workspace, safe_branch_name, hypothesis)

            matrix_res["branch"] = safe_branch_name
            matrix_res["hypothesis"] = hypothesis
            return matrix_res

        except Exception as e:
            return {
                "track_id": track_id,
                "passed": False,
                "diagnostics": "Track Runtime Exception: " + str(e),
                "branch": safe_branch_name,
                "hypothesis": hypothesis if "hypothesis" in dir() else None,
            }
        finally:
            # Always clean up the worktree directory.
            # The branch was already fetched into the main repo above, so this is safe.
            if wt_workspace and wt_manager:
                wt_manager.cleanup_worktree(track_id)

    async def run_pipeline(self, issue_url: str):
        print("====== STARTING SENTINEL ENGINE EXECUTION MATRIX ======\n")

        # Phase 1: Issue Triage
        triage_data = self.triage_engine.process_issue(issue_url)
        analysis = triage_data["analysis"]
        print("🎯 Target Acquired: " + triage_data["raw_title"] + "\n")

        # Phase 2: AST Cache Check
        commit_hash = self.indexer.index_repository(self.repo_path, self.repo_name)
        search_hint = (
            analysis.get("potential_files", [""])[0]
            if analysis.get("potential_files")
            else ""
        )
        symbols = self.indexer.lookup_symbol(self.repo_name, search_hint)
        symbol_ctx = (
            json.dumps(symbols[:5], indent=2)
            if symbols
            else "No explicit cached symbol coordinates found."
        )

        # Phase 3: Strategy Planning
        print("🤖 Generating multi-track fix blueprints with Gemini 2.5 Pro...")
        planner_prompt = (
            "You are a Principal Go Software Engineer. Analyze this bug triage report and codebase coordinates, "
            "and design exactly 2 distinct bug-fix strategies.\n\n"
            "ISSUE SYMPTOM: " + str(analysis.get("symptom")) + "\n"
            "REPRODUCTION NOTES: " + str(analysis.get("reproduction_steps")) + "\n\n"
            "LOCAL REPOSITORY AST SYMBOL VECTORS:\n" + symbol_ctx + "\n\n"
            "Output your solution as a valid JSON matching the StrategyBlueprint schema. "
            "Ensure the lines to replace are highly focused."
        )

        # gemini-2.5-pro: complex multi-hypothesis structured planning
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
                    print("❌ Parsing Failure: Found JSON-like structure but it is malformed.")
                    return False
            else:
                print("❌ Parsing Failure: Model output could not be parsed.")
                return False

        hypotheses = blueprint.get("hypotheses", [])
        if len(hypotheses) < 2:
            print("❌ Planning Error: Failed to generate distinct tracks.")
            return False

        print(
            "📊 Blueprint locked. "
            "Track ALPHA: '" + hypotheses[0]["title"] + "' | "
            "Track BETA: '" + hypotheses[1]["title"] + "'"
        )

        # Phase 4: Cycle 1 — Parallel Race
        print("\n🏎️  Deploying Cycle 1 tracks simultaneously across independent worktrees...")
        tasks = [
            self.execute_hypothesis_track("TRACK_ALPHA", FixHypothesis(**hypotheses[0])),
            self.execute_hypothesis_track("TRACK_BETA", FixHypothesis(**hypotheses[1])),
        ]
        results = await asyncio.gather(*tasks)
        winning_track = next((res for res in results if res["passed"]), None)

        # Phase 5: Cycle 2 — Self-Healing (if Cycle 1 fails)
        if not winning_track:
            print("\n🚨 Cycle 1 Failed. Intercepting diagnostic logs for Self-Healing loop...")
            retry_tasks = [
                self.execute_hypothesis_track(
                    "TRACK_ALPHA",
                    FixHypothesis(**hypotheses[0]),
                    retry_error_log=results[0]["diagnostics"],
                ),
                self.execute_hypothesis_track(
                    "TRACK_BETA",
                    FixHypothesis(**hypotheses[1]),
                    retry_error_log=results[1]["diagnostics"],
                ),
            ]
            print("🏎️  Deploying Cycle 2 (Healed Track Run) concurrently...")
            results = await asyncio.gather(*retry_tasks)
            winning_track = next((res for res in results if res["passed"]), None)

        # Phase 6: Results Summary
        print("\n🏁 --- CONCURRENT CONFLICT EVALUATION RUNTIME METRICS ---")
        for res in results:
            status = "🟩 PASSED ALL VERIFICATIONS" if res["passed"] else "🟥 FAILED SUITE"
            print("Result Vector -> Track: " + res["track_id"] + " | Status: " + status)
            if not res["passed"]:
                clean_diagnostics = res["diagnostics"].strip().replace("\n", " ")
                print("   ↳ Diagnostics: " + clean_diagnostics[:140] + "...")

        # Phase 7: PR Generation
        if winning_track:
            print("\n🏆 Winning Branch Verified: " + winning_track["branch"])
            print("📝 Generating pull request title and body with Gemini 2.5 Pro...\n")

            pr_result = self.pr_generator.generate(
                triage_data=triage_data,
                winning_hypothesis=winning_track["hypothesis"],
                repo_name=self.repo_name,
                branch_name=winning_track["branch"],
                test_diagnostics=winning_track.get(
                    "diagnostics", "All verification checkpoints passed cleanly."
                ),
            )

            self.pr_generator.print_pr_summary(pr_result)

            # ── Phase 8: Push branch + Open PR ───────────────────────────────
            issue_meta   = triage_data.get("meta", {})
            issue_number = issue_meta.get("issue_number", "?")
            branch       = winning_track["branch"]
            changed_file = winning_track["hypothesis"].target_file
            pr_title     = pr_result["title"]
            pr_body      = pr_result["body"]
            W            = 70

            # Only attempt push + PR if we have real GitHub context (not mock mode)
            pr_url = None
            if self.fork_username and self.upstream_owner:
                # 1. Push the branch from the local fork clone to GitHub
                pushed = self.github.push_branch(
                    repo_path=self.repo_path,
                    branch=branch,
                    token=self.github_token,
                    username=self.fork_username,
                    repo=self.repo_name,
                )

                # 2. Open the cross-repo PR: fork:branch → upstream:main
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

            # ── Final Summary ─────────────────────────────────────────────────
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
                print("  ⚠️   PR was not auto-opened (mock mode or push failed).")
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

        else:
            print("\n❌ System Regression: All parallel self-healing tracks exhausted.")
            return False