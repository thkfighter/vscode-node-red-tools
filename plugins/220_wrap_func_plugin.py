"""
Wrap Func Plugin

Wraps regular function nodes in testable function declarations.
Creates .wrapped.js (and .initialize.js, .finalize.js if present).

This makes functions testable outside Node-RED by wrapping them with:
  export default function camelCaseName(msg, node, context, flow, global, env, RED) {
    // original code here
  }
"""

from __future__ import annotations

import importlib.util
import re
import textwrap
from pathlib import Path
from typing import List, Dict, Any, Optional

# Load plugin helpers module
_helpers_path = Path(__file__).parent / "plugin_helpers.py"
_spec = importlib.util.spec_from_file_location("plugin_helpers", _helpers_path)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
to_camel_case = _helpers.to_camel_case


class WrapFuncPlugin:
    """Plugin for wrapping regular function nodes in testable declarations"""

    def get_name(self) -> str:
        return "wrap_func"

    def get_priority(self) -> Optional[int]:
        return None  # Use filename prefix (220)

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
        # .wrapped.js / .initialize.js / .finalize.js => function node
        for suffix in (".wrapped.js", ".initialize.js", ".finalize.js"):
            if (node_dir / f"{node_id}{suffix}").exists():
                return "function"
        return None

    def explode_node(self, node: Dict[str, Any], node_dir: Path) -> List[str]:
        """Wrap func, initialize, and finalize in testable function declarations

        Returns:
            List of created filenames
        """
        try:
            node_id: str = node.get("id")
            node_name: str = node.get("name", "Unnamed")
            func_name: str = to_camel_case(node_name)
            created_files: List[str] = []

            # Wrap main func code
            func_code: str = node.get("func", "")
            if func_code:
                # Node-RED function parameters: msg, node, context, flow, global, env, RED
                wrapped_func: str = (
                    f"export default function {func_name}(msg, node, context, flow, global, env, RED) {{\n"
                    f"{func_code}\n"
                    f"}}\n"
                )
                wrapped_file: Path = node_dir / f"{node_id}.wrapped.js"
                wrapped_file.write_text(wrapped_func)
                created_files.append(f"{node_id}.wrapped.js")

            # Wrap initialize code if present
            initialize_code: str = node.get("initialize", "")
            if initialize_code:
                # Initialize doesn't get msg parameter
                wrapped_init: str = (
                    f"export default function {func_name}_initialize(node, context, flow, global, env, RED) {{\n"
                    f"{initialize_code}\n"
                    f"}}\n"
                )
                init_file: Path = node_dir / f"{node_id}.initialize.js"
                init_file.write_text(wrapped_init)
                created_files.append(f"{node_id}.initialize.js")

            # Wrap finalize code if present
            finalize_code: str = node.get("finalize", "")
            if finalize_code:
                # Finalize doesn't get msg parameter
                wrapped_final: str = (
                    f"export default function {func_name}_finalize(node, context, flow, global, env, RED) {{\n"
                    f"{finalize_code}\n"
                    f"}}\n"
                )
                final_file: Path = node_dir / f"{node_id}.finalize.js"
                final_file.write_text(wrapped_final)
                created_files.append(f"{node_id}.finalize.js")

            return created_files

        except Exception as e:
            print(
                f"⚠ Warning: wrap_func plugin failed for {node.get('id', 'unknown')}: {e}"
            )
            return []

    def rebuild_node(
        self, node_id: str, node_dir: Path, skeleton: Dict[str, Any]
    ) -> Dict[str, str]:
        """Rebuild func, initialize, and finalize from wrapped files"""
        data: Dict[str, str] = {}

        # Rebuild main func code
        wrapped_file: Path = node_dir / f"{node_id}.wrapped.js"
        if wrapped_file.exists():
            wrapped_code: str = wrapped_file.read_text()
            # Extract function body (everything between first { and last })
            # Pattern: export default function name(params) { BODY }
            match: Optional[re.Match] = re.search(
                r"export\s+default\s+function\s+\w+\s*\([^)]*\)\s*\{(.*)\}",
                wrapped_code,
                re.DOTALL,
            )
            if match:
                body: str = match.group(1)
                # Remove exactly one leading and one trailing newline if present
                if body.startswith("\n"):
                    body = body[1:]
                if body.endswith("\n"):
                    body = body[:-1]
                # Dedent the body to remove indentation added by prettier
                body = textwrap.dedent(body)
                data["func"] = body

        # Rebuild initialize code
        init_file: Path = node_dir / f"{node_id}.initialize.js"
        if init_file.exists():
            init_code: str = init_file.read_text()
            match: Optional[re.Match] = re.search(
                r"export\s+default\s+function\s+\w+\s*\([^)]*\)\s*\{(.*)\}",
                init_code,
                re.DOTALL,
            )
            if match:
                body: str = match.group(1)
                if body.startswith("\n"):
                    body = body[1:]
                if body.endswith("\n"):
                    body = body[:-1]
                # Dedent the body
                body = textwrap.dedent(body)
                data["initialize"] = body
        elif skeleton and "initialize" in skeleton:
            # Skeleton has initialize field - preserve position with empty string
            data["initialize"] = ""

        # Rebuild finalize code
        final_file: Path = node_dir / f"{node_id}.finalize.js"
        if final_file.exists():
            final_code: str = final_file.read_text()
            match: Optional[re.Match] = re.search(
                r"export\s+default\s+function\s+\w+\s*\([^)]*\)\s*\{(.*)\}",
                final_code,
                re.DOTALL,
            )
            if match:
                body: str = match.group(1)
                if body.startswith("\n"):
                    body = body[1:]
                if body.endswith("\n"):
                    body = body[:-1]
                # Dedent the body
                body = textwrap.dedent(body)
                data["finalize"] = body
        elif skeleton and "finalize" in skeleton:
            # Skeleton has finalize field - preserve position with empty string
            data["finalize"] = ""

        return data
