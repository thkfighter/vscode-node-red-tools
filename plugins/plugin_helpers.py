"""
Plugin Helper Functions

Shared utility functions that can be used by any plugin.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

# Import security utilities
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from helper.utils import validate_path_for_subprocess
from helper.constants import SUBPROCESS_TIMEOUT


def to_camel_case(name: str) -> str:
    """Convert node name to camelCase for function/action name.

    Args:
        name: Node name (e.g., "func_build_action" or "Build Action")

    Returns:
        camelCase name (e.g., "buildAction")
    """
    words: List[str] = re.sub(r"[^a-zA-Z0-9]+", " ", name).split()
    if not words:
        return "unnamed"
    return words[0].lower() + "".join(word.capitalize() for word in words[1:])


def to_snake_case(name: str) -> str:
    """Convert node name to snake_case for action registration.

    Args:
        name: Node name (e.g., "func_build_action" or "Build Action")

    Returns:
        snake_case name (e.g., "build_action")
    """
    words: List[str] = re.sub(r"[^a-zA-Z0-9]+", " ", name).split()
    if not words:
        return "unnamed"
    return "_".join(word.lower() for word in words)


def extract_function_body(code: str, start_pattern: str) -> Optional[Tuple[str, str]]:
    r"""Extract params and body from a function using brace balancing.

    Args:
        code: Source code containing function
        start_pattern: Regex pattern to match function start (must capture params and end with {)
                      Example: r"function\s+\w+\s*\((.*?)\)\s*{"
                      Example: r"\((.*?)\)\s*=>\s*{"

    Returns:
        (params, body) tuple where:
        - params: function parameters as string
        - body: function body content (between opening and closing braces)

        Returns None if pattern doesn't match or braces are unbalanced.

    Example:
        >>> code = "function foo(a, b) { return a + b; }"
        >>> extract_function_body(code, r"function\s+\w+\s*\((.*?)\)\s*{")
        ("a, b", " return a + b; ")
    """
    match: Optional[re.Match] = re.search(start_pattern, code, re.DOTALL)
    if not match:
        return None

    params: str = match.group(1)
    body_start: int = match.end() - 1  # Position of opening {

    # Balance braces to find function body
    brace_count: int = 0
    pos: int = body_start
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

    body: str = code[body_start + 1 : pos]
    return (params, body)


def run_prettier(filepath: Path) -> bool:
    """Run prettier on a file.

    Args:
        filepath: Path to file to format

    Returns:
        True if formatting succeeded, False otherwise
    """
    try:
        # Validate path exists and length before passing to subprocess
        validated_filepath: Path = validate_path_for_subprocess(
            filepath, filepath.parent
        )

        result: subprocess.CompletedProcess = subprocess.run(
            [
                "npx",
                "prettier",
                "--trailing-comma",
                "es5",
                "--write",
                str(validated_filepath),
            ],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=SUBPROCESS_TIMEOUT,
            shell=(os.name == "nt"),  # Windows needs a shell to resolve npx.cmd
        )
        return True
    except FileNotFoundError:
        print(
            f"⚠ Warning: prettier not found (npx command failed) - skipping formatting for {filepath.name}"
        )
        return False
    except subprocess.CalledProcessError as e:
        print(f"⚠ Warning: prettier failed for {filepath.name}")
        if e.stderr:
            # Print first few lines of error
            error_lines: List[str] = e.stderr.strip().split("\n")
            for line in error_lines[:3]:
                print(f"  {line}")
            if len(error_lines) > 3:
                print(f"  ... ({len(error_lines) - 3} more lines)")
        return False
    except Exception as e:
        print(f"⚠ Warning: unexpected error running prettier for {filepath.name}: {e}")
        return False


def run_prettier_parallel(
    directory: Path, additional_files: Optional[List[Path]] = None
) -> bool:
    """Run prettier on a directory in parallel.

    For root files in directory: formats them as a list in one thread.
    For subdirectories: passes each directory to prettier (recursive) in parallel threads.

    Args:
        directory: Directory to format (typically src_dir)
        additional_files: Optional list of additional files to include (e.g., flows.json)
                         These files are validated against their own parent directories

    Returns:
        True if any formatting succeeded, False otherwise
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Collect root files (files directly in directory, not in subdirs)
    # Include ALL files (including hidden) - let prettier handle what it can format
    root_files: List[Path] = []
    for item in directory.iterdir():
        if item.is_file():
            root_files.append(item)

    # Add additional files (e.g., flows.json) to root files list
    if additional_files:
        root_files.extend(additional_files)

    # Collect subdirectories (skip .orphaned)
    subdirs: List[Path] = []
    for item in directory.iterdir():
        if item.is_dir() and item.name != ".orphaned":
            subdirs.append(item)

    if not root_files and not subdirs:
        return False

    # Worker function to format root files
    def format_root_files(files: List[Path]) -> bool:
        """Format root files as a list"""
        if not files:
            return False

        try:
            # Validate all file paths before passing to subprocess
            # Files from directory (src files) are validated against directory's parent
            # Additional files (like flows.json) are validated against their own parent
            validated_files: List[str] = []
            for f in files:
                try:
                    # Determine appropriate validation directory based on file location
                    # If file is within directory structure, validate against directory's parent
                    # Otherwise (like flows.json), validate against file's own parent
                    validation_root: Path
                    try:
                        f.relative_to(directory)
                        # File is within directory - validate against directory's parent
                        validation_root = directory.parent
                    except ValueError:
                        # File is outside directory (e.g., flows.json) - validate against its parent
                        validation_root = f.parent

                    validated: Path = validate_path_for_subprocess(f, validation_root)
                    validated_files.append(str(validated))
                except ValueError as e:
                    print(f"⚠ Warning: skipping file {f.name}: {e}")
                    continue

            if not validated_files:
                return False

            # Pass all root files to prettier at once
            subprocess.run(
                ["npx", "prettier", "--trailing-comma", "es5", "--write"]
                + validated_files,
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                check=True,
                timeout=SUBPROCESS_TIMEOUT,
                shell=(os.name == "nt"),  # Windows needs a shell to resolve npx.cmd
            )
            return True
        except Exception as e:
            print(f"⚠ Warning: prettier failed for root files: {e}")
            return False

    # Worker function to format a subdirectory
    def format_directory(subdir: Path) -> bool:
        """Format a subdirectory recursively"""
        try:
            # Validate directory path before passing to subprocess
            # Subdirectories are part of directory structure, validate against directory's parent
            validated_subdir: Path = validate_path_for_subprocess(
                subdir, directory.parent
            )

            # Pass directory to prettier (it handles recursion)
            subprocess.run(
                [
                    "npx",
                    "prettier",
                    "--trailing-comma",
                    "es5",
                    "--write",
                    str(validated_subdir),
                ],
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                check=True,
                timeout=SUBPROCESS_TIMEOUT,
                shell=(os.name == "nt"),  # Windows needs a shell to resolve npx.cmd
            )
            return True
        except Exception as e:
            print(f"⚠ Warning: prettier failed for {subdir.name}: {e}")
            return False

    # Build work items
    work_items: List[Tuple[str, Any]] = []
    if root_files:
        work_items.append(("root", root_files))
    for subdir in subdirs:
        work_items.append(("dir", subdir))

    # Process in parallel if multiple work items
    if len(work_items) > 1:
        any_success: bool = False
        with ThreadPoolExecutor() as executor:
            futures: List[Any] = []
            for work_type, work_data in work_items:
                if work_type == "root":
                    futures.append(executor.submit(format_root_files, work_data))
                else:
                    futures.append(executor.submit(format_directory, work_data))

            for future in as_completed(futures):
                if future.result():
                    any_success = True

        return any_success
    else:
        # Single work item - process sequentially
        work_type: str
        work_data: Any
        work_type, work_data = work_items[0]
        if work_type == "root":
            return format_root_files(work_data)
        else:
            return format_directory(work_data)
