"""
Rebuild operations for vscode-node-red-tools

Handles reconstruction of flows.json from exploded source files with:
- Plugin-based node rebuilding
- Parallel processing for large flows
- Pre/post-rebuild plugin stages
"""

import inspect
import json5 as json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any

from .file_ops import find_new_files, handle_new_files
from .logging import (
    create_progress_context,
    log_error,
    log_info,
    log_success,
    show_progress_bar,
)
from .exit_codes import SUCCESS, FILE_NOT_FOUND, REBUILD_ERROR, FILE_INVALID
from .skeleton import get_node_directory, load_skeleton
from .utils import (
    create_backup,
    validate_safe_path,
    read_json_with_size_limit,
)
from .constants import (
    MAX_NODE_FILE_SIZE,
    DEFAULT_MAX_WORKERS,
    PARALLEL_THRESHOLD,
)


def sort_keys_recursive(obj: Any) -> Any:
    """Return a copy of obj with all dict keys sorted alphabetically (recursively).

    Object (dict) keys are reordered; list/array order is left untouched, so
    node array order in flows.json is preserved. Scalars are returned as-is.
    Used by rebuild's --sort-keys option to produce a canonical, diff-friendly
    key order without changing flow behavior (Node-RED ignores key order).
    """
    if isinstance(obj, dict):
        return {k: sort_keys_recursive(obj[k]) for k in sorted(obj.keys())}
    if isinstance(obj, list):
        return [sort_keys_recursive(item) for item in obj]
    return obj


def rebuild_single_node(
    node_id: str,
    node_dir: Path,
    skeleton: dict,
    base_json_exists: bool,
    explode_plugins: List[Any],
    src_dir: Optional[Path] = None,
) -> dict:
    """Rebuild a single node from its files (used by explode for verification)

    Args:
        node_id: Node ID
        node_dir: Directory containing node files
        skeleton: Skeleton node data
        base_json_exists: Whether the .json file exists
        explode_plugins: List of explode plugins
        src_dir: Source directory root (for path validation)

    Returns:
        Rebuilt node dictionary

    Raises:
        ValueError: If path traversal is detected
    """
    # Start with skeleton (preserves structure and field order)
    node_data = skeleton.copy()

    # Remove metadata fields from rebuilt node
    node_data.pop("_explode_meta", None)

    # Load base .json file if it exists
    if base_json_exists:
        node_json_file = node_dir / f"{node_id}.json"
        if node_json_file.exists():
            # Validate path before reading (security check)
            if src_dir:
                node_json_file = validate_safe_path(src_dir, node_json_file)
            # Read with size limit to prevent memory exhaustion
            try:
                base_data = read_json_with_size_limit(
                    node_json_file, MAX_NODE_FILE_SIZE
                )
                node_data.update(base_data)
            except ValueError as e:
                # Log error but don't fail - node will use skeleton data
                log_error(f"Cannot load node file {node_id}: {e}", code=FILE_INVALID)

    # Track claimed fields
    claimed_fields = set()

    # Rebuild from plugins (pass node_data which has skeleton + .json merged)
    for plugin in explode_plugins:
        plugin_data = plugin.rebuild_node(node_id, node_dir, node_data)

        if plugin_data:
            plugin_fields = set(plugin.get_claimed_fields(node_data))

            # Check for conflicts
            if plugin_fields & claimed_fields:
                continue

            # Filter to only claimed fields (issue #10 - improve field claiming)
            filtered_data = {k: v for k, v in plugin_data.items() if k in plugin_fields}

            claimed_fields.update(plugin_fields)
            node_data.update(filtered_data)

    return node_data


