import base64
import json
import urllib.request
from google import genai
from google.genai import types


class IssueVisionAnalyzer:
    """
    Handles multimodal analysis of GitHub issue attachments.
    
    Some issues embed screenshots, stack trace images, or diagrams that carry
    diagnostic context not available in the text body. This module fetches those
    image URLs and passes them to Gemini's vision capability to extract structured
    textual summaries that can be appended to the triage context.
    """

    def __init__(self, gemini_key: str):
        self.client = genai.Client(api_key=gemini_key)

    def _fetch_image_as_base64(self, image_url: str) -> tuple[str, str]:
        """
        Downloads an image from a URL and returns (base64_data, mime_type).
        Supports JPEG, PNG, GIF, and WEBP — the four types Gemini vision accepts.
        """
        req = urllib.request.Request(image_url)
        req.add_header("User-Agent", "Sentinel-Agent-Engine")

        with urllib.request.urlopen(req, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "image/png").split(";")[0].strip()
            raw_bytes = response.read()

        # Normalise content-type to a Gemini-accepted MIME type
        mime_map = {
            "image/jpeg": "image/jpeg",
            "image/jpg":  "image/jpeg",
            "image/png":  "image/png",
            "image/gif":  "image/gif",
            "image/webp": "image/webp",
        }
        mime_type = mime_map.get(content_type, "image/png")
        b64 = base64.b64encode(raw_bytes).decode("utf-8")
        return b64, mime_type

    def extract_image_urls_from_body(self, issue_body: str) -> list[str]:
        """
        Extracts image URLs embedded in GitHub Markdown issue bodies.
        GitHub embeds images as: ![alt](url) or raw <img src="url"> tags.
        Focuses on github user-content CDN URLs which are public attachments.
        """
        import re
        patterns = [
            r"!\[.*?\]\((https://[^\s)]+\.(?:png|jpg|jpeg|gif|webp)[^\s)]*)\)",
            r'<img[^>]+src=["\']?(https://[^\s"\']+\.(?:png|jpg|jpeg|gif|webp)[^\s"\']*)["\']?',
            r"(https://user-images\.githubusercontent\.com/[^\s)>\"']+)",
            r"(https://github\.com/[^\s)>\"']+/assets/[^\s)>\"']+)",
        ]
        urls = []
        for pattern in patterns:
            urls.extend(re.findall(pattern, issue_body, re.IGNORECASE))
        # Deduplicate while preserving order
        seen = set()
        return [u for u in urls if not (u in seen or seen.add(u))]

    def analyze_issue_images(self, issue_body: str, max_images: int = 3) -> str:
        """
        Main entry point. Extracts images from an issue body, sends them to
        Gemini 2.5 Flash for vision analysis, and returns a consolidated
        plain-text summary of visual diagnostic context.

        Returns an empty string if no images are found or all fetches fail.
        """
        image_urls = self.extract_image_urls_from_body(issue_body)
        if not image_urls:
            return ""

        summaries = []
        for url in image_urls[:max_images]:
            summary = self._analyze_single_image(url)
            if summary:
                summaries.append(f"[Image: {url}]\n{summary}")

        if not summaries:
            return ""

        return "--- VISUAL CONTEXT FROM ISSUE ATTACHMENTS ---\n" + "\n\n".join(summaries)

    def _analyze_single_image(self, image_url: str) -> str:
        """
        Downloads and sends one image to Gemini for diagnostic extraction.
        Returns a plain-text description or empty string on failure.
        """
        try:
            b64_data, mime_type = self._fetch_image_as_base64(image_url)
        except Exception as e:
            print(f"  ⚠️  Vision: Could not fetch image {image_url}: {e}")
            return ""

        prompt = (
            "You are a software engineer analyzing a bug report attachment. "
            "Describe what you see in this image with technical precision. "
            "Focus on: stack traces, error messages, terminal output, code snippets, "
            "UI anomalies, or any information relevant to diagnosing a software bug. "
            "If the image contains no diagnostic information, reply with 'No diagnostic content.'"
        )

        try:
            # gemini-2.5-flash: fast, cost-efficient vision for inline issue images
            response = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    {
                        "parts": [
                            {
                                "inline_data": {
                                    "mime_type": mime_type,
                                    "data": b64_data,
                                }
                            },
                            {"text": prompt},
                        ]
                    }
                ],
            )
            result = response.text.strip()
            if "No diagnostic content" in result:
                return ""
            return result
        except Exception as e:
            print(f"  ⚠️  Vision: Gemini call failed for {image_url}: {e}")
            return ""