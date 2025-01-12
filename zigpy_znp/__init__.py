import logging

from .utils import TraceLogger

if not hasattr(logging.getLoggerClass(), "trace"):
    logging.setLoggerClass(TraceLogger)


MAJOR_VERSION = 0
MINOR_VERSION = 0
PATCH_VERSION = "1.dev1"

__short_version__ = f"{MAJOR_VERSION}.{MINOR_VERSION}"
__version__ = f"{__short_version__}.{PATCH_VERSION}"
