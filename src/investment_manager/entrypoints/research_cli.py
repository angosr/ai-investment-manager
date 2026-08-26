"""Offline research command-line entrypoint.

Importing this module registers only offline research and historical diagnostics;
managed production processes never import it.
"""

from investment_manager.entrypoints.cli import legacy_research_commands as _legacy  # noqa: F401
from investment_manager.entrypoints.cli import research_commands as _research  # noqa: F401
from investment_manager.entrypoints.cli.research_root import app

__all__ = ["app"]
