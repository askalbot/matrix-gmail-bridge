import structlog
from typing import cast, TYPE_CHECKING

Logger = lambda service: cast(structlog.BoundLogger, structlog.get_logger(service=service))