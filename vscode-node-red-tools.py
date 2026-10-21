#!/usr/bin/env python3
"""
vscode-node-red-tools.py - Unified Node-RED development tool

Combines explode, rebuild, and watch functionality into a single tool
with plugin support for extensibility.

Usage:
    # Explode flows to source files
    python vscode-node-red-tools.py explode flows/flows.json

    # Rebuild flows from source files
    python vscode-node-red-tools.py rebuild flows/flows.json

    # Watch mode (bidirectional sync)
    python vscode-node-red-tools.py watch --server https://server:1880 --username admin --password pass
"""

from __future__ import annotations

__version__ = "3.0.0"

import argparse
import json5 as json
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

# Import from helper modules
from helper import (
    # Logging
    log_error,
    # Config
    validate_config,
    # Rebuild
    rebuild_flows,
    # Explode
    explode_flows,
    # Diff
    diff_flows,
    # Watcher
    WATCH_AVAILABLE,
    watch_mode,
)

# Import exit codes
from helper.exit_codes import (
    SUCCESS,
    GENERAL_ERROR,
    KEYBOARD_INTERRUPT,
)

# Import command implementations
from helper.commands import (
    stats_command,
    benchmark_command,
    verify_flows,
)

from helper.commands_plugin import (
    new_plugin_command,
    list_plugins_command,
)

from helper.initialize import initialize_system


# ============================================================================
# Main CLI
# ============================================================================


