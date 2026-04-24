import logging
import structlog

# Write logs to file + console
file_handler = logging.FileHandler("logs/crm.log")
stream_handler = logging.StreamHandler()

logging.basicConfig(
    format="%(message)s",
    level=logging.INFO,
    handlers=[file_handler, stream_handler],
)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
)


def get_logger(name: str):
    return structlog.get_logger(name)
