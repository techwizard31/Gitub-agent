import os
import re
import sqlite3
import subprocess

class RepositoryIndexer:
    def __init__(self, db_path: str = ".cache/state_cache.db"):
        self.db_path = db_path
        # Ensure the cache directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initializes the relational SQLite database schema."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS repo_symbols (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    commit_hash TEXT,
                    repo_name TEXT,
                    file_path TEXT,
                    symbol_name TEXT,
                    symbol_type TEXT, -- 'function', 'method', 'struct', 'interface'
                    signature TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    UNIQUE(commit_hash, file_path, symbol_name, symbol_type)
                )
            """)
            conn.commit()

    def get_current_commit_hash(self, repo_path: str) -> str:
        """Retrieves the active Git commit hash of the targeted repository."""
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"], 
                cwd=repo_path, 
                stderr=subprocess.STDOUT
            ).decode().strip()
        except Exception:
            return "untracked_workspace"

    def is_repo_indexed(self, repo_name: str, commit_hash: str) -> bool:
        """Checks if this specific repository commit state is already cached."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM repo_symbols WHERE repo_name = ? AND commit_hash = ?", 
                (repo_name, commit_hash)
            )
            return cursor.fetchone()[0] > 0

    def parse_go_file(self, file_path: str) -> list[dict]:
        """
        Parses a Go file lexically to extract structural boundaries of 
        functions, methods, structs, and interfaces using brace-matching.
        """
        symbols = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            return []

        # Regular Expressions matching classic Go syntax patterns
        func_pattern = re.compile(r"^func\s+([A-Za-z0-9_]+)\s*\(")
        method_pattern = re.compile(r"^func\s*\([^)]+\)\s*([A-Za-z0-9_]+)\s*\(")
        struct_pattern = re.compile(r"^type\s+([A-Za-z0-9_]+)\s+struct")
        interface_pattern = re.compile(r"^type\s+([A-Za-z0-9_]+)\s+interface")

        in_block = False
        block_start = 0
        brace_count = 0
        current_symbol = None

        for idx, line in enumerate(lines):
            line_num = idx + 1
            clean_line = line.strip()

            # Skip comments and empty spaces
            if clean_line.startswith("//") or clean_line.startswith("/*") or not clean_line:
                continue

            if not in_block:
                # 1. Match Functions
                func_match = func_pattern.match(clean_line)
                # 2. Match Methods
                method_match = method_pattern.match(clean_line)
                # 3. Match Structs
                struct_match = struct_pattern.search(clean_line)
                # 4. Match Interfaces
                interface_match = interface_pattern.search(clean_line)

                match = func_match or method_match or struct_match or interface_match
                if match:
                    current_symbol = {
                        "name": match.group(1),
                        "signature": clean_line,
                        "start_line": line_num,
                        "type": "function" if func_match else "method" if method_match else "struct" if struct_match else "interface"
                    }
                    
                    # Track opening scopes
                    brace_count += clean_line.count("{")
                    brace_count -= clean_line.count("}")
                    
                    if "{" in clean_line:
                        in_block = True
                        block_start = line_num
                    # Single line declarations
                    if "{" in clean_line and "}" in clean_line and brace_count == 0:
                        current_symbol["end_line"] = line_num
                        symbols.append(current_symbol)
                        in_block = False
            else:
                # Keep tracking active nested brace loops
                brace_count += clean_line.count("{")
                brace_count -= clean_line.count("}")

                if brace_count <= 0:
                    current_symbol["end_line"] = line_num
                    symbols.append(current_symbol)
                    in_block = False
                    brace_count = 0

        return symbols

    def index_repository(self, repo_path: str, repo_name: str) -> str:
        """Walks the repo filesystem, structures all symbols, and updates the SQLite cache."""
        commit_hash = self.get_current_commit_hash(repo_path)
        
        print(f"📦 Checking index telemetry for execution context: {repo_name} [{commit_hash[:8]}]")
        if self.is_repo_indexed(repo_name, commit_hash):
            print("⚡ Index Cache Hit! Codebase map loaded instantly from local storage.")
            return commit_hash

        print("🔍 Index Cache Miss. Commencing complete code structural mapping...")
        parsed_symbols = []

        for root, _, files in os.walk(repo_path):
            # Skip hidden modules or configuration artifacts
            if any(part.startswith('.') for part in root.split(os.sep)):
                continue
            
            for file in files:
                if file.endswith(".go") and not file.endswith("_test.go"):
                    full_path = os.path.normpath(os.path.join(root, file))
                    # Retain relative reference pathing for easy review
                    rel_path = os.path.relpath(full_path, repo_path)
                    
                    file_symbols = self.parse_go_file(full_path)
                    for sym in file_symbols:
                        parsed_symbols.append((
                            commit_hash, repo_name, rel_path, 
                            sym["name"], sym["type"], sym["signature"], 
                            sym["start_line"], sym["end_line"]
                        ))

        # Atomic bulk ingestion to guarantee data integrity
        if parsed_symbols:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.executemany("""
                    INSERT OR REPLACE INTO repo_symbols 
                    (commit_hash, repo_name, file_path, symbol_name, symbol_type, signature, start_line, end_line)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, parsed_symbols)
                conn.commit()

        print(f"✅ Code structural index sync completed. Registered {len(parsed_symbols)} active code symbols.")
        return commit_hash

    def lookup_symbol(self, repo_name: str, symbol_name: str) -> list[dict]:
        """Queries the indexed schema directly to resolve coordinate vectors."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT file_path, symbol_name, symbol_type, signature, start_line, end_line 
                FROM repo_symbols 
                WHERE repo_name = ? AND symbol_name LIKE ?
            """, (repo_name, f"%{symbol_name}%"))
            
            return [dict(row) for row in cursor.fetchall()]

    def resolve_symbol(
        self, repo_name: str, file_path: str, symbol_name: str
    ) -> tuple[int, int] | None:
        """
        Returns fresh (start_line, end_line) for an exact symbol in a specific file.
        Prefers function/method symbols over struct/interface when names collide.
        """
        if not symbol_name or not file_path:
            return None

        norm_path = file_path.replace("\\", "/")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT start_line, end_line, symbol_type
                FROM repo_symbols
                WHERE repo_name = ? AND file_path = ? AND symbol_name = ?
                ORDER BY
                    CASE symbol_type
                        WHEN 'method' THEN 0
                        WHEN 'function' THEN 1
                        WHEN 'struct' THEN 2
                        ELSE 3
                    END,
                    start_line
            """, (repo_name, norm_path, symbol_name))
            rows = cursor.fetchall()

        if not rows:
            # Fallback: basename path match (planner may omit directory prefix)
            base = os.path.basename(norm_path)
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT start_line, end_line, symbol_type
                    FROM repo_symbols
                    WHERE repo_name = ? AND file_path LIKE ? AND symbol_name = ?
                    ORDER BY
                        CASE symbol_type
                            WHEN 'method' THEN 0
                            WHEN 'function' THEN 1
                            WHEN 'struct' THEN 2
                            ELSE 3
                        END,
                        start_line
                """, (repo_name, f"%{base}", symbol_name))
                rows = cursor.fetchall()

        if not rows:
            return None
        return (rows[0]["start_line"], rows[0]["end_line"])

    def resolve_symbol_at_line(
        self, repo_name: str, file_path: str, line: int
    ) -> tuple[str, int, int] | None:
        """Returns (symbol_name, start_line, end_line) for the symbol enclosing line."""
        if not file_path or line < 1:
            return None

        norm_path = file_path.replace("\\", "/")
        base = os.path.basename(norm_path)

        def _query(path_clause: str, path_arg: str) -> list:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT symbol_name, start_line, end_line, symbol_type
                    FROM repo_symbols
                    WHERE repo_name = ? AND {path_clause}
                    AND symbol_type IN ('function', 'method')
                    AND start_line <= ? AND end_line >= ?
                    ORDER BY (end_line - start_line), start_line
                """, (repo_name, path_arg, line, line))
                return cursor.fetchall()

        rows = _query("file_path = ?", norm_path)
        if not rows:
            rows = _query("file_path LIKE ?", f"%{base}")

        if not rows:
            return None
        row = rows[0]
        return (row["symbol_name"], row["start_line"], row["end_line"])