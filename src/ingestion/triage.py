import os
import re
import json
import urllib.request
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# 1. Define a strict Pydantic model for structural issue analysis
class TriageAnalysis(BaseModel):
    symptom: str = Field(description="Clear, concise summary of what is broken (e.g., unexpected panic, silent validation bypass).")
    target_package: str = Field(description="The specific package or subdirectory where the bug likely lives (e.g., 'gin', 'cobra').")
    potential_files: list[str] = Field(description="A list of possible file names deduced from stack traces or descriptions.")
    reproduction_steps: str = Field(description="Extracted or inferred minimal code snippet or sequence of commands to reproduce the bug.")
    is_breaking_change_risk: bool = Field(description="True if modifying this component risks altering backwards compatibility or shared public interfaces.")

def parse_github_url(url: str) -> dict:
    """Parses a standard GitHub issue link to isolate metadata parameters."""
    pattern = r"github\.com/([^/]+)/([^/]+)/issues/(\d+)"
    match = re.search(pattern, url)
    if not match:
        raise ValueError("Invalid GitHub Issue URL format. Expected: https://github.com/owner/repo/issues/num")
    
    return {
        "owner": match.group(1),
        "repo": match.group(2),
        "issue_number": int(match.group(3))
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
    except Exception as e:
        raise RuntimeError(f"Failed connecting to GitHub API: {e}. Check token permissions or issue privacy.")

class IssueTriageEngine:
    def __init__(self, gemini_key: str, github_token: str):
        # Initialize the official Google GenAI SDK client
        self.client = genai.Client(api_key=gemini_key)
        self.github_token = github_token

    def process_issue(self, issue_url: str) -> dict:
        """Runs the complete parsing, fetching, and structured LLM triage pipeline."""
        print(f"🔎 Parsing target endpoint: {issue_url}")
        meta = parse_github_url(issue_url)
        
        print(f"🌐 Fetching runtime data from GitHub REST API for Issue #{meta['issue_number']}...")
        issue_data = fetch_github_issue(meta['owner'], meta['repo'], meta['issue_number'], self.github_token)
        
        title = issue_data.get("title", "")
        body = issue_data.get("body", "") or "No body provided."
        
        print(f"🤖 Analyzing issue with Gemini 2.5 Pro (structured triage)...")
        
        prompt = f"""
        You are an elite triage engineer. Analyze the following open-source GitHub issue description and structure its operational goals.
        
        ISSUE TITLE: {title}
        ISSUE BODY:
        {body}
        """

        # Enforce structured output generation using Pydantic models
        # gemini-2.5-pro: best-in-class reasoning for complex structured JSON triage
        response = self.client.models.generate_content(
            model="gemini-2.5-pro",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TriageAnalysis,
                temperature=0.1
            )
        )
        
        # Parse the structured JSON result directly back into standard python arrays
        analysis_result = json.loads(response.text)
        
        return {
            "meta": meta,
            "raw_title": title,
            "analysis": analysis_result
        }