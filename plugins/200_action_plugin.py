"""
Action Plugin

Handles nodes with action definitions (qcmd.action_name = actionDef).
Explodes to .def.js and .execute.js (optional).
Rebuilds with initialize/func/finalize templates.
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
to_snake_case = _helpers.to_snake_case
extract_function_body = _helpers.extract_function_body


def parse_action_definition(code: str) -> Optional[Dict[str, Any]]:
    """Parse action definition from function code.

    Returns: dict with 'def_code' and 'execute' (if exists)
             or None if not an action definition
    """
    if not code:
        return None

    # Must have: const actionDef = { ... }
    if not re.search(r"const\s+actionDef\s*=\s*\{", code):
        return None

    # Must have: qcmd.action_name = actionDef
    if not re.search(r"qcmd\.\w+\s*=\s*actionDef", code):
        return None

    # Extract the action definition object
    obj_start: Optional[re.Match] = re.search(r"const\s+actionDef\s*=\s*\{", code)
    if not obj_start:
        return None

    # Balance braces to find the matching closing brace
    start_pos: int = obj_start.end() - 1  # Position of opening {
    brace_count: int = 0
    pos: int = start_pos

    while pos < len(code):
        if code[pos] == "{":
            brace_count += 1
        elif code[pos] == "}":
            brace_count -= 1
            if brace_count == 0:
                break
        pos += 1

    if brace_count != 0:
        return None

    # Extract the object code including braces
    obj_code: str = code[start_pos : pos + 1]

    # Check if execute function exists
    execute_match: Optional[re.Match] = re.search(r"execute:\s*\(", obj_code)
    if not execute_match:
        # No execute function - just return the definition
        return {"def_code": f"const actionDef = {obj_code};", "execute": None}

    # Extract execute function
    execute_start: int = execute_match.start()

    # Find where execute function starts: execute: (params) => {
    arrow_match: Optional[re.Match] = re.search(
        r"execute:\s*(\(.*?\)\s*=>\s*\{)", obj_code[execute_start:], re.DOTALL
    )
    if not arrow_match:
        return None

    func_body_start: int = (
        execute_start + arrow_match.end() - 1
    )  # Position of opening {

    # Balance braces to find execute function body
    brace_count: int = 1
    pos: int = func_body_start + 1

    while pos < len(obj_code) and brace_count > 0:
        if obj_code[pos] == "{":
            brace_count += 1
        elif obj_code[pos] == "}":
            brace_count -= 1
        pos += 1

    if brace_count != 0:
        return None

    # Extract execute arrow function (params) => { body }
    execute_code: str = obj_code[execute_start + len("execute:") : pos]

    # Remove execute from def_code
    # Find if there's a comma before or after execute
    before: str = obj_code[:execute_start]
    after: str = obj_code[pos:]

    # Clean up comma handling to avoid double commas
    # But preserve trailing commas (prettier wants them)
    if after.lstrip().startswith(",") and before.rstrip().endswith(","):
        # Would create double comma, remove one
        after = after.lstrip()[1:]

    # Strip whitespace from join point to avoid extra blank lines
    before = before.rstrip()
    after = after.lstrip()
    def_code: str = before + "\n" + after

    # Build complete def_code statement
    return {
        "def_code": f"const actionDef = {def_code};",
        "execute": execute_code,
    }


class ActionPlugin:
    """Plugin for handling action nodes (qcmd.name)"""

    def get_name(self) -> str:
        return "action"

    def get_priority(self) -> Optional[int]:
        return None  # Use filename prefix (20)

    def get_plugin_type(self) -> str:
        return "explode"

    def can_handle_node(self, node: Dict[str, Any]) -> bool:
        """Check if this node is an action"""
        if node.get("type") != "function":
            return False

        func_code: str = node.get("func", "")
        # Only used during explode (rebuild is file-driven). An empty func must
        # not match: claiming it would also swallow initialize/finalize code
        # without writing it anywhere.
        return parse_action_definition(func_code) is not None

    def get_claimed_fields(self, node: Dict[str, Any]) -> List[str]:
        """Claim all fields this plugin generates during rebuild"""
        return ["func", "initialize", "finalize"]

    def is_metadata_file(self, filename: str) -> bool:
        """Check if filename is a metadata file (not a primary node definition)"""
        return (
            filename.endswith(".action.json")
            or filename.endswith(".def.js")
            or filename.endswith(".execute.js")
        )

    def can_infer_node_type(self, node_dir: Path, node_id: str) -> Optional[str]:
        """Infer node type from files, returns None if can't infer"""
        # Actions are always function nodes
        def_file = node_dir / f"{node_id}.def.js"
        if def_file.exists():
            return "function"
        return None

    def explode_node(self, node: Dict[str, Any], node_dir: Path) -> List[str]:
        """Explode action to .def.js and .execute.js files

        Returns:
            List of created filenames
        """
        try:
            node_id: str = node.get("id")
            node_name: str = node.get("name", "Unnamed")
            func_code: str = node.get("func", "")
            created_files: List[str] = []

            action_data: Optional[Dict[str, Any]] = parse_action_definition(func_code)
            if not action_data:
                return []

            action_name: str = to_snake_case(
                node_name
            )  # Actions use snake_case everywhere

            # Write definition file with export default
            def_code: str = action_data["def_code"]
            def_file: Path = node_dir / f"{node_id}.def.js"
            # Add export default on separate line (can't export default const in one line)
            def_file.write_text(f"{def_code}\nexport default actionDef;\n")
            created_files.append(f"{node_id}.def.js")

            # Write execute file if it exists
            execute_code: Optional[str] = action_data["execute"]
            if execute_code:
                # Convert arrow function to function declaration
                result: Optional[Tuple[str, str]] = extract_function_body(
                    execute_code, r"\((.*?)\)\s*=>\s*{"
                )
                if result:
                    params: str
                    body: str
                    params, body = result
                    execute_func: str = (
                        f"export default function {action_name}({params}) {{{body}}}"
                    )

                    execute_file: Path = node_dir / f"{node_id}.execute.js"
                    execute_file.write_text(execute_func + "\n")
                    created_files.append(f"{node_id}.execute.js")

            return created_files

        except Exception as e:
            print(
                f"⚠ Warning: action plugin failed for {node.get('id', 'unknown')}: {e}"
            )
            return []

    def rebuild_node(
        self, node_id: str, node_dir: Path, skeleton: Dict[str, Any]
    ) -> Dict[str, str]:
        """Rebuild action from .def.js and .execute.js files"""
        def_file: Path = node_dir / f"{node_id}.def.js"
        execute_file: Path = node_dir / f"{node_id}.execute.js"

        if not def_file.exists():
            return {}

        def_code: str = def_file.read_text()
        # Strip export default line if present
        def_code = re.sub(
            r"^\s*export\s+default\s+actionDef;\s*$", "", def_code, flags=re.MULTILINE
        )

        node_name: str = skeleton.get("name", "Unnamed")
        action_name: str = to_snake_case(node_name)  # Actions use snake_case everywhere

        # Extract definition object (between { and })
        def_match: Optional[re.Match] = re.search(
            r"const\s+actionDef\s*=\s*(\{.*\});", def_code, re.DOTALL
        )
        if not def_match:
            return {}

        def_obj: str = def_match.group(1)

        # If execute file exists, insert it into definition
        if execute_file.exists():
            execute_code: str = execute_file.read_text()
            # Strip export default if present
            execute_code = re.sub(
                r"^export\s+default\s+", "", execute_code, flags=re.MULTILINE
            )

            # Convert function declaration back to arrow function
            result: Optional[Tuple[str, str]] = extract_function_body(
                execute_code, r"function\s+\w+\s*\((.*?)\)\s*{"
            )
            if result:
                params: str
                body: str
                params, body = result
                execute_arrow: str = f"({params}) => {{{body}}}"

                # Insert execute into definition object
                # Find the closing brace and insert before it
                close_brace: int = def_obj.rfind("}")
                if close_brace != -1:
                    before: str = def_obj[:close_brace]
                    # Add comma if needed
                    if not before.rstrip().endswith(","):
                        before += ","
                    def_obj = f"{before}\n  execute: {execute_arrow}\n}}"

        # Build templates (empty initialize - all work happens in func)
        init_template: str = ""

        func_template: str = f"""// Define action
const actionDef = {def_obj};

// Store in global context
const qcmd = global.get("qcmd") || {{}};
qcmd.{action_name} = actionDef;
global.set("qcmd", qcmd);

node.status({{ fill: "blue", shape: "dot", text: "{action_name} loaded" }});
return msg;"""

        finalize_template: str = f"""// Cleanup: Remove action from global context
const qcmd = global.get("qcmd") || {{}};
delete qcmd.{action_name};
global.set("qcmd", qcmd);"""

        return {
            "initialize": init_template,
            "func": func_template,
            "finalize": finalize_template,
        }
