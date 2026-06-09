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
        REPLACE mode: atomically overwrites lines start_line..end_line with new_content.

        ATOMIC: if gofmt fails after writing, the file is immediately restored to its
        original state. This prevents cascading failures where a corrupt file causes
        every subsequent patch to land on the wrong lines.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            if not os.path.exists(safe_path):
                return f"Error: Target file '{file_path}' not found."

            with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
                original_lines = f.readlines()

            total_lines = len(original_lines)
            start = max(1, start_line)
            end = min(total_lines, end_line)

            new_lines = new_content.splitlines(keepends=True)
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"

            updated_lines = original_lines[: start - 1] + new_lines + original_lines[end:]

            with open(safe_path, "w", encoding="utf-8") as f:
                f.writelines(updated_lines)

            syntax_status = self.run_local_syntax_check(file_path)
            if "FAIL" in syntax_status:
                # REVERT — restore original content so the file stays valid
                # for any subsequent patches in the same hypothesis
                with open(safe_path, "w", encoding="utf-8") as f:
                    f.writelines(original_lines)
                return f"⚠️ Patch written, but local syntax verification intercepted errors:\n{syntax_status}"

            return f"✅ Patch applied across lines {start} to {end}."
        except Exception as e:
            return f"Error applying patch: {e}"

    def insert_after_line(
        self, file_path: str, after_line: int, new_content: str
    ) -> str:
        """
        INSERT mode: atomically inserts new_content AFTER after_line.

        ATOMIC: if gofmt fails after writing, the file is immediately restored to its
        original state.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            if not os.path.exists(safe_path):
                return f"Error: Target file '{file_path}' not found."

            with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
                original_lines = f.readlines()

            total_lines = len(original_lines)
            insert_at = min(after_line, total_lines)

            new_lines = new_content.splitlines(keepends=True)
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            if insert_at > 0 and original_lines[insert_at - 1].strip():
                new_lines = ["\n"] + new_lines

            updated_lines = original_lines[:insert_at] + new_lines + original_lines[insert_at:]

            with open(safe_path, "w", encoding="utf-8") as f:
                f.writelines(updated_lines)

            syntax_status = self.run_local_syntax_check(file_path)
            if "FAIL" in syntax_status:
                # REVERT — restore original content
                with open(safe_path, "w", encoding="utf-8") as f:
                    f.writelines(original_lines)
                return f"⚠️ Insert written, but local syntax verification intercepted errors:\n{syntax_status}"

            return (
                f"✅ Code inserted after line {insert_at} "
                f"({len(new_lines)} lines added)."
            )
        except Exception as e:
            return f"Error inserting code: {e}"

    def run_local_syntax_check(self, file_path: str) -> str:
        """Uses gofmt -e for a fast pre-compilation syntax check."""
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