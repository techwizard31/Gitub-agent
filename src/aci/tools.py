import os
import subprocess

class AgentComputerInterface:
    def __init__(self, base_workspace_path: str = "."):
        # Anchor the root workspace path securely
        self.base_path = os.path.abspath(base_workspace_path)

    def _resolve_safe_path(self, relative_file_path: str) -> str:
        """Ensures file mutations stay strictly bounded inside the active workspace."""
        full_path = os.path.abspath(os.path.join(self.base_path, relative_file_path))
        if not full_path.startswith(self.base_path):
            raise PermissionError(f"Access Denied: Path '{relative_file_path}' falls outside workspace constraints.")
        return full_path

    def view_file_range(self, file_path: str, start_line: int, end_line: int) -> str:
        """
        Sliding-window code reader. Returns line intervals with structural spacing.
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
                return f"Error: Invalid line bounds requested. Target file contains {total_lines} total lines."

            output = []
            for i in range(start - 1, end):
                # Format output with clear line-number telemetry
                output.append(f"{i + 1:4d} | {lines[i]}")
            
            return "".join(output)
        except Exception as e:
            return f"Error executing context read range: {e}"

    def apply_code_patch(self, file_path: str, start_line: int, end_line: int, new_content: str) -> str:
        """
        Range-bound code mutation injector. Replaces elements strictly between 
        start_line and end_line while preserving the surrounding codebase intact.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            if not os.path.exists(safe_path):
                return f"Error: Target modification asset '{file_path}' not found."

            with open(safe_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            total_lines = len(lines)
            start = max(1, start_line)
            end = min(total_lines, end_line)

            # Ensure incoming content maintains standard newline breaks
            new_lines = new_content.splitlines(keepends=True)
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"

            # Splice new content slices directly into the array map
            updated_lines = lines[:start - 1] + new_lines + lines[end:]

            with open(safe_path, "w", encoding="utf-8") as f:
                f.writelines(updated_lines)

            # Defensive Check: Run an instant syntax check to catch basic compile bugs immediately
            syntax_status = self.run_local_syntax_check(file_path)
            if "FAIL" in syntax_status:
                return f"⚠️ Patch written, but local syntax verification intercepted errors:\n{syntax_status}"

            return f"✅ Patch successfully applied and verified across lines {start} to {end}."
        except Exception as e:
            return f"Error processing patch allocation boundaries: {e}"

    def run_local_syntax_check(self, file_path: str) -> str:
        """
        Uses native 'gofmt' to perform a ultra-fast pre-compilation check 
        to ensure structural code alignment.
        """
        try:
            safe_path = self._resolve_safe_path(file_path)
            result = subprocess.run(
                ["gofmt", "-e", safe_path],
                capture_output=True,
                text=True,
                cwd=self.base_path
            )
            if result.returncode != 0:
                # Format output to obscure host-specific absolute path leaks
                clean_err = result.stderr.replace(safe_path, os.path.basename(file_path))
                return f"SYNTAX FAIL:\n{clean_err.strip()}"
            return "SYNTAX PASS: Code syntactic structures verified."
        except Exception as e:
            return f"Pre-verification engine exception: {e}"