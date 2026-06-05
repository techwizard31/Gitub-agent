import asyncio
import os

class AsyncTestSuiteRunner:
    def __init__(self, workspace_path: str):
        self.workspace = os.path.abspath(workspace_path)

    async def run_command_async(self, cmd: list[str]) -> dict:
        """Executes system checks within an async process lifecycle wrapper."""
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            return {
                "command": " ".join(cmd),
                "return_code": process.returncode,
                "stdout": stdout.decode(errors="ignore").strip(),
                "stderr": stderr.decode(errors="ignore").strip(),
                "success": process.returncode == 0
            }
        except Exception as e:
            return {
                "command": " ".join(cmd),
                "return_code": -1,
                "stdout": "",
                "stderr": str(e),
                "success": False
            }

    async def execute_verification_matrix(self, track_id: str) -> dict:
        """Runs compilation vetting, styling rules, and tests concurrently."""
        print(f"⚡ [Matrix:{track_id}] Launching parallel Go check suite execution...")
        
        # Define tasks to execute simultaneously
        tasks = [
            self.run_command_async(["go", "vet", "./..."]),
            self.run_command_async(["go", "test", "-short", "./..."]) # Short execution constraint to save runtime cycles
        ]
        
        # Gather execution metrics natively
        results = await asyncio.gather(*tasks)
        
        vet_result = results[0]
        test_result = results[1]
        
        is_all_passed = vet_result["success"] and test_result["success"]
        
        # Compress dense diagnostic arrays down to atomic telemetry snippets for the agent
        failure_log = ""
        if not vet_result["success"]:
            failure_log += f"[VET ERROR]\n{vet_result['stderr'] or vet_result['stdout']}\n"
        if not test_result["success"]:
            # Isolate standard test failure logs cleanly
            failure_log += f"[TEST ERROR]\n{test_result['stdout'][:1000]}" # Truncated to avoid context token bloating
            
        return {
            "track_id": track_id,
            "passed": is_all_passed,
            "diagnostics": failure_log if not is_all_passed else "All verification checkpoints passed cleanly."
        }