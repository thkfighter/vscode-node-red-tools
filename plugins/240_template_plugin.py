"""
Template Plugin

Handles template extraction from various Node-RED template nodes:
- ui-template (Dashboard 2.0, @flowfuse/node-red-dashboard) - Vue SFC in "format" field
- ui_template (Dashboard 1, node-red-dashboard) - Angular template in "format" field
- template (core) - Mustache/HTML/JSON/YAML/etc in "template" field ("format" holds syntax name)

Extracts template content to appropriately named files for IDE support.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any, Optional


# Format to extension mapping for core template node
FORMAT_EXTENSIONS: Dict[str, str] = {
    "handlebars": ".mustache",
    "html": ".html",
    "json": ".json",
    "yaml": ".yaml",
    "javascript": ".js",
    "css": ".css",
    "markdown": ".md",
    "python": ".py",
    "sql": ".sql",
    "c_cpp": ".cpp",
    "java": ".java",
    "text": ".txt",
}

# Node types whose template content lives in the "format" field
UI_TEMPLATE_TYPES: List[str] = ["ui-template", "ui_template"]


class TemplatePlugin:
    """Plugin for handling template field extraction to files with appropriate extensions"""

    def get_name(self) -> str:
        return "template"

    def get_priority(self) -> Optional[int]:
        return None  # Use filename prefix (240)

    def get_plugin_type(self) -> str:
        return "explode"

    def _get_content_field(self, node_type: str) -> str:
        """Return the field holding template content for a node type.

        Dashboard 1 (ui_template) and Dashboard 2.0 (ui-template) store their
        template code in "format"; the core template node uses "template"
        (its "format" field is the syntax name, e.g. "handlebars").
        """
        return "format" if node_type in UI_TEMPLATE_TYPES else "template"

    def can_handle_node(self, node: Dict[str, Any]) -> bool:
        """Check if this node carries template content"""
        node_type: str = node.get("type", "")
        if node_type == "template":
            return "template" in node
        if node_type in UI_TEMPLATE_TYPES:
            return "format" in node
        return False

    def get_claimed_fields(self, node: Dict[str, Any]) -> List[str]:
        """Claim the field holding the template content"""
        return [self._get_content_field(node.get("type", ""))]

    def is_metadata_file(self, filename: str) -> bool:
        """Check if filename is a metadata file (not a primary node definition)"""
        # Template files are identifiable by their patterns
        return (
            filename.endswith(".vue")
            or filename.endswith(".ui-template.html")
            or ".template." in filename
        )

    def can_infer_node_type(self, node_dir: Path, node_id: str) -> Optional[str]:
        """Infer node type from files, returns None if can't infer"""
        # Check for Dashboard 2.0 (Vue)
        if (node_dir / f"{node_id}.vue").exists():
            return "ui-template"

        # Check for Dashboard 1 (Angular)
        if (node_dir / f"{node_id}.ui-template.html").exists():
            return "ui_template"

        # Check for core template node (has .template. in filename)
        for file in node_dir.glob(f"{node_id}.template.*"):
            return "template"

        return None

    def _get_template_extension(self, node: Dict[str, Any]) -> str:
        """Determine appropriate file extension based on node type and format"""
        node_type: str = node.get("type", "")

        if node_type == "ui-template":
            # Dashboard 2.0 - Vue SFC
            return ".vue"
        elif node_type == "ui_template":
            # Dashboard 1 - Angular templates
            return ".ui-template.html"
        elif node_type == "template":
            # Core template node - use format field
            format_type: str = node.get("format", "handlebars")
            ext: str = FORMAT_EXTENSIONS.get(format_type, ".txt")
            return f".template{ext}"
        else:
            # Unknown template type - use generic
            return ".template.txt"

    def explode_node(self, node: Dict[str, Any], node_dir: Path) -> List[str]:
        """Extract template content to appropriate file

        Returns:
            List of created filenames
        """
        try:
            node_id: str = node.get("id")
            content_field: str = self._get_content_field(node.get("type", ""))
            template_content: str = node.get(content_field, "")
            created_files: List[str] = []

            if template_content:
                extension: str = self._get_template_extension(node)
                template_file: Path = node_dir / f"{node_id}{extension}"
                template_file.write_text(template_content)
                created_files.append(f"{node_id}{extension}")

            return created_files

        except Exception as e:
            print(
                f"⚠ Warning: template plugin failed for {node.get('id', 'unknown')}: {e}"
            )
            return []

    def rebuild_node(
        self, node_id: str, node_dir: Path, skeleton: Dict[str, Any]
    ) -> Dict[str, str]:
        """Rebuild template content from file"""
        data: Dict[str, str] = {}
        node_type: str = skeleton.get("type", "") if skeleton else ""

        # Try to find template file by checking known patterns
        template_file: Optional[Path] = None
        content_field: str = "template"

        # Check for Dashboard 2.0 (Vue)
        vue_file: Path = node_dir / f"{node_id}.vue"
        if vue_file.exists():
            template_file = vue_file
            content_field = "format"

        # Check for Dashboard 1 (Angular)
        if not template_file:
            ui_template_file: Path = node_dir / f"{node_id}.ui-template.html"
            if ui_template_file.exists():
                template_file = ui_template_file
                content_field = "format"

        # Check for core template node (any .template.* file)
        if not template_file:
            template_files: List[Path] = list(node_dir.glob(f"{node_id}.template.*"))
            if template_files:
                template_file = template_files[0]
                content_field = "template"

        # Read template content if found
        if template_file and template_file.exists():
            data[content_field] = template_file.read_text()
        elif skeleton and node_type:
            # No file found - preserve field position with empty string
            expected_field: str = self._get_content_field(node_type)
            if expected_field in skeleton:
                data[expected_field] = ""

        return data
