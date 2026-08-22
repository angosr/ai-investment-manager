"""Investment Manager command-line entrypoint."""

from investment_manager.entrypoints.cli import (
    assessment_commands as _assessment_commands,  # noqa: F401
)
from investment_manager.entrypoints.cli import capital_commands as _capital_commands  # noqa: F401
from investment_manager.entrypoints.cli import commands as _commands  # noqa: F401
from investment_manager.entrypoints.cli import (  # noqa: F401
    fact_store_commands as _fact_store_commands,
)
from investment_manager.entrypoints.cli import research_commands as _research_commands  # noqa: F401
from investment_manager.entrypoints.cli import service_commands as _service_commands  # noqa: F401
from investment_manager.entrypoints.cli.root import app

__all__ = ["app"]
