"""
Func Plugin

Handles basic func field extraction for function nodes.
This is a fallback plugin that runs after action and global-function plugins.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any, Optional


class FuncPlugin:
    """Plugin for handling basic func field in function nodes"""

    def get_name(self) -> str:
        return "func"

    def get_priority(self) -> Optional[int]:
        return None  # Use filename prefix (30)

    def get_plugin_type(self) -> str:
        return "explode"

    def can_handle_node(self, node: Dict[str, Any]) -> bool:
        """Handle any function node not already claimed by action/global-function.

        func may be empty while initialize/finalize still hold code (e.g. a node
        that only seeds global context on deploy), so don't require func.
        """
        return node.get("type") == "function"

    def get_claimed_fields(self, node: Dict[str, Any]) -> List[str]:
        """Claim the func, initialize, and finalize fields"""
        return ["func", "initialize", "finalize"]

    def can_infer_node_type(self, node_dir: Path, node_id: str) -> Optional[str]:
        """Infer node type from files, returns None if can't infer"""
        # .js / .initialize.js / .finalize.js => function node
        for suffix in (".js", ".initialize.js", ".finalize.js"):
            if (node_dir / f"{node_id}{suffix}").exists():
                return "function"
        return None

    def explode_node(self, node: Dict[str, Any], node_dir: Path) -> List[str]:
        """Extract func, initialize, and finalize fields to separate .js files

        Returns:
            List of created filenames
        """
        try:
            node_id: str = node.get("id")
            created_files: List[str] = []

            # Extract main func code
            func_code: str = node.get("func", "")
            if func_code:
                js_file: Path = node_dir / f"{node_id}.js"
                js_file.write_text(func_code)
                created_files.append(f"{node_id}.js")

            # Extract initialize code if present
            initialize_code: str = node.get("initialize", "")
            if initialize_code:
                init_file: Path = node_dir / f"{node_id}.initialize.js"
                init_file.write_text(initialize_code)
                created_files.append(f"{node_id}.initialize.js")

            # Extract finalize code if present
            finalize_code: str = node.get("finalize", "")
            if finalize_code:
                final_file: Path = node_dir / f"{node_id}.finalize.js"
                final_file.write_text(finalize_code)
                created_files.append(f"{node_id}.finalize.js")

            return created_files

        except Exception as e:
            print(f"⚠ Warning: func plugin failed for {node.get('id', 'unknown')}: {e}")
            return []

    def rebuild_node(
        self, node_id: str, node_dir: Path, skeleton: Dict[str, Any]
    ) -> Dict[str, str]:
        """Rebuild func, initialize, and finalize from .js files"""
        data: Dict[str, str] = {}

        # Rebuild main func code
        js_file: Path = node_dir / f"{node_id}.js"
        if js_file.exists():
            data["func"] = js_file.read_text()

        # Rebuild initialize code
        init_file: Path = node_dir / f"{node_id}.initialize.js"
        if init_file.exists():
            data["initialize"] = init_file.read_text()
        elif skeleton and "initialize" in skeleton:
            # Skeleton has initialize field - preserve position with empty string
            data["initialize"] = ""

        # Rebuild finalize code
        final_file: Path = node_dir / f"{node_id}.finalize.js"
        if final_file.exists():
            data["finalize"] = final_file.read_text()
        elif skeleton and "finalize" in skeleton:
            # Skeleton has finalize field - preserve position with empty string
            data["finalize"] = ""

        return data
