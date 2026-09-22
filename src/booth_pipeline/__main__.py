"""Entrypoint: ``python -m booth_pipeline`` (or the ``booth-pipeline`` script)."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import create_app
from .config import Config, ConfigError


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"booth-pipeline: configuration error: {e}", file=sys.stderr)
        return 2
    app = create_app(cfg)
    # One process, several threads: runs execute in-process worker threads, so this must not be
    # started with multiple uvicorn workers (each would run its own scheduler and worker pool;
    # scale by replicas instead, which the store's atomic claiming is built for).
    uvicorn.run(app, host="0.0.0.0", port=cfg.port, log_level="info")  # noqa: S104 - a pod listens on all interfaces
    return 0


if __name__ == "__main__":
    sys.exit(main())