def _run_pre_rebuild_stage(
    pre_rebuild_plugins: List[Any],
    src_dir: Path,
    quiet_plugins: bool,
    continued_from_explode: bool = False,
    progress_task: Optional[Tuple] = None,
) -> None:
    """Run pre-rebuild plugins to modify source files

    Args:
        pre_rebuild_plugins: List of pre-rebuild plugins
        src_dir: Source directory
        quiet_plugins: Whether to suppress plugin messages
        continued_from_explode: True if rebuilding immediately after explode
        progress_task: Optional (progress, task_id) tuple for rich progress tracking
    """
    if pre_rebuild_plugins:
        log_info("Stage 1: Running pre-rebuild plugins...")
        for plugin in pre_rebuild_plugins:
            if not quiet_plugins:
                log_info(f"  {plugin.get_name()}")

            # Check if plugin supports continuation flag
            sig = inspect.signature(plugin.process_directory_pre_rebuild)
            if "continued_from_explode" in sig.parameters:
                plugin.process_directory_pre_rebuild(
                    src_dir, continued_from_explode=continued_from_explode
                )
            else:
                # Plugin doesn't support flag, call without it
                plugin.process_directory_pre_rebuild(src_dir)

            # Update progress
            if progress_task:
                progress, task_id = progress_task
                progress.update(task_id, advance=1)
    else:
        log_info("Stage 1: No pre-rebuild plugins")


def _rebuild_single_node(
    idx: int,
    skeleton_node: dict,
    explode_plugins: List[Any],
    src_dir: Path,
    tab_ids: set,
) -> Tuple[int, dict]:
    """Rebuild a single node from skeleton and source files (thread-safe worker function)

    Args:
        idx: Node index (for ordering results)
        skeleton_node: Skeleton node data
        explode_plugins: List of explode plugins (used for rebuild)
        src_dir: Source directory
        tab_ids: Set of tab/subflow IDs

    Returns:
        tuple of (idx, rebuilt_node)

    Raises:
        ValueError: If path traversal is detected
    """
    node_id = skeleton_node["id"]

    # Start with skeleton (preserves structure and field order)
    node_data = skeleton_node.copy()

    # Determine node directory
    node_dir = get_node_directory(skeleton_node, src_dir, tab_ids)
    node_json_file = node_dir / f"{node_id}.json"

    if node_json_file.exists():
        # Validate path before reading (security check)
        safe_json_file = validate_safe_path(src_dir, node_json_file)
        # .json file exists - merge functional fields (with size limit)
        try:
            base_data = read_json_with_size_limit(safe_json_file, MAX_NODE_FILE_SIZE)
            # Merge functional fields (preserves skeleton's field order)
            node_data.update(base_data)
        except ValueError as e:
            # Log error but don't fail - node will use skeleton data
            log_error(f"Cannot load node file {node_id}: {e}", code=FILE_INVALID)

    # Track claimed fields to prevent conflicts
    claimed_fields = set()

    # Merge plugin data (multiple plugins can process same node)
    for plugin in explode_plugins:
        # Try to rebuild node data (pass node_data which has skeleton + .json merged)
        plugin_data = plugin.rebuild_node(node_id, node_dir, node_data)

        # If plugin returned data, claim fields and merge
        if plugin_data:
            # Get claimed fields from the plugin
            plugin_fields = set(plugin.get_claimed_fields(node_data))

            # Check for field conflicts
            if plugin_fields & claimed_fields:
                # Skip this plugin - another plugin already claimed these fields
                continue

            # Claim fields and merge data
            claimed_fields.update(plugin_fields)
            node_data.update(plugin_data)

    # Remove metadata before adding to rebuilt nodes (metadata is for skeleton only)
    node_data.pop("_explode_meta", None)

    return (idx, node_data)


