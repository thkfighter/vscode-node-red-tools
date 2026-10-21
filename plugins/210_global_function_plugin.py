"""
Global Function Plugin

Handles nodes with global function definitions (gfunc.functionName = globalDef).
Explodes to .function.js file and rebuilds with initialization/finalize templates.
"""

from __future__ import annotations

import re
import importlib.util
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# Load plugin helpers module
_helpers_path = Path(__file__).parent / "plugin_helpers.py"
_spec = importlib.util.spec_from_file_location("plugin_helpers", _helpers_path)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
to_camel_case = _helpers.to_camel_case
extract_function_body = _helpers.extract_function_body


def parse_global_function(code: str) -> Optional[Dict[str, str]]:
    """Parse global function definition from function code.

    Returns: dict with 'name', 'params', 'body' or None if not a global function
    """
    if not code:
        return None

    # Must have: const globalDef = (params) => { ... }
    if not re.search(r"const\s+globalDef\s*=\s*\(", code):
        return None

    # Must have: gfunc.functionName = globalDef
    gfunc_assign: Optional[re.Match] = re.search(r"gfunc\.(\w+)\s*=\s*globalDef;", code)
    if not gfunc_assign:
        return None

    func_name: str = gfunc_assign.group(1)

    # Extract params and body
    result: Optional[Tuple[str, str]] = extract_function_body(
        code, r"const\s+globalDef\s*=\s*\((.*?)\)\s*=>\s*{"
    )
    if not result:
        return None

    params: str
    body: str
    params, body = result
    return {"name": func_name, "params": params, "body": body}


class GlobalFunctionPlugin:
    """Plugin for handling global function nodes (gfunc.name)"""

    def get_name(self) -> str:
        return "global-function"

    def get_priority(self) -> Optional[int]:
        return None  # Use filename prefix (30)

    def get_plugin_type(self) -> str:
        return "explode"

    def can_handle_node(self, node: Dict[str, Any]) -> bool:
        """Check if this node is a global function"""
        if node.get("type") != "function":
            return False

        func_code: str = node.get("func", "")
        # Only used during explode (rebuild is file-driven). An empty func must
        # not match: claiming it would also swallow initialize/finalize code
        # without writing it anywhere.
        return parse_global_function(func_code) is not None

    def get_claimed_fields(self, node: Dict[str, Any]) -> List[str]:
        """Claim all fields this plugin generates during rebuild"""
        return ["func", "initialize", "finalize"]

    def explode_node(self, node: Dict[str, Any], node_dir: Path) -> List[str]:
        """Explode global function to .function.js file

        Returns:
            List of created filenames
        """
        try:
            node_id: str = node.get("id")
            node_name: str = node.get("name", "Unnamed")
            func_code: str = node.get("func", "")
            created_files: List[str] = []

            global_func_data: Optional[Dict[str, str]] = parse_global_function(
                func_code
            )
            if not global_func_data:
                return []

            # Use node name for function name
            func_name: str = to_camel_case(node_name)
            params: str = global_func_data["params"]
            body: str = global_func_data["body"]

            # Wrap in function declaration with export default (preserves exact body content)
            func_code = f"export default function {func_name}({params}) {{{body}}}"

            # Write to file (prettier will format in post-explode)
            func_file: Path = node_dir / f"{node_id}.function.js"
            func_file.write_text(func_code + "\n")
            created_files.append(f"{node_id}.function.js")

            return created_files

        except Exception as e:
            print(
                f"⚠ Warning: global-function plugin failed for {node.get('id', 'unknown')}: {e}"
            )
            return []

    def rebuild_node(
        self, node_id: str, node_dir: Path, skeleton: Dict[str, Any]
    ) -> Dict[str, str]:
        """Rebuild global function from .function.js file"""
        func_file: Path = node_dir / f"{node_id}.function.js"

        if not func_file.exists():
            return {}

        func_code: str = func_file.read_text()
        # Strip export default if present
        func_code = re.sub(r"^export\s+default\s+", "", func_code, flags=re.MULTILINE)

        node_name: str = skeleton.get("name", "Unnamed")
        func_name: str = to_camel_case(node_name)

        # Extract params and body
        result: Optional[Tuple[str, str]] = extract_function_body(
            func_code, r"function\s+\w+\s*\((.*?)\)\s*{"
        )
        if not result:
            return {}

        params: str
        body: str
        params, body = result

        # Build initialize template (empty - all work happens in func)
        init_template: str = ""

        # Build main func template (convert back to arrow function)
        func_template: str = f"""// Define global function
const globalDef = ({params}) => {{{body}}};

// Store in global context
const gfunc = global.get("gfunc") || {{}};
gfunc.{func_name} = globalDef;
global.set("gfunc", gfunc);

node.status({{ fill: "blue", shape: "dot", text: "{func_name} loaded" }});
return msg;"""

        # Build finalize template
        finalize_template: str = f"""// Cleanup: Remove function from global context
const gfunc = global.get("gfunc") || {{}};
delete gfunc.{func_name};
global.set("gfunc", gfunc);"""

        return {
            "initialize": init_template,
            "func": func_template,
            "finalize": finalize_template,
        }
