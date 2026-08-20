"""Investment Manager command-line entrypoint."""

from investment_manager.entrypoints.cli import commands as _commands  # noqa: F401
from investment_manager.entrypoints.cli.root import app

__all__ = ["app"]
