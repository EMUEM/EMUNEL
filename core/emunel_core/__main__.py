"""EMUNEL Core entry point.

Usage:
    python -m emunel_core
"""

import asyncio
import logging
import signal
import sys

from .app import ProxyServer
from .config import CoreConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("emunel.core")


async def run() -> None:
    config = CoreConfig.from_env()
    server = ProxyServer(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info(
        "EMUNEL Core starting on %s:%d", config.listen_host, config.listen_port
    )

    try:
        await server.start()
        await stop_event.wait()
    finally:
        await server.stop()
        logger.info("EMUNEL Core stopped.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
