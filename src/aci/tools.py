import os
import subprocess


class AgentComputerInterface:
    def __init__(self, base_workspace_path: str = "."):
        self.base_path = os.path.abspath(base_workspace_path)

    def _resolve_safe_path(self, relative_file_path: str) -> str:
        """Ensures file mutations stay strictly bounded inside the active workspace."""
        full_path = os.path.abspath(os.path.join(self.base_path, relative_file_path))
        if not full_path.startswith(self.base_path):
            raise PermissionError(
                f"Access Denied: Path '{relative_file_path}' falls outside workspace constraints."
            )
        return full_path

    def view_file_range(self, file_path: str, start_line: int, end_line: int) -> str:
        """
        Sliding-window code reader. Returns line intervals with line numbers.
        Prevents large source code bases from blowing out token context ceilings.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            if not os.path.exists(safe_path):
                return f"Error: File '{file_path}' does not exist."

            with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start = max(1, start_line)
            end = min(total_lines, end_line)

            if start > total_lines or start > end:
                return f"Error: Invalid line bounds. File has {total_lines} lines."

            output = []
            for i in range(start - 1, end):
                output.append(f"{i + 1:4d} | {lines[i]}")

            return "".join(output)
        except Exception as e:
            return f"Error reading file range: {e}"

    def apply_code_patch(
        self, file_path: str, start_line: int, end_line: int, new_content: str
    ) -> str:
        """
        REPLACE mode: overwrites lines start_line..end_line with new_content.
        Use for bug fixes that modify existing logic.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            if not os.path.exists(safe_path):
                return f"Error: Target file '{file_path}' not found."

            with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start = max(1, start_line)
            end = min(total_lines, end_line)

            new_lines = new_content.splitlines(keepends=True)
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"

            updated_lines = lines[: start - 1] + new_lines + lines[end:]

            with open(safe_path, "w", encoding="utf-8") as f:
                f.writelines(updated_lines)

            syntax_status = self.run_local_syntax_check(file_path)
            if "FAIL" in syntax_status:
                return f"⚠️ Patch written, but local syntax verification intercepted errors:\n{syntax_status}"

            return f"✅ Patch applied across lines {start} to {end}."
        except Exception as e:
            return f"Error applying patch: {e}"

    def insert_after_line(
        self, file_path: str, after_line: int, new_content: str
    ) -> str:
        """
        INSERT mode: inserts new_content AFTER after_line without removing anything.
        Use for enhancements that add new functions, map entries, or validators.

        This is the correct operation for adding new code that doesn't exist yet.
        'apply_code_patch' (replace) would overwrite existing lines, destroying them.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            if not os.path.exists(safe_path):
                return f"Error: Target file '{file_path}' not found."

            with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            total_lines = len(lines)
            insert_at = min(after_line, total_lines)  # clamp to file length

            # Ensure new content ends with a newline
            new_lines = new_content.splitlines(keepends=True)
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            # Add a blank separator line before inserted block if previous line isn't blank
            if insert_at > 0 and lines[insert_at - 1].strip():
                new_lines = ["\n"] + new_lines

            updated_lines = lines[:insert_at] + new_lines + lines[insert_at:]

            with open(safe_path, "w", encoding="utf-8") as f:
                f.writelines(updated_lines)

            syntax_status = self.run_local_syntax_check(file_path)
            if "FAIL" in syntax_status:
                return f"⚠️ Insert written, but local syntax verification intercepted errors:\n{syntax_status}"

            return (
                f"✅ Code inserted after line {insert_at} "
                f"({len(new_lines)} lines added)."
            )
        except Exception as e:
            return f"Error inserting code: {e}"

    def run_local_syntax_check(self, file_path: str) -> str:
        """
        Uses native 'gofmt' for a fast pre-compilation syntax check.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            result = subprocess.run(
                ["gofmt", "-e", safe_path],
                capture_output=True,
                text=True,
                cwd=self.base_path,
            )
            if result.returncode != 0:
                clean_err = result.stderr.replace(safe_path, os.path.basename(file_path))
                return f"SYNTAX FAIL:\n{clean_err.strip()}"
            return "SYNTAX PASS: Code syntactic structures verified."
        except Exception as e:
            return f"Pre-verification engine exception: {e}"