#!/usr/bin/env python3
"""EMUNEL — Single-service entry point.

Runs the FastAPI backend with optional core proxy and worker
agent in a unified process for simple deployments.
"""

import asyncio
import os
import sys
import signal
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("emunel")


def main() -> None:
    """Launch EMUNEL services."""
    import uvicorn
    from api.emunel_api.main import app  # noqa: F401

    host = os.getenv("EMUNEL_HOST", "0.0.0.0")
    port = int(os.getenv("EMUNEL_PORT", "8000"))
    debug = os.getenv("EMUNEL_DEBUG", "false").lower() == "true"

    logger.info("Starting EMUNEL on %s:%d (debug=%s)", host, port, debug)

    uvicorn.run(
        "api.emunel_api.main:app",
        host=host,
        port=port,
        reload=debug,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
