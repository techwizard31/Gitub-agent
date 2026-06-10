import json
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


class PullRequestDraft(BaseModel):
    title: str = Field(
        description="Concise PR title following conventional commit style. Max 72 chars. "
                    "Examples: 'fix(cobra): handle nil pointer in command completion', "
                    "'fix(gin): prevent panic on empty router group prefix'"
    )
    summary: str = Field(
        description="1-2 sentence plain-English summary of what was broken and what the fix does."
    )
    problem_description: str = Field(
        description="Detailed explanation of the root cause: what was failing, why, and under what conditions."
    )
    solution_description: str = Field(
        description="Technical explanation of the fix approach: what was changed and why this approach is correct."
    )
    testing_notes: str = Field(
        description="Description of what validation was performed (go vet, go test -short ./...) and what passed."
    )
    breaking_change: bool = Field(
        description="True if this change alters any public API signatures or backwards-compatible behaviour."
    )
    breaking_change_note: str = Field(
        description="If breaking_change is True, describe the impact. Otherwise leave as empty string."
    )


def _format_patches_for_prompt(hyp_dict: dict) -> str:
    """Renders all FilePatch entries from a FixHypothesis for the LLM prompt."""
    patches = hyp_dict.get("patches") or []
    if not patches:
        return "(no patches recorded)"

    blocks = []
    for i, p in enumerate(patches, 1):
        blocks.append(
            f"Patch {i}: {p.get('target_file', 'N/A')}\n"
            f"  Lines: {p.get('start_line', '?')}-{p.get('end_line', '?')}\n"
            f"  Mode: {p.get('patch_mode', 'replace')}\n"
            f"  Description: {p.get('description', '')}\n"
            f"  Code:\n```go\n{p.get('new_code', '(applied in worktree)')}\n```"
        )
    return "\n\n".join(blocks)


def _format_patches_table(hyp_dict: dict) -> str:
    """Markdown table rows for all patches."""
    patches = hyp_dict.get("patches") or []
    if not patches:
        return "| (none) | — | — |"

    rows = []
    for p in patches:
        fname = p.get("target_file", "N/A")
        lines = f"{p.get('start_line', '?')}–{p.get('end_line', '?')}"
        desc = p.get("description", hyp_dict.get("title", ""))
        rows.append(f"| `{fname}` | {lines} | {desc} |")
    return "\n".join(rows)


class PRGenerator:
    """
    Generates a production-quality GitHub Pull Request title and body from
    the winning hypothesis track, issue triage data, and the applied patches.
    """

    def __init__(self, gemini_key: str):
        self.client = genai.Client(api_key=gemini_key)

    def generate(
        self,
        triage_data: dict,
        winning_hypothesis: object,
        repo_name: str,
        branch_name: str,
        test_diagnostics: str,
    ) -> dict:
        analysis = triage_data.get("analysis", {})
        issue_meta = triage_data.get("meta", {})
        issue_number = issue_meta.get("issue_number", "")
        issue_owner = issue_meta.get("owner", "")
        raw_title = triage_data.get("raw_title", "")

        hyp_dict = (
            winning_hypothesis.model_dump()
            if hasattr(winning_hypothesis, "model_dump")
            else vars(winning_hypothesis)
        )
        patches_text = _format_patches_for_prompt(hyp_dict)

        prompt = f"""
You are a senior open-source contributor writing a GitHub Pull Request for a Go repository.

Generate a structured PR draft for the following resolved bug fix.

--- ISSUE CONTEXT ---
Repository: {issue_owner}/{repo_name}
Issue #{issue_number}: {raw_title}
Symptom: {analysis.get('symptom', '')}
Root Cause Area: {analysis.get('target_package', '')}
Anchor Symbol: {analysis.get('anchor_symbol', '')}
Reproduction: {analysis.get('reproduction_steps', '')}
Breaking Change Risk: {analysis.get('is_breaking_change_risk', False)}

--- APPLIED FIX ---
Fix Title: {hyp_dict.get('title', '')}
Patches ({len(hyp_dict.get('patches', []))} file(s)):
{patches_text}

--- VALIDATION RESULT ---
{test_diagnostics}

Write a precise, professional PR description. Follow conventional commit style for the title.
Use the exact file names and line numbers from the patches. Do not use generic language.
"""

        response = self.client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PullRequestDraft,
                temperature=0.2,
            ),
        )

        draft_data = json.loads(response.text)
        draft = PullRequestDraft(**draft_data)

        body = self._render_markdown_body(
            draft, issue_number, branch_name, hyp_dict, test_diagnostics
        )

        return {
            "title": draft.title,
            "body": body,
            "branch": branch_name,
            "raw": draft_data,
        }

    def _render_markdown_body(
        self,
        draft: PullRequestDraft,
        issue_number: int | str,
        branch_name: str,
        hyp_dict: dict,
        test_diagnostics: str,
    ) -> str:
        breaking_section = ""
        if draft.breaking_change:
            breaking_section = f"""
## ⚠️ Breaking Change
{draft.breaking_change_note}
"""

        return f"""## Summary
{draft.summary}

Closes #{issue_number}

## Problem
{draft.problem_description}

## Solution
{draft.solution_description}

## Changes
| File | Lines | Description |
|------|-------|-------------|
{_format_patches_table(hyp_dict)}
{breaking_section}
## Testing
```
{test_diagnostics}
```

All verifications passed on branch `{branch_name}`:
- `go vet ./...` — no issues
- `go test -short ./...` — all tests pass

## Checklist
- [x] Code follows project conventions
- [x] `go vet` passes
- [x] `go test -short ./...` passes
- [x] No unrelated changes included
- [ ] Changelog updated (if applicable)
"""

    def print_pr_summary(self, pr_result: dict):
        print("\n" + "=" * 70)
        print("📋  GENERATED PULL REQUEST")
        print("=" * 70)
        print(f"\n🏷️  TITLE:\n   {pr_result['title']}")
        print(f"\n🌿  BRANCH: {pr_result['branch']}")
        print(f"\n📝  BODY:\n")
        for line in pr_result["body"].splitlines():
            print(f"   {line}")
        print("\n" + "=" * 70)
