import os
import re
import json
import asyncio
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from ingestion.triage import IssueTriageEngine
from indexing.indexer import RepositoryIndexer
from aci.tools import AgentComputerInterface
from verification.worktree import WorktreeManager
from verification.tester import AsyncTestSuiteRunner

class FixHypothesis(BaseModel):
    title: str = Field(description="A short descriptive title of this specific approach.")
    target_file: str = Field(description="The relative file path targeted for modification.")
    start_line: int = Field(description="The exact start line vector coordinate.")
    end_line: int = Field(description="The exact end line vector coordinate.")
    proposed_code: str = Field(description="The raw Go code block replacement patch text.")

class StrategyBlueprint(BaseModel):
    hypotheses: list[FixHypothesis] = Field(description="A list of 2 distinct code patch hypotheses to evaluate concurrently.")

def clean_llm_code_output(raw_text: str) -> str:
    """
    Defensive parser that extracts raw source code from LLM responses, 
    stripping markdown code blocks, conversational filler, and conflict markers.
    """
    text = raw_text.strip()
    
    # Corrected regex pattern using standard non-capturing optional 'go' flag
    code_block_match = re.search(r"
http://googleusercontent.com/immersive_entry_chip/0
http://googleusercontent.com/immersive_entry_chip/1

COMPILER / LINTER ERROR TRACE RECEIVED:
{retry_error_log}

INSTRUCTIONS:
1. Correct the syntax errors, missing commas, mismatched parentheses, or malformed blocks highlighted in the error log.
2. Ensure the code block integrates cleanly with the surrounding structures.
3. Do NOT include git conflict markers (e.g., <<<<<<<, =======) or unified diff markers.
4. Output ONLY the raw replacement Go code. Do not include markdown commentary or explanations."""
                
                # Using standard production-grade gemini-1.5-flash for rapid, cost-efficient code repairs
                heal_res = self.client.models.generate_content(
                    model="gemini-1.5-flash", 
                    contents=heal_prompt
                )
                code_to_patch = clean_llm_code_output(heal_res.text)

            print(f"📝 [{track_id}] Applying patch at {hypothesis.target_file}...")
            patch_res = aci.apply_code_patch(
                file_path=hypothesis.target_file,
                start_line=hypothesis.start_line,
                end_line=hypothesis.end_line,
                new_content=code_to_patch
            )
            
            # Persist modified code version back into tracking history for diagnostics
            hypothesis.proposed_code = code_to_patch
            
            if "⚠️" in patch_res:
                return {"track_id": track_id, "passed": False, "diagnostics": patch_res, "branch": safe_branch_name, "hypothesis": hypothesis}

            tester = AsyncTestSuiteRunner(wt_workspace)
            matrix_res = await tester.execute_verification_matrix(track_id)
            
            matrix_res["branch"] = safe_branch_name
            matrix_res["hypothesis"] = hypothesis
            return matrix_res
            
        except Exception as e:
            return {"track_id": track_id, "passed": False, "diagnostics": f"Track Runtime Exception: {e}", "branch": safe_branch_name, "hypothesis": hypothesis}
        finally:
            wt_manager.cleanup_worktree(track_id)

    async def run_pipeline(self, issue_url: str):
        print("====== STARTING SENTINEL ENGINE EXECUTION MATRIX ======\n")
        
        # Phase 1: Issue Triage
        triage_data = self.triage_engine.process_issue(issue_url)
        analysis = triage_data["analysis"]
        print(f"🎯 Target Acquired: {triage_data['raw_title']}\n")

        # Phase 2: Cache Check
        commit_hash = self.indexer.index_repository(self.repo_path, self.repo_name)
        search_hint = analysis.get("potential_files", [""])[0] if analysis.get("potential_files") else ""
        symbols = self.indexer.lookup_symbol(self.repo_name, search_hint)
        symbol_ctx = json.dumps(symbols[:5], indent=2) if symbols else "No explicit cached symbol coordinates found."

        # Phase 3: Planning
        print("🤖 Generating multi-track programmatic fix blueprints using Gemini 1.5 Pro...")
        planner_prompt = f"""You are a Principal Go Software Engineer. Analyze this bug triage report and codebase coordinates, and design exactly 2 distinct bug-fix strategies.

ISSUE SYMPTOM: {analysis.get('symptom')}
REPRODUCTION NOTES: {analysis.get('reproduction_steps')}

LOCAL REPOSITORY AST SYMBOL VECTORS:
{symbol_ctx}

Output your solution as a valid JSON matching the StrategyBlueprint schema. Ensure the lines to replace are highly focused."""

        # Enforce standard production-grade gemini-1.5-pro for complex structured output operations
        response = self.client.models.generate_content(
            model="gemini-1.5-pro",
            contents=planner_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StrategyBlueprint,
                temperature=0.2
            )
        )
        
        # Defensive JSON parsing layer to guarantee protection against application crashes
        try:
            blueprint = json.loads(response.text)
        except json.JSONDecodeError:
            # Fallback regex search if conversational text was added outside the JSON block
            json_match = re.search(r"(\{.*\})", response.text, re.DOTALL)
            if json_match:
                try:
                    blueprint = json.loads(json_match.group(1))
                except json.JSONDecodeError:
                    print("❌ Parsing Failure: Found JSON-like structure but it is malformed.")
                    return
            else:
                print("❌ Parsing Failure: Model output could not be parsed into a clean structural array.")
                return

        hypotheses = blueprint.get("hypotheses", [])
        if len(hypotheses) < 2:
            print("❌ Planning Error: Failed to generate distinct tracks.")
            return

        print(f"📊 Blueprint locked. Track ALPHA: '{hypotheses[0]['title']}' | Track BETA: '{hypotheses[1]['title']}'")

        # Phase 4: Run Cycle 1 (Parallel Race)
        print("\n🏎️  Deploying Cycle 1 isolation tracks simultaneously across independent worktrees...")
        tasks = [
            self.execute_hypothesis_track("TRACK_ALPHA", FixHypothesis(**hypotheses[0])),
            self.execute_hypothesis_track("TRACK_BETA", FixHypothesis(**hypotheses[1]))
        ]
        results = await asyncio.gather(*tasks)

        # Evaluate Cycle 1
        winning_track = next((res for res in results if res["passed"]), None)

        # Phase 5: Self-Healing Trigger (If Cycle 1 completely fails)
        if not winning_track:
            print("\n🚨 Cycle 1 Failed. Intercepting diagnostic logs to initialize Self-Healing loops...")
            
            # Pass the failure logs from the first run right back into a parallel Retry Cycle
            retry_tasks = [
                self.execute_hypothesis_track("TRACK_ALPHA", FixHypothesis(**hypotheses[0]), retry_error_log=results[0]["diagnostics"]),
                self.execute_hypothesis_track("TRACK_BETA", FixHypothesis(**hypotheses[1]), retry_error_log=results[1]["diagnostics"])
            ]
            print("🏎️  Deploying Cycle 2 (Healed Track Run) concurrently...")
            results = await asyncio.gather(*retry_tasks)
            winning_track = next((res for res in results if res["passed"]), None)

        print("\n🏁 --- CONCURRENT CONFLICT EVALUATION RUNTIME METRICS ---")
        for res in results:
            status = "🟩 PASSED ALL VERIFICATIONS" if res["passed"] else "🟥 FAILED SUITE"
            print(f"Result Vector -> Track: {res['track_id']} | Status: {status}")
            if not res["passed"]:
                # Separated the string backslash manipulation to be compatible with all Python versions
                clean_diagnostics = res["diagnostics"].strip().replace("\n", " ")
                print(f"   ↳ Diagnostics: {clean_diagnostics[:140]}...")

        if winning_track:
            print(f"\n🏆 Winning Branch Verified: {winning_track['branch']}")
            print("📝 Automatically compiling engineering pull request overview...")
            return True
        else:
            print("\n❌ System Regression: All parallel self-healing tracks exhausted.")
            return False