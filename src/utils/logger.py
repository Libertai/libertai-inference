import logging
import os
import sys

from src.config import config


def setup_logger(name: str, level: int | None = None) -> logging.Logger:
    """
    Set up and configure a logger

    Args:
        name: Logger name (usually __name__ from the calling module)
        level: Logging level (default: from config.LOG_LEVEL)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Use provided level or get from config
    log_level = level if level is not None else config.LOG_LEVEL

    # Avoid adding handlers multiple times
    if not logger.handlers:
        # Always add stdout handler
        stream_handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        # Add file handler if LOG_FILE is specified
        if config.LOG_FILE:
            # Create directory if it doesn't exist
            log_dir = os.path.dirname(config.LOG_FILE)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)

            file_handler = logging.FileHandler(config.LOG_FILE)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

    logger.setLevel(log_level)
    logger.propagate = False

    return logger