def _rebuild_nodes_stage(
    skeleton_data: List[dict],
    explode_plugins: List[Any],
    src_dir: Path,
    quiet_plugins: bool,
    max_workers: Optional[int] = DEFAULT_MAX_WORKERS,
    progress_task: Optional[Tuple] = None,
) -> List[dict]:
    """Rebuild nodes from skeleton and source files (with optional parallel processing)

    Args:
        skeleton_data: Skeleton data array
        explode_plugins: List of explode plugins (used for rebuild)
        src_dir: Source directory
        quiet_plugins: Whether to suppress plugin messages
        max_workers: Max worker threads (None = cpu_count, 1 = sequential)
        progress_task: Optional (progress, task_id) tuple for rich progress tracking

    Returns:
        List of rebuilt node dictionaries
    """
    log_info("Stage 2: Rebuilding nodes from src...")

    # Get tab/subflow IDs for directory resolution
    tab_ids = {
        node["id"] for node in skeleton_data if node.get("type") in ["tab", "subflow"]
    }

    # Progress reporting
    total_nodes = len(skeleton_data)

    # Decide whether to use parallel processing
    use_parallel = max_workers != 1 and total_nodes >= PARALLEL_THRESHOLD

    if use_parallel and max_workers is None:
        max_workers = os.cpu_count() or 4

    # Shared state for thread-safe operations
    progress_lock = threading.Lock()
    progress_counter = [0]
    show_progress = total_nodes > 50

    def update_progress():
        """Thread-safe progress update"""
        with progress_lock:
            progress_counter[0] += 1
            if progress_task:
                progress, task_id = progress_task
                progress.update(task_id, advance=1)
            elif show_progress:
                show_progress_bar(progress_counter[0], total_nodes, "Rebuilding nodes")

    # Process nodes
    if use_parallel:
        # Parallel processing with ThreadPoolExecutor
        rebuilt_nodes = [None] * total_nodes  # Pre-allocate to maintain order

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks
            futures = {
                executor.submit(
                    _rebuild_single_node,
                    idx,
                    skeleton_node,
                    explode_plugins,
                    src_dir,
                    tab_ids,
                ): idx
                for idx, skeleton_node in enumerate(skeleton_data)
            }

            # Collect results as they complete
            for future in as_completed(futures):
                idx, node_data = future.result()
                rebuilt_nodes[idx] = node_data
                update_progress()

    else:
        # Sequential processing (original behavior)
        rebuilt_nodes = []

        for idx, skeleton_node in enumerate(skeleton_data):
            idx_result, node_data = _rebuild_single_node(
                idx,
                skeleton_node,
                explode_plugins,
                src_dir,
                tab_ids,
            )
            rebuilt_nodes.append(node_data)
            update_progress()

    return rebuilt_nodes


def _run_post_rebuild_stage(
    post_rebuild_plugins: List[Any],
    flows_path: Path,
    quiet_plugins: bool,
    progress_task: Optional[Tuple] = None,
) -> None:
    """Run post-rebuild plugins to format flows file

    Args:
        post_rebuild_plugins: List of post-rebuild plugins
        flows_path: Flows file path
        quiet_plugins: Whether to suppress plugin messages
        progress_task: Optional (progress, task_id) tuple for rich progress tracking
    """
    if post_rebuild_plugins:
        log_info("Stage 3: Running post-rebuild plugins...")
        for plugin in post_rebuild_plugins:
            if not quiet_plugins:
                log_info(f"  {plugin.get_name()}")
            plugin.process_flows_post_rebuild(flows_path)

            # Update progress
            if progress_task:
                progress, task_id = progress_task
                progress.update(task_id, advance=1)
    else:
        log_info("Stage 3: No post-rebuild plugins")


