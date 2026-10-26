"""Final-emission redaction for application and Uvicorn logs.

Do not mutate request scope: routing continues to use the original capability.
Apply after exception/argument formatting, including query strings and encoded
paths. Custom deployment log handlers must use this formatter boundary too.
"""
import logging
import re
from urllib.parse import unquote

from uvicorn.logging import AccessFormatter, DefaultFormatter

# Redact the complete URI suffix, including query values, even for malformed
# tokens or unsupported methods. Repeated decoding covers encoded separators.
PATH = re.compile(r'/energy-data/shared-buildings', re.IGNORECASE)
SPAN = re.compile(r"[^\s'\"<>]+")
CAPABILITY = re.compile(r'(?<![A-Za-z0-9_-])(?:[A-Za-z0-9_-]{43}|[0-9a-fA-F]{64})(?![A-Za-z0-9_-])')
BEARER = re.compile(r'Bearer\s+[^\s\'"<>]+', re.IGNORECASE)


def redact(value):
    def redact_span(match):
        result = match.group()
        for _ in range(4):
            decoded = unquote(result)
            if decoded == result:
                break
            result = decoded
        sensitive = PATH.search(result)
        if sensitive:
            # Identify spans BEFORE decoding: encoded spaces/quotes must not
            # terminate redaction and expose the rest of a sensitive query.
            return result[:sensitive.start()] + '/energy-data/shared-buildings/[REDACTED]'
        return result

    result = SPAN.sub(redact_span, value)
    result = CAPABILITY.sub('[REDACTED]', result)
    return BEARER.sub('Bearer [REDACTED]', result)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


class RedactingDefaultFormatter(DefaultFormatter):
    def format(self, record):
        return redact(super().format(record))


class RedactingAccessFormatter(AccessFormatter):
    def format(self, record):
        return redact(super().format(record))


class RedactingWrapper(logging.Formatter):
    """Preserve an existing handler's layout while protecting final emission."""
    def __init__(self, original):
        super().__init__()
        self.original = original or logging.Formatter()

    def format(self, record):
        return redact(self.original.format(record))


def install_log_redaction():
    # Uvicorn configures handlers before importing the app. Protect those and
    # existing application handlers, including direct launches without a config.
    # Establish a root handler before ETL basicConfig can create a raw one.
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    loggers = [root, *(value for value in logging.Logger.manager.loggerDict.values()
                       if isinstance(value, logging.Logger))]
    for logger in loggers:
        for handler in logger.handlers:
            if not isinstance(handler.formatter, (RedactingWrapper, RedactingFormatter,
                    RedactingDefaultFormatter, RedactingAccessFormatter)):
                handler.setFormatter(RedactingWrapper(handler.formatter))
