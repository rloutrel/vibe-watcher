"""Minimal stdlib-only JSON log formatter for stdout/journald consumption."""

import datetime
import json
import logging

RESERVED = set(
    logging.makeLogRecord({}).__dict__
) | {"message", "asctime", "taskName"}


class JSONFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload.setdefault(key, value)
        # Structured fields passed via logger.info(..., extra={...}) land as
        # plain record attributes; serialize everything the logger added.
        for key, value in record.__dict__.items():
            if key in RESERVED or key in payload:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload)


class ExtraLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter that merges structured ``extra`` fields into the record."""

    def process(self, msg, kwargs):
        extra = kwargs.pop("extra", {}) or {}
        kwargs["extra"] = {"extra_fields": extra}
        return msg, kwargs
