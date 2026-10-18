"""Minimal stdlib-only JSON log formatter for stdout/journald consumption."""

import datetime
import json
import logging


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
        return json.dumps(payload)


class ExtraLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter that merges structured ``extra`` fields into the record."""

    def process(self, msg, kwargs):
        extra = kwargs.pop("extra", {}) or {}
        kwargs["extra"] = {"extra_fields": extra}
        return msg, kwargs
