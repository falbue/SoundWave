import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger("sound-wave")
log.propagate = False


def debug_enabled():
    return os.getenv("DEBUG", "").strip().casefold() == "true"


def configure_debug(directory=None):
    if not debug_enabled() or log.handlers:
        return
    directory = directory or Path(os.getenv("SOUND_WAVE_LOG_DIR", "logs"))
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            directory / "debug.log",
            maxBytes=256 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
    except OSError:
        print("Не удалось открыть файл диагностики «Волны»", file=sys.stderr)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.debug("SoundWave запущен с DEBUG=True")


def log_http(response, action):
    if not log.isEnabledFor(logging.DEBUG):
        return
    headers = response.headers
    content_type = headers.get("Content-Type", "").partition(";")[0]
    if content_type not in ("application/json", "text/html"):
        content_type = "other"
    log.debug(
        "SoundCloud: %s → HTTP %d [CloudFront=%s, EdgeCacheError=%s, DataDome=%s, type=%s]",
        action,
        response.status_code,
        "X-Amz-Cf-Id" in headers,
        headers.get("X-Cache") == "Error from cloudfront",
        "X-DataDome" in headers,
        content_type,
    )
