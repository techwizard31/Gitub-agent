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


class PRGenerator:
    """
    Generates a production-quality GitHub Pull Request title and body from
    the winning hypothesis track, issue triage data, and the applied patch.

    The output is formatted as standard GitHub Markdown, ready for direct use
    with the GitHub PR creation API or for submission as a local patch summary.
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
        """
        Generates a structured PR draft and returns both raw fields and
        a formatted Markdown body string.

        Args:
            triage_data:         Output from IssueTriageEngine.process_issue()
            winning_hypothesis:  The FixHypothesis Pydantic object that passed tests
            repo_name:           Target repository name (e.g. 'cobra', 'gin')
            branch_name:         The git branch where the patch lives
            test_diagnostics:    The success message from AsyncTestSuiteRunner

        Returns:
            dict with keys: title, body (Markdown), branch, raw (PullRequestDraft fields)
        """
        analysis = triage_data.get("analysis", {})
        issue_meta = triage_data.get("meta", {})
        issue_number = issue_meta.get("issue_number", "")
        issue_owner = issue_meta.get("owner", "")
        raw_title = triage_data.get("raw_title", "")

        hyp_dict = winning_hypothesis.model_dump() if hasattr(winning_hypothesis, "model_dump") else vars(winning_hypothesis)

        prompt = f"""
You are a senior open-source contributor writing a GitHub Pull Request for a Go repository.

Generate a structured PR draft for the following resolved bug fix.

--- ISSUE CONTEXT ---
Repository: {issue_owner}/{repo_name}
Issue #{issue_number}: {raw_title}
Symptom: {analysis.get('symptom', '')}
Root Cause Area: {analysis.get('target_package', '')}
Reproduction: {analysis.get('reproduction_steps', '')}
Breaking Change Risk: {analysis.get('is_breaking_change_risk', False)}

--- APPLIED FIX ---
Fix Title: {hyp_dict.get('title', '')}
Modified File: {hyp_dict.get('target_file', '')}
Lines Changed: {hyp_dict.get('start_line', '')} to {hyp_dict.get('end_line', '')}
Patch Applied:
```go
{hyp_dict.get('proposed_code', '')}
```

--- VALIDATION RESULT ---
{test_diagnostics}

Write a precise, professional PR description. Follow conventional commit style for the title.
Use the exact file names and line numbers from the fix. Do not use generic language.
"""

        # gemini-2.5-pro: structured JSON output for PR draft fields
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

        body = self._render_markdown_body(draft, issue_number, branch_name, hyp_dict, test_diagnostics)

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
        """Renders the PullRequestDraft into standard GitHub PR Markdown."""

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
| `{hyp_dict.get('target_file', 'N/A')}` | {hyp_dict.get('start_line', '?')}–{hyp_dict.get('end_line', '?')} | {hyp_dict.get('title', '')} |
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
        """Prints a formatted terminal summary of the generated PR."""
        print("\n" + "=" * 70)
        print("📋  GENERATED PULL REQUEST")
        print("=" * 70)
        print(f"\n🏷️  TITLE:\n   {pr_result['title']}")
        print(f"\n🌿  BRANCH: {pr_result['branch']}")
        print(f"\n📝  BODY:\n")
        # Indent body for terminal readability
        for line in pr_result["body"].splitlines():
            print(f"   {line}")
        print("\n" + "=" * 70)