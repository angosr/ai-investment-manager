"""Investment Manager command-line entrypoint."""

from investment_manager.entrypoints.cli import commands as _commands  # noqa: F401
from investment_manager.entrypoints.cli import service_commands as _service_commands  # noqa: F401
from investment_manager.entrypoints.cli.root import app
from investment_manager.research import cli as _research_cli  # noqa: F401

__all__ = ["app"]
