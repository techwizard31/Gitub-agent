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
    """One atomic file change — one entry in a multi-file patch set."""
    target_file: str = Field(
        description="Relative path of the file to modify (must exist in the repo)."
    )
    start_line: int = Field(description="First line of the region to operate on.")
    end_line: int = Field(description="Last line of the region to operate on.")
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
            "For 'replace': complete replacement for start_line..end_line. "
            "For 'insert_after': only the new lines to insert — do not repeat existing lines."
        )
    )


class FixHypothesis(BaseModel):
    """One complete fix strategy, potentially spanning multiple files."""
    title: str = Field(description="Short descriptive title of this fix approach.")
    patches: list[FilePatch] = Field(
        description=(
            "Ordered list of file changes that together implement this fix. "
            "Include ALL files that need to change. No limit on patch count."
        )
    )


class StrategyBlueprint(BaseModel):
    hypotheses: list[FixHypothesis] = Field(
        description="Exactly 2 distinct fix strategies to evaluate concurrently."
    )


# ── Utilities ─────────────────────────────────────────────────────────────────

def clean_llm_code_output(raw_text: str) -> str:
    """Strips markdown fences, conflict markers, and diff markers from LLM output."""
    if not raw_text:
        return ""
    text = raw_text.strip()
    m = re.search(r"```(?:go)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    text = re.compile(r"^(<{7}|={7}|>{7}).*$", re.MULTILINE).sub("", text).strip()
    text = re.compile(r"^[+-]{3}\s+.*$", re.MULTILINE).sub("", text).strip()
    return text


def extract_error_line(error_text: str) -> int | None:
    """Extracts first error line number from gofmt/go vet output."""
    m = re.search(r":(\d+):\d+:", error_text)
    return int(m.group(1)) if m else None


def extract_error_file(error_text: str) -> str | None:
    """Extracts the filename from a gofmt/go vet error line."""
    m = re.search(r"([\w./\\-]+\.go):(\d+):\d+:", error_text)
    return m.group(1) if m else None


def make_branch_name(issue_number, track_id: str, cycle: int, run_ts: int) -> str:
    """
    Globally unique branch name: sentinel/issue-{N}/{track}-c{cycle}-{ts}
    run_ts is fixed at pipeline start so re-running always gets a new timestamp.
    """
    issue_slug = "issue-" + str(issue_number) + "/" if issue_number else ""
    track_slug = re.sub(r"[^a-zA-Z0-9]", "-", track_id.lower())
    return "sentinel/" + issue_slug + track_slug + "-c" + str(cycle) + "-" + str(run_ts)


def _build_file_inventory(repo_path: str) -> str:
    """Real .go source files with line counts — ground truth for the planner."""
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
                    lines = sum(1 for _ in f)
                entries.append(rel + "  (" + str(lines) + " lines)")
            except OSError:
                pass
    if not entries:
        return "No Go source files found."
    if len(entries) > 40:
        entries = entries[:40] + ["... (truncated)"]
    return "\n".join(entries)


def _validate_and_correct_patches(patches: list[dict], repo_path: str) -> list[dict]:
    """Verify each patch's target_file exists. Attempt basename match for hallucinated paths."""
    good = []
    for p in patches:
        target = p.get("target_file", "")
        full   = os.path.join(repo_path, target.replace("/", os.sep))
        if os.path.isfile(full):
            good.append(p)
            continue
        base = os.path.basename(target)
        found = False
        for root, _, files in os.walk(repo_path):
            if base in files:
                real_rel = os.path.relpath(
                    os.path.join(root, base), repo_path
                ).replace(os.sep, "/")
                print("   ↳ Correcting path '" + target + "' → '" + real_rel + "'")
                p["target_file"] = real_rel
                good.append(p)
                found = True
                break
        if not found:
            print("   ↳ Dropping patch — file not found: '" + target + "'")
    return good


def _find_nearest_pattern(file_content_lines: list, error_line: int, target_call: str) -> str:
    """
    Scans backwards from error_line to find the nearest block of lines containing
    target_call. Returns those lines with line numbers. This finds the actual
    existing usage pattern rather than random preceding code.

    For example, if target_call is "ut.Add" it finds the nearest previous ut.Add(...)
    block — showing the LLM exactly what syntax to follow.
    """
    if not target_call:
        # Fall back to raw preceding lines
        start = max(0, error_line - 15)
        end   = max(0, error_line - 1)
        return "".join(
            str(i + 1) + " | " + file_content_lines[i]
            for i in range(start, end)
        )

    # Scan backwards for the nearest block containing target_call
    search_from = min(error_line - 1, len(file_content_lines) - 1)
    block_end   = -1
    for i in range(search_from, max(0, search_from - 80), -1):
        if target_call in file_content_lines[i]:
            block_end = i
            break

    if block_end == -1:
        # target_call not found — return raw preceding lines
        start = max(0, error_line - 12)
        return "".join(
            str(i + 1) + " | " + file_content_lines[i]
            for i in range(start, error_line - 1)
        )

    # Return the block from block_end - 2 to block_end + 2 for context
    start = max(0, block_end - 2)
    end   = min(len(file_content_lines), block_end + 3)
    return "".join(
        str(i + 1) + " | " + file_content_lines[i]
        for i in range(start, end)
    )


def _detect_call_pattern(file_path: str) -> str:
    """
    Detects the dominant function-call pattern in a file so the pattern finder
    can look for the right thing. Handles common Go patterns:
      - translations files: "ut.Add("
      - registration maps:  "bakedInValidators[" or just the map key pattern
      - validator funcs:    "func is" or "func has"
    """
    fname = os.path.basename(file_path).lower()
    path_lower = file_path.lower()
    if "translat" in path_lower or fname in ("en.go", "zh.go", "fr.go", "de.go"):
        return "ut.Add("
    if "baked_in" in fname:
        return "bakedInValidators["
    if "regexes" in fname:
        return "= regexp.MustCompile("
    return ""


def _analyze_error(error_log: str, patch: "FilePatch", aci: "AgentComputerInterface") -> dict:
    """
    Converts a raw compiler/gofmt error into a structured analysis dict with:
      - error_type:   'syntax' | 'undefined' | 'type' | 'other'
      - error_msg:    single clean line describing what is wrong (no file/line prefix)
      - error_line:   integer line number or None
      - pattern:      nearest SAME-CALL-PATTERN block before the error line
                      (not random preceding lines — the actual call pattern to follow)
      - instruction:  one short surgical sentence: what to fix and how

    Design rules:
    - Never include raw error text in the instruction — classify it first
    - Pattern is always the nearest identical call pattern, not arbitrary context
    - Instruction is max 2 sentences — anything longer increases hallucination risk
    """
    error_line = extract_error_line(error_log)

    # ── Classify ────────────────────────────────────────────────────────────
    undef_name = ""
    # Detect LLM outputting top-level declarations (type/func/var) inside function bodies.
    # Symptoms: "expected '}'" before a type/func keyword, or "found 'type'"/"found 'func'"
    # at a position that should be inside an existing block.
    _toplevel_signals = (
        ("found 'type'" in error_log or "found 'func'" in error_log or
         "found 'var'" in error_log or "found 'const'" in error_log)
        and ("expected" in error_log or "found" in error_log)
    )
    if _toplevel_signals:
        error_type = "toplevel"
    elif "missing import" in error_log or "missing ',' in argument" in error_log:
        error_type = "import"
    elif "SYNTAX FAIL" in error_log or "expected" in error_log or "illegal" in error_log:
        error_type = "syntax"
    elif "undefined:" in error_log:
        error_type = "undefined"
        m = re.search(r"undefined:\s*(\S+)", error_log)
        undef_name = m.group(1) if m else "unknown"
    elif "cannot use" in error_log or "type mismatch" in error_log or "cannot convert" in error_log:
        error_type = "type"
    else:
        error_type = "other"

    # ── Clean error message — strip file:line: prefix noise ─────────────────
    first_meaningful = ""
    for raw_line in error_log.strip().split("\n"):
        m = re.search(r":\d+:\d+:\s*(.+)$", raw_line)
        if m:
            first_meaningful = m.group(1).strip()
            break
    if not first_meaningful:
        first_meaningful = error_log.strip().split("\n")[0][:100]

    # ── Find the nearest same-call pattern before the error ─────────────────
    # Read the actual file lines so we can scan them directly
    try:
        safe_path = aci._resolve_safe_path(patch.target_file)
        with open(safe_path, encoding="utf-8", errors="ignore") as f:
            file_lines = f.readlines()
    except Exception:
        file_lines = []

    anchor_line = error_line or patch.end_line
    call_pattern = _detect_call_pattern(patch.target_file)

    if file_lines and anchor_line:
        pattern = _find_nearest_pattern(file_lines, anchor_line, call_pattern)
    elif patch.start_line > 5:
        pattern = aci.view_file_range(
            patch.target_file,
            max(1, patch.start_line - 12),
            patch.start_line - 1,
        )
    else:
        pattern = ""

    # ── Build surgical instruction ───────────────────────────────────────────
    if error_type == "toplevel":
        instruction = (
            "You output a top-level Go declaration (type/func/var/const) inside "
            "a location that is already inside a struct, interface, or function body. "
            "Output ONLY the inner body lines — method implementations, field entries, "
            "or statement lines — NOT a new type or func declaration. "
            "Study the PATTERN below to see what lines belong at this location."
        )
    elif error_type == "import":
        instruction = (
            "Your output included a package declaration or import block. "
            "Output ONLY the function/method body lines for the target range. "
            "NEVER output 'package X', 'import (...)', or file headers — "
            "those already exist in the file. Adding them again breaks the syntax."
        )
    elif error_type == "syntax":
        if call_pattern == "ut.Add(":
            instruction = (
                "Syntax error in translation call. "
                "Use the EXACT same ut.Add(...) call signature as the pattern below — "
                "same argument count, same string quoting style, same {0} placeholder format."
            )
        else:
            instruction = (
                "Syntax error: '" + first_meaningful + "'. "
                "Match the call syntax in the PATTERN below exactly — "
                "same delimiters, same argument format, same indentation."
            )
    elif error_type == "undefined":
        instruction = (
            "'" + undef_name + "' is not defined. "
            "Use a name that already exists in the file or define it in this patch."
        )
    elif error_type == "type":
        instruction = (
            "Type mismatch. "
            "Check the PATTERN below for the correct type used in this call."
        )
    else:
        instruction = (
            "Error: '" + first_meaningful + "'. "
            "Match the PATTERN below exactly."
        )

    return {
        "error_type":  error_type,
        "error_msg":   first_meaningful,
        "error_line":  error_line,
        "pattern":     pattern,
        "instruction": instruction,
    }


def _expand_to_symbol_boundary(
    patch: dict,
    repo_path: str,
    indexer,
    repo_name: str,
) -> tuple[int, int] | None:
    """
    When the planner targets a suspiciously narrow line range (< 3 lines),
    this finds the nearest enclosing function/method in the SQLite symbol index
    and returns its (start_line, end_line) instead.

    This prevents the LLM from being asked to replace a blank line or a
    function signature without its body, which causes gofmt failures.
    """
    target_line = patch.get("start_line", 0)
    target_file = patch.get("target_file", "")
    if not target_file or not target_line:
        return None

    try:
        # Walk the repo to find all symbols in the target file
        import sqlite3
        db_path = os.path.join(repo_path, "..", ".cache", "state_cache.db")
        db_path = os.path.normpath(db_path)
        if not os.path.exists(db_path):
            return None

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT start_line, end_line FROM repo_symbols
                WHERE repo_name = ? AND file_path = ?
                AND symbol_type IN ('function', 'method')
                ORDER BY start_line
            """, (repo_name, target_file))
            symbols = [(row["start_line"], row["end_line"]) for row in cursor.fetchall()]

        if not symbols:
            return None

        # Find the symbol whose range contains target_line
        for start, end in symbols:
            if start <= target_line <= end:
                return (start, end)

        # No exact match — find the nearest symbol by proximity
        nearest = min(symbols, key=lambda s: abs(s[0] - target_line))
        return nearest

    except Exception:
        return None


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
        self._run_ts: int = int(time.time())  # fixed at start — unique per re-run

    # ── Branch commit + transfer ──────────────────────────────────────────────

    def _commit_and_transfer_branch(
        self,
        wt_workspace: str,
        branch_name: str,
        hypothesis: FixHypothesis,
    ) -> bool:
        """
        Stages ALL modified files, commits, then transfers the branch to the
        main repo so it survives worktree cleanup and can be pushed.
        """
        files_changed = "\n".join(
            "  - " + p.target_file + " (" + p.description + ")"
            for p in hypothesis.patches
        )
        commit_msg = (
            "fix: " + hypothesis.title + "\n\n"
            "Applied by Sentinel Engine.\nFiles changed:\n" + files_changed
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
            return False
        commit_hash = hash_res.stdout.strip()

        wt_normalized = wt_workspace.replace(os.sep, "/")
        fetch_res = subprocess.run(
            ["git", "fetch", wt_normalized, branch_name + ":" + branch_name],
            cwd=self.repo_path, capture_output=True, text=True
        )
        if fetch_res.returncode == 0:
            print("  ✅ Branch '" + branch_name + "' transferred to main repo.")
            return True

        create_res = subprocess.run(
            ["git", "branch", "-f", branch_name, commit_hash],
            cwd=self.repo_path, capture_output=True, text=True
        )
        if create_res.returncode == 0:
            print("  ✅ Branch '" + branch_name + "' created via hash fallback.")
            return True

        print("  ❌ Branch transfer failed.")
        return False

    # ── Heal prompt builder ───────────────────────────────────────────────────

    def _build_heal_prompt(
        self,
        patch: FilePatch,
        bad_code: str,
        error_log: str,
        aci: AgentComputerInterface,
        all_patches: list | None = None,
    ) -> str:
        """
        Builds a SHORT, SURGICAL heal prompt.

        Design principles:
        1. Never dump raw error text — parse it into a structured analysis first
        2. Show the PATTERN (existing similar code) not the broken insertion point
        3. One clear instruction: "write code exactly like the pattern, but for X"
        4. Cross-file identity check only when relevant (undefined errors)
        5. Keep total prompt under ~500 tokens — shorter = less hallucination
        """
        analysis = _analyze_error(error_log, patch, aci)

        mode_instruction = (
            "Insert new code AFTER line " + str(patch.end_line) + ". "
            "Do NOT reproduce existing lines."
            if patch.patch_mode == "insert_after"
            else "Replace lines " + str(patch.start_line)
            + " to " + str(patch.end_line) + "."
        )

        # Cross-file identifiers — only include for 'undefined' errors
        cross_ctx = ""
        if analysis["error_type"] == "undefined" and all_patches and len(all_patches) > 1:
            others = [
                "  - " + p.target_file + ": " + p.description
                for p in all_patches
                if p.description != patch.description
            ]
            if others:
                cross_ctx = (
                    "\nOTHER PATCHES (use EXACT same identifier names):\n"
                    + "\n".join(others) + "\n"
                )

        return (
            "Go patch correction. Output ONLY raw Go code.\n\n"
            "FILE: " + patch.target_file + "\n"
            "TASK: " + patch.description + "\n"
            "MODE: " + patch.patch_mode
            + " | TARGET: lines " + str(patch.start_line)
            + "-" + str(patch.end_line) + "\n\n"
            + cross_ctx
            + "WHAT TO FIX: " + analysis["instruction"] + "\n\n"
            "CORRECT PATTERN IN THIS FILE:\n"
            + analysis["pattern"] + "\n\n"
            "YOUR BROKEN CODE:\n"
            + bad_code + "\n\n"
            "STRICT RULES:\n"
            "- Output ONLY the replacement lines. No package declarations.\n"
            "- NEVER output import blocks or file headers — they already exist.\n"
            "- Match indentation from the pattern above exactly.\n"
            "Raw Go only. No markdown. No explanation."
        )

    # ── Single patch applier ──────────────────────────────────────────────────

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
        Generates and applies one FilePatch.
        Returns (success, result_message, code_written).
        """
        # Wide context window: 15 lines before and after the target range.
        # This gives the LLM enough surrounding code to understand where it is
        # in the file, which prevents it from generating package/import headers
        # to orient itself.
        current_code = aci.view_file_range(
            patch.target_file,
            max(1, patch.start_line - 15),
            patch.end_line + 15,
        )

        if not retry_error_log:
            # Cycle 1: fresh generation
            task = (
                "PATCH MODE: INSERT — insert NEW code AFTER line "
                + str(patch.end_line) + ". Do NOT reproduce existing lines."
                if patch.patch_mode == "insert_after"
                else "PATCH MODE: REPLACE — write the complete replacement for lines "
                + str(patch.start_line) + " to " + str(patch.end_line) + "."
            )
            prompt = (
                "You are a Principal Go Software Engineer.\n\n"
                "TASK: " + patch.description + "\n"
                "FILE: " + patch.target_file + "\n"
                "TARGET LINES: " + str(patch.start_line) + "-" + str(patch.end_line) + "\n\n"
                "CURRENT CODE AT TARGET LOCATION:\n" + current_code + "\n\n"
                + task + "\n\n"
                "STRICT RULES:\n"
                "- Output ONLY the code for the target lines. Nothing else.\n"
                "- NEVER output package declarations, import blocks, or file headers.\n"
                "  Those already exist in the file. Adding them again corrupts the file.\n"
                "- Preserve exact indentation from the context shown above.\n"
                "- Output must pass gofmt with zero errors."
            )
        else:
            # Cycle 2: structured self-healing
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
        raw_text = gen_res.text if gen_res and gen_res.text else ""
        code_written = clean_llm_code_output(raw_text)
        if not code_written:
            return False, "LLM returned empty response — cannot apply patch.", ""

        if patch.patch_mode == "insert_after":
            result = aci.insert_after_line(patch.target_file, patch.end_line, code_written)
        else:
            result = aci.apply_code_patch(
                patch.target_file, patch.start_line, patch.end_line, code_written
            )

        success = "⚠️" not in result
        return success, result, code_written

    # ── Core track executor ───────────────────────────────────────────────────

    async def execute_hypothesis_track(
        self,
        track_id: str,
        hypothesis: FixHypothesis,
        retry_context: dict | None = None,
    ) -> dict:
        """
        Runs one complete hypothesis track:
        1. Fresh Git Worktree on a collision-free branch
        2. Apply ALL patches sequentially (continue even if one fails — collect all bad_codes)
        3. go vet + go test across the entire worktree
        4. If all pass: commit every changed file and transfer branch to main repo

        retry_context (Cycle 2):
          - error_log:  full diagnostics from Cycle 1 (go vet / gofmt)
          - bad_codes:  dict[patch_index → broken code written in Cycle 1]
        """
        cycle       = 2 if retry_context else 1
        branch_name = make_branch_name(self._issue_number, track_id, cycle, self._run_ts)

        wt_manager   = WorktreeManager(self.repo_path)
        wt_workspace = None

        try:
            wt_workspace = wt_manager.create_hypothesis_worktree(track_id, branch_name)
            aci = AgentComputerInterface(base_workspace_path=wt_workspace)

            all_patch_success = True
            first_failure_msg = ""
            bad_codes: dict[int, str] = {}
            patch_errors: dict[int, str] = {}

            for idx, patch in enumerate(hypothesis.patches):
                label = (
                    "[" + track_id + "][patch "
                    + str(idx + 1) + "/" + str(len(hypothesis.patches)) + "]"
                )
                print("📝 " + label + " Applying (" + patch.patch_mode + ") → " + patch.target_file)

                # Every patch gets the full error log in Cycle 2 —
                # cross-patch errors (undefined: X) require all patches to see the same diagnostic
                retry_bad = (retry_context or {}).get("bad_codes", {}).get(idx, "")
                # Use per-patch error if available — more precise than the global error_log
                per_patch_errors = (retry_context or {}).get("patch_errors", {})
                retry_err = (
                    per_patch_errors.get(idx)
                    or (retry_context or {}).get("error_log", "")
                    or ""
                )

                ok, msg, code = await self._apply_single_patch(
                    patch, aci, track_id,
                    retry_bad_code=retry_bad,
                    retry_error_log=retry_err,
                    all_patches=hypothesis.patches,
                )
                bad_codes[idx] = code

                if not ok:
                    print("  ⚠️  Patch " + str(idx + 1) + " failed: " + msg[:120])
                    if all_patch_success:
                        first_failure_msg = msg
                    all_patch_success = False
                    # Store per-patch error so Cycle 2 gets the exact right error
                    # for each patch, not a single global error applied to all
                    patch_errors[idx] = msg
                    # Do NOT break — keep applying remaining patches so all bad_codes
                    # are collected, enabling full Cycle 2 self-healing context

            if not all_patch_success:
                return {
                    "track_id":    track_id,
                    "passed":      False,
                    "diagnostics": first_failure_msg,
                    "bad_codes":   bad_codes,
                    "patch_errors": patch_errors,
                    "branch":      branch_name,
                    "hypothesis":  hypothesis,
                }

            tester     = AsyncTestSuiteRunner(wt_workspace)
            matrix_res = await tester.execute_verification_matrix(track_id)

            if matrix_res["passed"]:
                self._commit_and_transfer_branch(wt_workspace, branch_name, hypothesis)
            else:
                # Surface go vet/test output as the error_log for Cycle 2
                # (cross-patch compile errors only appear here, not in per-patch gofmt)
                first_failure_msg = matrix_res.get("diagnostics", "")
                matrix_res["diagnostics"] = first_failure_msg

            matrix_res["branch"]    = branch_name
            matrix_res["hypothesis"] = hypothesis
            matrix_res["bad_codes"] = bad_codes
            return matrix_res

        except Exception as e:
            import traceback
            return {
                "track_id":    track_id,
                "passed":      False,
                "diagnostics": "Track Runtime Exception: " + str(e) + "\n" + traceback.format_exc(),
                "bad_codes":   bad_codes if "bad_codes" in dir() else {},
                "branch":      branch_name if "branch_name" in dir() else "unknown",
                "hypothesis":  hypothesis,
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

        search_terms = {self.repo_name, analysis.get("target_package", "")}
        for f in analysis.get("potential_files", []):
            base = os.path.splitext(os.path.basename(f))[0]
            if base:
                search_terms.add(base)
        search_terms.update(re.findall(r"[A-Za-z]{4,}", analysis.get("symptom", ""))[:3])

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
        # Build file snippets for the files most likely to need patching
        # This shows the LLM actual code content so it can pick correct line ranges
        file_snippets = ""
        snippet_files = list({
            f for f in analysis.get("potential_files", [])
            if f and os.path.exists(os.path.join(self.repo_path, f))
        })[:3]
        for sf in snippet_files:
            try:
                full = os.path.join(self.repo_path, sf)
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
                numbered = "".join(
                    str(i + 1).rjust(4) + " | " + l
                    for i, l in enumerate(lines[:200])  # first 200 lines max
                )
                file_snippets += "\n--- " + sf + " (first 200 lines) ---\n" + numbered
            except Exception:
                pass

        planner_prompt = (
            "You are a Principal Go Software Engineer designing fix strategies.\n\n"
            "ISSUE SYMPTOM: " + str(analysis.get("symptom")) + "\n"
            "REPRODUCTION NOTES: " + str(analysis.get("reproduction_steps")) + "\n\n"
            "INDEXED SYMBOLS (file_path, symbol_name, start_line, end_line):\n"
            + symbol_ctx + "\n\n"
            "REAL FILES IN REPO (use ONLY these in target_file — never invent paths):\n"
            + file_inventory + "\n\n"
            + ("ACTUAL FILE CONTENT (use these line numbers for start_line/end_line):\n"
               + file_snippets + "\n\n" if file_snippets else "")
            + "SCHEMA: Each hypothesis has a 'patches' list — one FilePatch per file "
            "that needs changing. Include ALL files required.\n\n"
            "CRITICAL RULES:\n"
            "1. target_file must be from REAL FILES list. Never invent paths.\n"
            "2. start_line and end_line MUST span the COMPLETE function or block to change.\n"
            "   Never target a blank line or a single line — always cover the entire\n"
            "   function signature through its closing brace.\n"
            "3. patch_mode:\n"
            "   'replace' — modifying EXISTING logic (must cover full function body)\n"
            "   'insert_after' — adding NEW code that doesn't exist yet\n"
            "4. When adding a new validator, include at minimum:\n"
            "   a) A patch that adds the validator FUNCTION\n"
            "   b) A patch that adds the registration entry in the validator MAP\n"
            "   Do NOT include patches for translation/documentation files — "
            "those are optional and frequently cause syntax errors in CI.\n"
            "5. description must clearly state what this individual change does.\n"
            "Output valid JSON with exactly 2 hypotheses."
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

        for h in hypotheses_raw:
            h["patches"] = _validate_and_correct_patches(h.get("patches", []), self.repo_path)
            # Expand any suspiciously narrow line ranges — a patch covering fewer
            # than 3 lines almost always means the planner targeted a blank line
            # or only the function signature instead of the whole function body.
            for p in h.get("patches", []):
                if p.get("patch_mode", "replace") == "replace":
                    span = p.get("end_line", 0) - p.get("start_line", 0)
                    if span < 3:
                        # Find the enclosing symbol from the index
                        expanded = _expand_to_symbol_boundary(
                            p, self.repo_path, self.indexer, self.repo_name
                        )
                        if expanded:
                            p["start_line"] = expanded[0]
                            p["end_line"]   = expanded[1]
                            print("  📐 Expanded narrow patch range → lines "
                                  + str(expanded[0]) + "-" + str(expanded[1])
                                  + " in " + p.get("target_file", ""))

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

        # Phase 5: Cycle 2 — Structured Self-Healing
        results = c1_results
        if not winning_track:
            print("\n🚨 Cycle 1 Failed. Launching Self-Healing Cycle 2...")
            print("🏎️  Deploying Cycle 2 concurrently...")
            c2_results = await asyncio.gather(
                self.execute_hypothesis_track(
                    "TRACK_ALPHA",
                    FixHypothesis(**hypotheses_raw[0]),
                    retry_context={
                        "error_log":    c1_results[0]["diagnostics"],
                        "bad_codes":    c1_results[0].get("bad_codes", {}),
                        "patch_errors": c1_results[0].get("patch_errors", {}),
                    },
                ),
                self.execute_hypothesis_track(
                    "TRACK_BETA",
                    FixHypothesis(**hypotheses_raw[1]),
                    retry_context={
                        "error_log":    c1_results[1]["diagnostics"],
                        "bad_codes":    c1_results[1].get("bad_codes", {}),
                        "patch_errors": c1_results[1].get("patch_errors", {}),
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

        issue_meta    = triage_data.get("meta", {})
        issue_number  = issue_meta.get("issue_number", "?")
        branch        = winning_track["branch"]
        hypothesis    = winning_track["hypothesis"]
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