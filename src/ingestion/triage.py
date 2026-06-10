import os
import re
import json
import urllib.request
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

REJECT_LABELS = frozenset({
    "security", "type/proposal", "needs-discussion",
    "question", "enhancement", "proposal",
})

ADMISSION_POLICY = """
ADMIT only small or medium bug fixes in existing Go code.
REJECT if ANY of these apply:
- Feature or proposal (new methods, new files, new public API)
- Security/CVE/auth/crypto vulnerability
- Architectural rewrite or large refactor (3+ files)
- Unclear maintainer direction (RFC, WIP, open debate, multiple conflicting approaches)
- Requires primarily new code (new functions/files as main deliverable)

SMALL bug: 1 file, 1 symbol, stack trace or clear repro, fix is a guard/logic correction.
MEDIUM bug: 1-2 files, clear bug (not feature), fix modifies existing logic.
OUT_OF_SCOPE: everything else.
"""


class TriageAnalysis(BaseModel):
    symptom: str = Field(
        description="Clear summary of what is broken (panic, wrong result, regression)."
    )
    target_package: str = Field(
        description="Package or subdirectory where the bug likely lives."
    )
    potential_files: list[str] = Field(
        description="Possible file paths from stack traces or descriptions."
    )
    reproduction_steps: str = Field(
        description="Minimal repro code or commands."
    )
    is_breaking_change_risk: bool = Field(
        description="True if fix risks altering backwards-compatible public APIs."
    )
    complexity: str = Field(
        description="'small', 'medium', or 'out_of_scope'."
    )
    confidence: float = Field(
        description="0.0-1.0 confidence this is a fixable bug in existing code."
    )
    anchor_symbol: str = Field(
        description="Exact function/method name to patch (from stack trace or repro)."
    )
    target_file: str = Field(
        description="Primary file path relative to repo root."
    )
    requires_new_code: bool = Field(
        description="True if fix requires new functions, files, or public API."
    )
    is_security_related: bool = Field(
        description="True if issue involves security, CVE, auth, or crypto."
    )
    is_architectural: bool = Field(
        description="True if fix requires architectural redesign or 3+ files."
    )
    maintainer_decision_unclear: bool = Field(
        description="True if maintainer direction is unclear or debated."
    )
    estimated_files: int = Field(
        description="Estimated files to change: 1 for small, 1-2 for medium."
    )
    has_clear_repro: bool = Field(
        description="True if issue has stack trace or minimal repro."
    )
    admitted: bool = Field(
        description="True only if this issue passes all admission criteria."
    )
    reject_reason: str = Field(
        default="",
        description="Why the issue was rejected; empty if admitted.",
    )


def parse_github_url(url: str) -> dict:
    """Parses a standard GitHub issue link to isolate metadata parameters."""
    pattern = r"github\.com/([^/]+)/([^/]+)/issues/(\d+)"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(
            "Invalid GitHub Issue URL format. "
            "Expected: https://github.com/owner/repo/issues/num"
        )

    return {
        "owner": match.group(1),
        "repo": match.group(2),
        "issue_number": int(match.group(3)),
    }


