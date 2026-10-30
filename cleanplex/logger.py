import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# Rotation caps the log at LOG_MAX_BYTES * (LOG_BACKUPS + 1) so a long-running service cannot fill the disk (#158).
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 4


def setup_logging(level: str = "INFO") -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file = os.environ.get("CLEANPLEX_LOG_FILE", "")
    if log_file:
        # Everything goes to the rotating file; stderr keeps only errors, for the supervisor's crash log.
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS)
        stderr = logging.StreamHandler(sys.stderr)
        stderr.setLevel(logging.ERROR)
        handlers = [handler, stderr]
    else:
        handlers = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        handlers=handlers,
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