def main() -> int:
    """Main CLI entry point."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Unified Node-RED development tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"vscode-node-red-tools {__version__}",
    )

    # Global options (apply to all commands)
    parser.add_argument(
        "--config",
        type=str,
        help="Path to config file (overrides standard locations)",
    )
    parser.add_argument(
        "--flows",
        default="flows/flows.json",
        help="Path to flows.json file (default: flows/flows.json)",
    )
    parser.add_argument(
        "--src",
        default="src",
        help="Path to source directory (default: src)",
    )

    # Global plugin control options
    parser.add_argument(
        "--enable",
        type=str,
        help="Comma-separated list of plugins to enable, or 'all' (overrides config)",
    )
    parser.add_argument(
        "--disable",
        type=str,
        help="Comma-separated list of plugins to disable, or 'all' to disable all",
    )

    # Global logging control options
    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress info messages (warnings and errors only)",
    )
    log_group.add_argument(
        "--verbose",
        action="store_true",
        help="Show debug messages",
    )
    log_group.add_argument(
        "--log-level",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set log level explicitly (overrides --quiet/--verbose and NODERED_TOOLS_LOG_LEVEL)",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Explode command
    explode_parser = subparsers.add_parser(
        "explode", help="Explode flows.json into source files"
    )
    explode_parser.add_argument(
        "--backup",
        action="store_true",
        help="Create timestamped backup before exploding",
    )
    explode_parser.add_argument(
        "--delete-orphaned",
        action="store_true",
        help="Delete orphaned files instead of moving to .orphaned/",
    )
    explode_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )

    # Rebuild command
    rebuild_parser = subparsers.add_parser(
        "rebuild", help="Rebuild flows.json from source files"
    )
    rebuild_parser.add_argument(
        "--backup",
        action="store_true",
        help="Create timestamped backup before rebuilding",
    )
    rebuild_parser.add_argument(
        "--orphan-new",
        action="store_true",
        help="Move new files (not in skeleton) to .orphaned/",
    )
    rebuild_parser.add_argument(
        "--delete-new", action="store_true", help="Delete new files (not in skeleton)"
    )
    rebuild_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files",
    )
    rebuild_parser.add_argument(
        "--sort-keys",
        action="store_true",
        help="Recursively sort JSON object keys in the output for a canonical, "
        "diff-friendly flows.json (node array order is preserved)",
    )

    # Verify command
    verify_parser = subparsers.add_parser(
        "verify", help="Verify round-trip stability (explode → rebuild → compare)"
    )

    # List-plugins command
    list_plugins_parser = subparsers.add_parser(
        "list-plugins", help="List all available plugins with status and priority"
    )

    # Diff command
    diff_parser = subparsers.add_parser(
        "diff",
        help="Compare src, flow, or server",
    )
    diff_parser.add_argument(
        "source", choices=["src", "flow", "server"], help="Source to compare from"
    )
    diff_parser.add_argument(
        "target", choices=["src", "flow", "server"], help="Target to compare to"
    )
    diff_parser.add_argument(
        "--server",
        help="Node-RED server URL (default: http://127.0.0.1:1880 for server comparisons)",
    )
    diff_parser.add_argument(
        "--username",
        help="Node-RED username (for basic auth)",
    )
    diff_parser.add_argument(
        "--password",
        help="Node-RED password (INSECURE - use NODERED_PASSWORD env var instead)",
    )
    diff_parser.add_argument(
        "--token",
        help="Bearer token (INSECURE - use NODERED_TOKEN env var or token file instead)",
    )
    diff_parser.add_argument(
        "--token-file",
        help="Path to file containing bearer token",
    )
    diff_parser.add_argument(
        "--no-verify-ssl", action="store_true", help="Disable SSL verification"
    )
    diff_parser.add_argument(
        "--bcomp", action="store_true", help="Launch Beyond Compare for visual diff"
    )
    diff_parser.add_argument(
        "-C",
        "--context",
        type=int,
        default=3,
        help="Number of context lines to show in unified diff (default: 3)",
    )

    # Watch command
    watch_parser = subparsers.add_parser(
        "watch",
        help="Watch mode - bidirectional sync",
    )
    watch_parser.add_argument(
        "--server",
        help="Node-RED server URL (default: http://127.0.0.1:1880)",
    )
    watch_parser.add_argument(
        "--username",
        help="Node-RED username (for basic auth)",
    )
    watch_parser.add_argument(
        "--password",
        help="Node-RED password (INSECURE - use NODERED_PASSWORD env var instead)",
    )
    watch_parser.add_argument(
        "--token",
        help="Bearer token (INSECURE - use NODERED_TOKEN env var or token file instead)",
    )
    watch_parser.add_argument(
        "--token-file",
        help="Path to file containing bearer token",
    )
    watch_parser.add_argument(
        "--no-verify-ssl", action="store_true", help="Disable SSL verification"
    )
    watch_parser.add_argument(
        "--dashboard", action="store_true", help="Enable visual dashboard mode"
    )

    # Stats command
    stats_parser = subparsers.add_parser(
        "stats", help="Display comprehensive flow and source statistics"
    )

    # Benchmark command
    benchmark_parser = subparsers.add_parser(
        "benchmark", help="Benchmark explode and rebuild performance"
    )
    benchmark_parser.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=3,
        help="Number of iterations (default: 3)",
    )

    # New-plugin command
    new_plugin_parser = subparsers.add_parser(
        "new-plugin", help="Generate a new plugin scaffold"
    )
    new_plugin_parser.add_argument("name", help="Plugin name (e.g., MyPlugin)")
    new_plugin_parser.add_argument(
        "type",
        choices=[
            "pre-explode",
            "explode",
            "post-explode",
            "pre-rebuild",
            "post-rebuild",
        ],
        help="Plugin type",
    )
    new_plugin_parser.add_argument(
        "--priority",
        "-p",
        type=int,
        help="Plugin priority (default: auto based on type)",
    )

    # Validate-config command
    validate_config_parser = subparsers.add_parser(
        "validate-config",
        help="Validate configuration file and test credential resolution",
    )
    validate_config_parser.add_argument(
        "--test-auth",
        action="store_true",
        help="Test authentication credential resolution",
    )

    # Enable shell completion if argcomplete is available
    try:
        import argcomplete

        argcomplete.autocomplete(parser)
    except ImportError:
        pass  # Shell completion is optional

    args: argparse.Namespace = parser.parse_args()

    # Set logging level from CLI args (before any logging occurs)
    from helper import LogLevel, set_log_level

    if hasattr(args, "log_level") and args.log_level:
        level_map: Dict[str, Any] = {
            "DEBUG": LogLevel.DEBUG,
            "INFO": LogLevel.INFO,
            "WARNING": LogLevel.WARNING,
            "ERROR": LogLevel.ERROR,
        }
        set_log_level(level_map[args.log_level])
    elif hasattr(args, "quiet") and args.quiet:
        set_log_level(LogLevel.WARNING)
    elif hasattr(args, "verbose") and args.verbose:
        set_log_level(LogLevel.DEBUG)
    # Otherwise use environment variable (already set in logging module init)

    try:
        # --- Main command dispatch ---
        # Setup (config, server_client, plugins)
        init: Optional[Tuple[Dict[str, Any], Dict[str, List[Any]], Any]] = (
            initialize_system(args)
        )
        if init is None:
            return GENERAL_ERROR
        config: Dict[str, Any]
        plugins_dict: Dict[str, List[Any]]
        server_client: Any
        config, plugins_dict, server_client = init
        flows_path: Path = Path(args.flows).resolve()
        src_path: Path = Path(args.src).resolve()

        # Command dispatch

        # Commands that don't need plugins
        if args.command == "validate-config":
            return validate_config(config, server_client, args)
        elif args.command == "new-plugin":
            priority: Optional[int] = (
                args.priority if hasattr(args, "priority") else None
            )
            return new_plugin_command(args.name, args.type, priority)
        elif args.command == "list-plugins":
            return list_plugins_command(plugins_dict, config)
        elif args.command == "watch":
            return watch_mode(
                args,
                flows_path,
                src_path,
                plugins_dict=plugins_dict,
                config=config,
                server_client=server_client,
            )
        elif args.command == "explode":
            return explode_flows(
                flows_path,
                src_path,
                backup=args.backup,
                delete_orphaned=args.delete_orphaned,
                dry_run=args.dry_run,
                plugins_dict=plugins_dict,
            )
        elif args.command == "rebuild":
            return rebuild_flows(
                flows_path,
                src_path,
                backup=args.backup,
                orphan_new=args.orphan_new,
                delete_new=args.delete_new,
                dry_run=args.dry_run,
                plugins_dict=plugins_dict,
                sort_keys=args.sort_keys,
            )
        elif args.command == "verify":
            return verify_flows(
                flows_path,
                plugins_dict=plugins_dict,
                config=config,
            )
        elif args.command == "diff":
            return diff_flows(
                args.source,
                args.target,
                flows_path,
                src_path,
                server_client,
                args.bcomp,
                plugins_dict=plugins_dict,
                context=args.context,
            )
        elif args.command == "stats":
            return stats_command(
                flows_path,
                src_path,
                plugins_dict=plugins_dict,
                config=config,
            )
        elif args.command == "benchmark":
            return benchmark_command(
                flows_path,
                src_path,
                plugins_dict=plugins_dict,
                config=config,
                iterations=args.iterations,
            )
        else:
            log_error(f"Unknown command: {args.command}", code=GENERAL_ERROR)
            return GENERAL_ERROR

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return KEYBOARD_INTERRUPT
    except Exception as e:
        log_error(f"Error: {e}", code=GENERAL_ERROR)
        import traceback

        traceback.print_exc()
        return GENERAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