def fetch_github_issue(owner: str, repo: str, issue_number: int, token: str) -> dict:
    """Fetches the main payload of an issue from the official GitHub REST API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {token}")
    req.add_header("User-Agent", "Sentinel-Agent-Engine")
    req.add_header("Accept", "application/vnd.github.v3+json")

    try:
        with urllib.request.urlopen(req) as response:
            if response.status == 200:
                return json.loads(response.read().decode())
            raise RuntimeError(
                f"GitHub API returned HTTP {response.status} for issue fetch."
            )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Failed connecting to GitHub API: {e}. "
            "Check token permissions or issue privacy."
        )


def _repo_has_go_files(repo_path: str) -> bool:
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.endswith(".go"):
                return True
    return False


def _normalize_anchor_name(anchor: str) -> str:
    """Strip type qualifiers — 'Command.ExecuteC' → 'ExecuteC'."""
    anchor = (anchor or "").strip()
    if "." in anchor:
        anchor = anchor.rsplit(".", 1)[-1]
    return anchor


def _extract_file_line_hint(text: str) -> tuple[str, int] | None:
    """Parse 'command.go at lines 1111' or 'command.go#L1111' from issue text."""
    if not text:
        return None
    patterns = [
        r"([\w./\\-]+\.go)\s+at\s+lines?\s+(\d+)",
        r"([\w./\\-]+\.go)#L(\d+)",
        r"([\w./\\-]+\.go):(\d+):\d+",
        r"lines?\s+(\d+)[^\n]*\n[^\n]*([\w./\\-]+\.go)",
    ]
    for i, pat in enumerate(patterns):
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        if i == 3:
            return (os.path.basename(m.group(2)), int(m.group(1)))
        return (os.path.basename(m.group(1)), int(m.group(2)))
    return None


def _title_suggests_non_bug(title: str) -> str | None:
    """Reject feature/proposal issues from title prefixes."""
    t = (title or "").strip().lower()
    for prefix in ("feat:", "feat(", "feature:", "proposal:", "rfc:", "enhancement:"):
        if t.startswith(prefix):
            return f"Title indicates feature/proposal: '{title[:60]}'"
    return None


def run_admission_checks(
    analysis: dict,
    issue_labels: list[str],
    repo_path: str,
    indexer,
    repo_name: str,
    issue_title: str = "",
    issue_body: str = "",
) -> dict:
    """
    Code-level pre-flight after LLM triage + repo index.
    Updates admitted/reject_reason on analysis dict in place.
    """
    if not analysis.get("admitted", True):
        if not analysis.get("reject_reason"):
            analysis["reject_reason"] = "LLM triage rejected this issue."
        return analysis

    title_reject = _title_suggests_non_bug(issue_title)
    if title_reject:
        analysis["admitted"] = False
        analysis["reject_reason"] = title_reject
        return analysis

    label_names = {lbl.lower() for lbl in issue_labels}
    if label_names & REJECT_LABELS:
        matched = label_names & REJECT_LABELS
        analysis["admitted"] = False
        analysis["reject_reason"] = (
            f"GitHub labels indicate non-bug issue: {', '.join(sorted(matched))}"
        )
        return analysis

    if not _repo_has_go_files(repo_path):
        analysis["admitted"] = False
        analysis["reject_reason"] = "Repository contains no Go source files."
        return analysis

    complexity = (analysis.get("complexity") or "out_of_scope").lower()
    if complexity == "out_of_scope":
        analysis["admitted"] = False
        if not analysis.get("reject_reason"):
            analysis["reject_reason"] = "Issue classified as out of scope (not a small/medium bug)."
        return analysis

    reject_flags = [
        (analysis.get("requires_new_code"), "requires new code (features/new API)"),
        (analysis.get("is_security_related"), "security-sensitive issue"),
        (analysis.get("is_architectural"), "architectural change required"),
        (analysis.get("maintainer_decision_unclear"), "unclear maintainer direction"),
    ]
    for flag, reason in reject_flags:
        if flag:
            analysis["admitted"] = False
            analysis["reject_reason"] = f"Issue not admitted: {reason}."
            return analysis

    confidence = float(analysis.get("confidence") or 0)
    if complexity == "small" and confidence < 0.85:
        analysis["admitted"] = False
        analysis["reject_reason"] = (
            f"Small-bug confidence {confidence:.2f} below threshold 0.85."
        )
        return analysis
    if complexity == "medium" and confidence < 0.70:
        analysis["admitted"] = False
        analysis["reject_reason"] = (
            f"Medium-bug confidence {confidence:.2f} below threshold 0.70."
        )
        return analysis

    estimated = int(analysis.get("estimated_files") or 0)
    if complexity == "small" and estimated > 1:
        analysis["admitted"] = False
        analysis["reject_reason"] = f"Small bug estimated {estimated} files (max 1)."
        return analysis
    if complexity == "medium" and estimated > 2:
        analysis["admitted"] = False
        analysis["reject_reason"] = f"Medium bug estimated {estimated} files (max 2)."
        return analysis

    target_file = (analysis.get("target_file") or "").strip()
    if target_file:
        full = os.path.join(repo_path, target_file.replace("/", os.sep))
        if not os.path.isfile(full):
            analysis["admitted"] = False
            analysis["reject_reason"] = f"Target file not found in repo: {target_file}"
            return analysis

    anchor = _normalize_anchor_name(analysis.get("anchor_symbol") or "")
    if anchor:
        analysis["anchor_symbol"] = anchor

    bounds = None
    if anchor and target_file:
        bounds = indexer.resolve_symbol(repo_name, target_file, anchor)
    if not bounds and anchor:
        matches = [
            m for m in indexer.lookup_symbol(repo_name, anchor)
            if m.get("symbol_name") == anchor
        ]
        if matches:
            best = matches[0]
            analysis["target_file"] = best["file_path"]
            target_file = best["file_path"]
            bounds = (best["start_line"], best["end_line"])

    if not bounds:
        hint_text = " ".join([
            str(issue_body or ""),
            str(analysis.get("reproduction_steps", "")),
            str(analysis.get("target_file", "")),
            " ".join(analysis.get("potential_files", [])),
        ])
        hint = _extract_file_line_hint(hint_text)
        if hint:
            hint_file, hint_line = hint
            if not target_file:
                target_file = hint_file
                analysis["target_file"] = hint_file
            resolved = indexer.resolve_symbol_at_line(
                repo_name, target_file or hint_file, hint_line
            )
            if resolved:
                sym_name, start, end = resolved
                analysis["anchor_symbol"] = sym_name
                analysis["target_file"] = target_file or hint_file
                anchor = sym_name
                bounds = (start, end)
                print(
                    f"   ↳ Resolved anchor from line hint: {sym_name} "
                    f"in {analysis['target_file']} ({start}-{end})"
                )

    if anchor and not bounds:
        analysis["admitted"] = False
        analysis["reject_reason"] = (
            f"Anchor symbol '{anchor}' not found in repository index."
        )
        return analysis

    if not analysis.get("has_clear_repro") and complexity == "small":
        analysis["admitted"] = False
        analysis["reject_reason"] = "Small bug requires clear reproduction or stack trace."
        return analysis

    analysis["admitted"] = True
    analysis["reject_reason"] = ""
    return analysis


class IssueTriageEngine:
    def __init__(self, gemini_key: str, github_token: str):
        self.client = genai.Client(api_key=gemini_key)
        self.github_token = github_token

    def process_issue(self, issue_url: str) -> dict:
        """Runs parsing, fetching, and structured LLM triage with admission fields."""
        print(f"🔎 Parsing target endpoint: {issue_url}")
        meta = parse_github_url(issue_url)

        print(
            f"🌐 Fetching runtime data from GitHub REST API "
            f"for Issue #{meta['issue_number']}..."
        )
        issue_data = fetch_github_issue(
            meta["owner"], meta["repo"], meta["issue_number"], self.github_token
        )

        title = issue_data.get("title", "")
        body = issue_data.get("body", "") or "No body provided."
        labels = [lbl.get("name", "") for lbl in issue_data.get("labels", [])]
        labels_str = ", ".join(labels) if labels else "(none)"

        print("🤖 Analyzing issue with Gemini 2.5 Pro (structured triage + admission)...")

        prompt = f"""
You are an elite triage engineer for Go open-source bug fixes.

{ADMISSION_POLICY}

ISSUE TITLE: {title}
GITHUB LABELS: {labels_str}
ISSUE BODY:
{body}

Set admitted=True ONLY if this is a small or medium bug fix in existing code.
Set admitted=False with a clear reject_reason for features, proposals, security,
architecture, or unclear maintainer decisions.

anchor_symbol must be the bare Go function/method name (e.g. 'ExecuteC'), NOT qualified
names like 'Command.ExecuteC'. target_file should be the relative path (e.g. 'command.go').
"""

        response = self.client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TriageAnalysis,
                temperature=0.1,
            ),
        )

        analysis_result = json.loads(response.text)

        return {
            "meta": meta,
            "raw_title": title,
            "raw_body": body,
            "labels": labels,
            "analysis": analysis_result,
        }
