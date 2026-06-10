import os
import re
import json
import time
import asyncio
import subprocess
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from ingestion.triage import IssueTriageEngine, run_admission_checks
from ingestion.pr_generator import PRGenerator
from indexing.indexer import RepositoryIndexer
from aci.tools import AgentComputerInterface
from verification.worktree import WorktreeManager
from verification.tester import AsyncTestSuiteRunner
from github_client import GitHubClient


# ── Schema ────────────────────────────────────────────────────────────────────

LLM_CAP_SMALL = 10
LLM_CAP_MEDIUM = 14
MICRO_HEAL_FLASH_SMALL = 3
MICRO_HEAL_FLASH_MEDIUM = 2


class FilePatch(BaseModel):
    """One atomic file change — one entry in a multi-file patch set."""
    target_file: str = Field(
        description="Relative path of the file to modify (must exist in the repo)."
    )
    start_line: int = Field(description="First line of the region to operate on.")
    end_line: int = Field(description="Last line of the region to operate on.")
    anchor_symbol: str = Field(
        default="",
        description="Function/method name to re-resolve from SQLite before applying.",
    )
    patch_mode: str = Field(
        default="replace",
        description="'replace' only for bug fixes — overwrite lines start_line..end_line.",
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
        description="1 or 2 distinct fix strategies (1 for small bugs, 2 for medium)."
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


def _coalesce_replace_per_file(patches: list[dict]) -> list[dict]:
    """Keep at most one replace patch per file — prevents stale line drift."""
    seen_replace: set[str] = set()
    result = []
    for p in patches:
        mode = p.get("patch_mode", "replace")
        fname = p.get("target_file", "")
        if mode == "replace" and fname in seen_replace:
            print("   ↳ Dropping duplicate replace on '" + fname + "'")
            continue
        if mode == "replace":
            seen_replace.add(fname)
        result.append(p)
    return result


def _enforce_bugfix_patches(patches: list[dict], complexity: str) -> list[dict]:
    """Bug-fix mode: replace only, enforce file/patch count limits."""
    cleaned = []
    for p in patches:
        if p.get("patch_mode", "replace") != "replace":
            print("   ↳ Dropping insert_after patch (bug-fix mode): " + p.get("target_file", ""))
            continue
        cleaned.append(p)
    cleaned = _coalesce_replace_per_file(cleaned)
    max_files = 1 if complexity == "small" else 2
    if len(cleaned) > max_files:
        print("   ↳ Trimming to " + str(max_files) + " patch(es) for " + complexity + " tier")
        cleaned = cleaned[:max_files]
    return cleaned


def _read_error_context(aci: "AgentComputerInterface", error_log: str, patch: "FilePatch") -> str:
    """Reads ±5 lines around the compiler error line for precision healing."""
    err_line = extract_error_line(error_log) or patch.end_line
    err_file = extract_error_file(error_log) or patch.target_file
    start = max(1, err_line - 5)
    end = err_line + 5
    return aci.view_file_range(err_file, start, end)


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
    Expands narrow line ranges to enclosing symbol boundaries via the indexer.
    """
    target_line = patch.get("start_line", 0)
    target_file = patch.get("target_file", "")
    anchor = patch.get("anchor_symbol", "")
    if not target_file:
        return None

    if anchor:
        bounds = indexer.resolve_symbol(repo_name, target_file, anchor)
        if bounds:
            return bounds

    if not target_line:
        return None

    try:
        import sqlite3
        with sqlite3.connect(indexer.db_path) as conn:
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

        for start, end in symbols:
            if start <= target_line <= end:
                return (start, end)

        return min(symbols, key=lambda s: abs(s[0] - target_line))

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
        self._llm_calls: int = 0
        self._complexity: str = "medium"
        self._failure_memory: list[dict] = []

    def _llm_generate(self, model: str, contents: str, config=None):
        """Tracks LLM call budget; raises when cap exceeded."""
        cap = LLM_CAP_SMALL if self._complexity == "small" else LLM_CAP_MEDIUM
        if self._llm_calls >= cap:
            raise RuntimeError("LLM call budget exhausted (" + str(cap) + " calls)")
        self._llm_calls += 1
        if config:
            return self.client.models.generate_content(
                model=model, contents=contents, config=config
            )
        return self.client.models.generate_content(model=model, contents=contents)

    def _reanchor_patch(self, patch: FilePatch) -> FilePatch:
        """Re-resolve line numbers from SQLite before applying a patch."""
        anchor = (patch.anchor_symbol or "").strip()
        if not anchor:
            return patch
        bounds = self.indexer.resolve_symbol(
            self.repo_name, patch.target_file, anchor
        )
        if bounds:
            return patch.model_copy(update={
                "start_line": bounds[0],
                "end_line": bounds[1],
            })
        return patch

    def _verify_patch(self, patch: FilePatch, aci: AgentComputerInterface) -> tuple[bool, str]:
        """gofmt + go build on the changed package."""
        syntax = aci.run_local_syntax_check(patch.target_file)
        if "FAIL" in syntax:
            return False, syntax
        build = aci.run_package_build(patch.target_file)
        if "FAIL" in build:
            return False, build
        return True, "Patch verified (gofmt + build)."

    def _revert_files(self, wt_workspace: str, files: list[str]):
        """Restore touched files after unhealable patch failure."""
        for fpath in files:
            subprocess.run(
                ["git", "checkout", "--", fpath],
                cwd=wt_workspace, capture_output=True, text=True,
            )

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
        use_pro: bool = False,
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
            task = (
                "PATCH MODE: REPLACE — write the complete replacement for lines "
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
            err_ctx = _read_error_context(aci, retry_error_log, patch)
            prompt = self._build_heal_prompt(
                patch=patch,
                bad_code=retry_bad_code,
                error_log=retry_error_log,
                aci=aci,
                all_patches=all_patches,
            )
            prompt += "\n\nERROR CONTEXT (lines around failure):\n" + err_ctx

        model = "gemini-2.5-pro" if use_pro else "gemini-2.5-flash"
        gen_res = self._llm_generate(model=model, contents=prompt)
        raw_text = gen_res.text if gen_res and gen_res.text else ""
        code_written = clean_llm_code_output(raw_text)
        if not code_written:
            return False, "LLM returned empty response — cannot apply patch.", ""

        result = aci.apply_code_patch(
            patch.target_file, patch.start_line, patch.end_line, code_written
        )

        success = "⚠️" not in result
        return success, result, code_written

    async def _apply_patch_with_micro_heal(
        self,
        patch: FilePatch,
        aci: AgentComputerInterface,
        track_id: str,
        all_patches: list[FilePatch] | None = None,
    ) -> tuple[bool, str, str]:
        """
        Apply one patch with inline micro-heal: up to N Flash attempts,
        then optional Pro escalation for small bugs.
        """
        flash_limit = (
            MICRO_HEAL_FLASH_SMALL
            if self._complexity == "small"
            else MICRO_HEAL_FLASH_MEDIUM
        )
        bad_code = ""
        error_log = ""
        last_msg = ""

        for attempt in range(flash_limit + 1):
            use_pro = attempt == flash_limit

            ok, msg, code = await self._apply_single_patch(
                patch, aci, track_id,
                retry_bad_code=bad_code,
                retry_error_log=error_log,
                all_patches=all_patches,
                use_pro=use_pro,
            )
            bad_code = code
            last_msg = msg

            if not ok:
                error_log = msg
                continue

            verified, verify_msg = self._verify_patch(patch, aci)
            if verified:
                return True, msg, code

            error_log = verify_msg
            bad_code = code

        return False, last_msg or error_log, bad_code

    # ── Core track executor ───────────────────────────────────────────────────

    async def execute_hypothesis_track(
        self,
        track_id: str,
        hypothesis: FixHypothesis,
        cycle: int = 1,
    ) -> dict:
        """
        Runs one hypothesis track:
        1. Git worktree on a unique branch
        2. Re-anchor + apply patches sequentially with micro-heal
        3. Stop and revert on first unhealable patch failure
        4. Full go vet + go test if all patches pass
        """
        branch_name = make_branch_name(self._issue_number, track_id, cycle, self._run_ts)

        wt_manager   = WorktreeManager(self.repo_path)
        wt_workspace = None
        bad_codes: dict[int, str] = {}
        touched_files: list[str] = []

        try:
            wt_workspace = wt_manager.create_hypothesis_worktree(track_id, branch_name)
            aci = AgentComputerInterface(base_workspace_path=wt_workspace)

            for idx, raw_patch in enumerate(hypothesis.patches):
                patch = self._reanchor_patch(raw_patch)
                label = (
                    "[" + track_id + "][patch "
                    + str(idx + 1) + "/" + str(len(hypothesis.patches)) + "]"
                )
                sym = patch.anchor_symbol or "(no anchor)"
                print(
                    "📝 " + label + " → " + patch.target_file
                    + " [" + sym + "] lines " + str(patch.start_line)
                    + "-" + str(patch.end_line)
                )

                ok, msg, code = await self._apply_patch_with_micro_heal(
                    patch, aci, track_id, all_patches=hypothesis.patches,
                )
                bad_codes[idx] = code
                touched_files.append(patch.target_file)

                if not ok:
                    print("  ❌ Patch " + str(idx + 1) + " unhealable: " + msg[:150])
                    self._revert_files(wt_workspace, touched_files)
                    self._failure_memory.append({
                        "track": track_id,
                        "file": patch.target_file,
                        "symbol": patch.anchor_symbol,
                        "error": msg[:500],
                        "code_attempted": code[:300] if code else "",
                        "cycle": cycle,
                    })
                    return {
                        "track_id": track_id,
                        "passed": False,
                        "diagnostics": msg,
                        "bad_codes": bad_codes,
                        "branch": branch_name,
                        "hypothesis": hypothesis,
                    }

            tester     = AsyncTestSuiteRunner(wt_workspace)
            matrix_res = await tester.execute_verification_matrix(track_id)

            if matrix_res["passed"]:
                self._commit_and_transfer_branch(wt_workspace, branch_name, hypothesis)
            else:
                self._failure_memory.append({
                    "track": track_id,
                    "file": ", ".join(p.target_file for p in hypothesis.patches),
                    "symbol": "",
                    "error": matrix_res.get("diagnostics", "")[:500],
                    "code_attempted": "",
                    "cycle": cycle,
                })

            matrix_res["branch"]     = branch_name
            matrix_res["hypothesis"] = hypothesis
            matrix_res["bad_codes"]  = bad_codes
            return matrix_res

        except Exception as e:
            import traceback
            if wt_workspace and touched_files:
                self._revert_files(wt_workspace, touched_files)
            return {
                "track_id": track_id,
                "passed": False,
                "diagnostics": "Track Runtime Exception: " + str(e) + "\n" + traceback.format_exc(),
                "bad_codes": bad_codes,
                "branch": branch_name,
                "hypothesis": hypothesis,
            }
        finally:
            if wt_workspace and wt_manager:
                wt_manager.cleanup_worktree(track_id)

    def _build_planner_prompt(
        self,
        analysis: dict,
        symbol_ctx: str,
        file_inventory: str,
        file_snippets: str,
        num_hypotheses: int,
        max_files: int,
    ) -> str:
        return (
            "You are a Principal Go Software Engineer designing BUG FIX strategies.\n\n"
            "ISSUE SYMPTOM: " + str(analysis.get("symptom")) + "\n"
            "ANCHOR SYMBOL: " + str(analysis.get("anchor_symbol", "")) + "\n"
            "TARGET FILE: " + str(analysis.get("target_file", "")) + "\n"
            "REPRODUCTION NOTES: " + str(analysis.get("reproduction_steps")) + "\n\n"
            "INDEXED SYMBOLS (file_path, symbol_name, start_line, end_line):\n"
            + symbol_ctx + "\n\n"
            "REAL FILES IN REPO (use ONLY these in target_file — never invent paths):\n"
            + file_inventory + "\n\n"
            + ("ACTUAL FILE CONTENT (use these line numbers for start_line/end_line):\n"
               + file_snippets + "\n\n" if file_snippets else "")
            + "SCHEMA: Each hypothesis has a 'patches' list. "
            + "Output exactly " + str(num_hypotheses) + " hypothesis/hypotheses.\n\n"
            "CRITICAL RULES (bug fix only):\n"
            "1. target_file must be from REAL FILES list. Never invent paths.\n"
            "2. Set anchor_symbol on every patch — the exact function/method to modify.\n"
            "3. start_line/end_line MUST span the COMPLETE function or method body.\n"
            "4. patch_mode MUST be 'replace' only — modify existing logic, never add new APIs.\n"
            "5. Max " + str(max_files) + " file(s) per hypothesis. "
            "Max 1 replace patch per file — never patch the same file twice.\n"
            "6. For 2-file fixes: patch the callee/definition file first, caller second.\n"
            "7. description must clearly state what this individual change does.\n"
            "Output valid JSON."
        )

    def _replan(
        self,
        analysis: dict,
        symbol_ctx: str,
        file_inventory: str,
        file_snippets: str,
        num_hypotheses: int,
        max_files: int,
    ) -> list[dict] | None:
        """Generate a new blueprint informed by FailureMemory."""
        memory_text = json.dumps(self._failure_memory, indent=2)
        prompt = (
            self._build_planner_prompt(
                analysis, symbol_ctx, file_inventory, file_snippets,
                num_hypotheses, max_files,
            )
            + "\n\nPREVIOUS FAILED ATTEMPTS (do NOT repeat these approaches):\n"
            + memory_text
            + "\n\nOutput a DIFFERENT fix strategy than all previous attempts."
        )
        try:
            response = self._llm_generate(
                model="gemini-2.5-pro",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=StrategyBlueprint,
                    temperature=0.3,
                ),
            )
            blueprint = json.loads(response.text)
            return blueprint.get("hypotheses", [])
        except Exception as e:
            print("  ⚠️  Replan failed: " + str(e))
            return None

    def _prepare_hypotheses(
        self, hypotheses_raw: list[dict], complexity: str, analysis: dict
    ) -> list[FixHypothesis]:
        """Validate paths, expand symbols, enforce bug-fix limits."""
        default_anchor = (analysis.get("anchor_symbol") or "").strip()
        prepared = []
        for h in hypotheses_raw:
            h["patches"] = _validate_and_correct_patches(
                h.get("patches", []), self.repo_path
            )
            h["patches"] = _enforce_bugfix_patches(h["patches"], complexity)
            for p in h.get("patches", []):
                if not p.get("anchor_symbol"):
                    p["anchor_symbol"] = default_anchor
                if p.get("patch_mode", "replace") == "replace":
                    span = p.get("end_line", 0) - p.get("start_line", 0)
                    if span < 3:
                        expanded = _expand_to_symbol_boundary(
                            p, self.repo_path, self.indexer, self.repo_name
                        )
                        if expanded:
                            p["start_line"] = expanded[0]
                            p["end_line"] = expanded[1]
            if h.get("patches"):
                prepared.append(FixHypothesis(**h))
        return prepared

    # ── Main pipeline ─────────────────────────────────────────────────────────

    async def run_pipeline(self, issue_url: str):
        print("====== STARTING SENTINEL ENGINE EXECUTION MATRIX ======\n")

        # Phase 1: Issue Triage
        triage_data        = self.triage_engine.process_issue(issue_url)
        analysis           = triage_data["analysis"]
        self._issue_number = triage_data.get("meta", {}).get("issue_number")
        self._llm_calls    = 1  # triage call
        print("🎯 Target Acquired: " + triage_data["raw_title"] + "\n")

        # Phase 2: AST Symbol Index + Admission Gate
        self.indexer.index_repository(self.repo_path, self.repo_name)

        run_admission_checks(
            analysis,
            triage_data.get("labels", []),
            self.repo_path,
            self.indexer,
            self.repo_name,
            issue_title=triage_data.get("raw_title", ""),
        )
        complexity = (analysis.get("complexity") or "out_of_scope").lower()
        self._complexity = complexity if complexity in ("small", "medium") else "medium"

        if not analysis.get("admitted"):
            reason = analysis.get("reject_reason") or "Issue not admitted."
            print("🚫 ADMISSION REJECTED: " + reason)
            print("   Complexity: " + str(analysis.get("complexity", "unknown")))
            print("   Exiting early — no planning or patching will run.")
            return False

        print(
            "✅ Issue admitted as " + self._complexity.upper() + " bug "
            "(confidence: " + str(analysis.get("confidence", "?")) + ")\n"
        )

        search_terms = {self.repo_name, analysis.get("target_package", "")}
        anchor = (analysis.get("anchor_symbol") or "").strip()
        if anchor:
            search_terms.add(anchor)
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

        # Build file snippets for planner context
        file_snippets = ""
        snippet_candidates = list({
            f for f in analysis.get("potential_files", [])
            if f and os.path.exists(os.path.join(self.repo_path, f))
        })
        target_file = (analysis.get("target_file") or "").strip()
        if target_file and target_file not in snippet_candidates:
            snippet_candidates.insert(0, target_file)
        for sf in snippet_candidates[:3]:
            try:
                full = os.path.join(self.repo_path, sf)
                with open(full, encoding="utf-8", errors="ignore") as fh:
                    lines = fh.readlines()
                numbered = "".join(
                    str(i + 1).rjust(4) + " | " + l
                    for i, l in enumerate(lines[:200])
                )
                file_snippets += "\n--- " + sf + " (first 200 lines) ---\n" + numbered
            except Exception:
                pass

        is_small = self._complexity == "small"
        num_hypotheses = 1 if is_small else 2
        max_files = 1 if is_small else 2
        max_replans = 1 if is_small else 2

        # Phase 3: Strategy Planning
        print("🤖 Generating fix blueprint(s) with Gemini 2.5 Pro...")
        planner_prompt = self._build_planner_prompt(
            analysis, symbol_ctx, file_inventory, file_snippets,
            num_hypotheses, max_files,
        )

        response = self._llm_generate(
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

        hypotheses_raw = blueprint.get("hypotheses", [])[:num_hypotheses]
        if not hypotheses_raw:
            print("❌ Planning Error: no hypotheses returned.")
            return False

        prepared = self._prepare_hypotheses(hypotheses_raw, self._complexity, analysis)
        if not prepared:
            print("❌ Planning Error: no valid patches after validation.")
            return False
        if is_small and any(len(h.patches) != 1 for h in prepared):
            prepared = [
                FixHypothesis(
                    title=h.title,
                    patches=h.patches[:1],
                )
                for h in prepared
            ]
        if not is_small and len(prepared) < 2:
            print("❌ Planning Error: medium bug requires 2 hypotheses.")
            return False

        for i, h in enumerate(prepared):
            track = "ALPHA" if i == 0 else "BETA"
            print(
                "   Track " + track + ": '" + h.title + "' ("
                + str(len(h.patches)) + " patch(es))"
            )

        winning_track = None
        results: list[dict] = []
        cycle = 1
        max_cycles = 1 + max_replans

        while cycle <= max_cycles and not winning_track:
            if cycle > 1:
                print("\n🔄 Replan cycle " + str(cycle) + " with FailureMemory...")
                replanned = self._replan(
                    analysis, symbol_ctx, file_inventory, file_snippets,
                    num_hypotheses, max_files,
                )
                if not replanned:
                    break
                prepared = self._prepare_hypotheses(
                    replanned, self._complexity, analysis
                )
                if not prepared:
                    break

            print(
                "\n🏎️  Deploying cycle " + str(cycle)
                + " track(s) across independent worktrees..."
            )
            if is_small:
                results = [await self.execute_hypothesis_track(
                    "TRACK_ALPHA", prepared[0], cycle=cycle,
                )]
            else:
                results = list(await asyncio.gather(
                    self.execute_hypothesis_track("TRACK_ALPHA", prepared[0], cycle=cycle),
                    self.execute_hypothesis_track("TRACK_BETA", prepared[1], cycle=cycle),
                ))

            winning_track = next((r for r in results if r["passed"]), None)
            cycle += 1

        # Phase 6: Results Summary
        print("\n🏁 --- CONCURRENT CONFLICT EVALUATION RUNTIME METRICS ---")
        for res in results:
            status = "🟩 PASSED ALL VERIFICATIONS" if res["passed"] else "🟥 FAILED SUITE"
            print("Result Vector -> Track: " + res["track_id"] + " | Status: " + status)
            if not res["passed"]:
                clean_diag = res["diagnostics"].strip().replace("\n", " ")
                print("   ↳ Diagnostics: " + clean_diag[:200] + "...")

        if not winning_track:
            print("\n❌ System Regression: All tracks and replans exhausted.")
            print("   LLM calls used: " + str(self._llm_calls)
                  + "/" + str(LLM_CAP_SMALL if is_small else LLM_CAP_MEDIUM))
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