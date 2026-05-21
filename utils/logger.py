import logging
import logging.handlers
import os
import yaml
from pathlib import Path


def load_config():
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_logger(name):
    config = load_config()
    logging_config = config.get("logging", {})

    log_level = logging_config.get("level", "INFO")
    log_to_file = logging_config.get("log_to_file", False)
    log_file = logging_config.get("log_file", "tradebot.log")

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level))

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_to_file:
        log_dir = Path(__file__).parent.parent
        log_path = log_dir / log_file

        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