def rebuild_flows(
    flows_path: Path,
    src_dir: Path,
    backup: bool = False,
    orphan_new: bool = False,
    delete_new: bool = False,
    quiet_plugins: bool = False,
    continued_from_explode: bool = False,
    dry_run: bool = False,
    plugins_dict: Optional[Dict[str, List[Any]]] = None,
    sort_keys: bool = False,
) -> int:
    """Rebuild flows.json from source files

    Args:
        flows_path: Path to flows.json file to create
        src_dir: Source directory with exploded files
        backup: Create timestamped backup before rebuilding
        orphan_new: Move new files (not in skeleton) to .orphaned/
        delete_new: Delete new files (not in skeleton)
        quiet_plugins: Suppress plugin loading messages
        continued_from_explode: Skip redundant pre-rebuild work
        dry_run: Show what would change without writing files
        plugins_dict: Pre-loaded plugins dictionary (required)
        sort_keys: Recursively sort object keys in the output (node array
            order is preserved). Opt-in; produces a canonical, diff-friendly
            flows.json without affecting flow behavior.

    Returns:
        Exit code (SUCCESS = success, REBUILD_ERROR = error)
    """
    # Handle dry-run by using temp file and comparing
    if dry_run:
        import tempfile
        from . import _print_flows_diff  # Import helper if needed

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_flows = Path(temp_dir) / flows_path.name

            # Run rebuild in temp directory
            result = rebuild_flows(
                temp_flows,
                src_dir,
                backup=False,
                orphan_new=orphan_new,
                delete_new=delete_new,
                quiet_plugins=True,
                continued_from_explode=continued_from_explode,
                dry_run=False,
                plugins_dict=plugins_dict,
                sort_keys=sort_keys,
            )

            # Compare flows.json files
            if result == 0 and temp_flows.exists():
                try:
                    from . import _print_flows_diff

                    _print_flows_diff(flows_path, temp_flows)
                except:
                    log_info(f"Dry run complete - would write to {flows_path}")

            return result

    try:
        log_info(f"Rebuilding {flows_path} from {src_dir}/")

        # Create backup if requested
        if backup and flows_path.exists():
            create_backup(flows_path)

        # Require plugins_dict to be provided
        if plugins_dict is None:
            raise ValueError("plugins_dict is required - call preload_plugins() first")

        pre_rebuild_plugins = plugins_dict["pre-rebuild"]
        explode_plugins = plugins_dict["explode"]  # Also used for rebuild
        post_rebuild_plugins = plugins_dict["post-rebuild"]

        # Load skeleton first to determine progress context size
        try:
            skeleton_data, skeleton_map = load_skeleton(src_dir, flows_path)
        except FileNotFoundError as e:
            log_error(str(e), code=FILE_NOT_FOUND)
            return FILE_NOT_FOUND

        # Detect and handle new files (not in skeleton)
        new_files = find_new_files(src_dir, skeleton_data, explode_plugins)
        new_nodes = handle_new_files(
            new_files, src_dir, explode_plugins, orphan=orphan_new, delete=delete_new
        )

        # Add new nodes to skeleton data
        if new_nodes:
            skeleton_data.extend(new_nodes)
            # Update skeleton map with new nodes
            for node in new_nodes:
                skeleton_map[node["id"]] = node

        # STAGE 1: Run pre-rebuild plugins (with its own progress context)
        if pre_rebuild_plugins:
            with create_progress_context(True) as progress:
                task1 = progress.add_task(
                    "Pre-rebuild plugins", total=len(pre_rebuild_plugins)
                )
                _run_pre_rebuild_stage(
                    pre_rebuild_plugins,
                    src_dir,
                    quiet_plugins,
                    continued_from_explode,
                    progress_task=(progress, task1),
                )
        else:
            _run_pre_rebuild_stage(
                pre_rebuild_plugins,
                src_dir,
                quiet_plugins,
                continued_from_explode,
                progress_task=None,
            )

        # STAGE 2: Rebuild nodes (with its own progress context)
        with create_progress_context(True) as progress:
            task2 = progress.add_task("Rebuilding nodes", total=len(skeleton_data))
            rebuilt_nodes = _rebuild_nodes_stage(
                skeleton_data,
                explode_plugins,
                src_dir,
                quiet_plugins,
                progress_task=(progress, task2),
            )

        # Optionally sort object keys for a canonical, diff-friendly output.
        # Node array order is preserved; only keys within objects are reordered.
        if sort_keys:
            log_info("Sorting object keys (node array order preserved)...")
            rebuilt_nodes = sort_keys_recursive(rebuilt_nodes)

        # Write flows file - compact format
        flows_path.parent.mkdir(parents=True, exist_ok=True)
        flows_json = json.dumps(
            rebuilt_nodes, separators=(",", ":"), ensure_ascii=False, quote_keys=True
        )
        flows_path.write_text(flows_json + "\n")

        # STAGE 3: Run post-rebuild plugins (with its own progress context)
        if post_rebuild_plugins:
            with create_progress_context(True) as progress:
                task3 = progress.add_task(
                    "Post-rebuild plugins", total=len(post_rebuild_plugins)
                )
                _run_post_rebuild_stage(
                    post_rebuild_plugins,
                    flows_path,
                    quiet_plugins,
                    progress_task=(progress, task3),
                )

        log_success(f"Rebuilt {len(rebuilt_nodes)} nodes to {flows_path}")
        return SUCCESS

    except Exception as e:
        log_error(f"Rebuild failed: {e}", code=REBUILD_ERROR)
        import traceback

        traceback.print_exc()
        return REBUILD_ERROR
