"""EMUNEL Core — Proxy server orchestrator."""

import asyncio
import logging
import ssl
from typing import Optional

from .config import CoreConfig
from .state import ConnectionState
from .links import LinkManager
from .quota import QuotaManager
from .relay import vless, trojan, shadowsocks
from .relay.plugin import PluginRegistry

logger = logging.getLogger("emunel.core.app")


class ProxyServer:
    """Main proxy server handling multi-protocol connections."""

    def __init__(self, config: CoreConfig) -> None:
        self.config = config
        self.state = ConnectionState()
        self.links = LinkManager(config)
        self.quota = QuotaManager(config)
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        """Start the proxy server."""
        ssl_ctx = self._create_ssl_context()

        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self.config.listen_host,
            port=self.config.listen_port,
            ssl=ssl_ctx,
        )

        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        logger.info("Proxy server listening on %s", addrs)

    async def stop(self) -> None:
        """Gracefully stop the proxy server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Proxy server stopped.")

        await self.state.close_all()

    def _create_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Create TLS context if certificates are configured."""
        if not self.config.tls_cert or not self.config.tls_key:
            logger.warning("No TLS certificate configured; running without TLS.")
            return None

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.config.tls_cert, self.config.tls_key)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Route incoming connection to the appropriate protocol handler."""
        peer = writer.get_extra_info("peername")
        conn_id = self.state.register(peer)
        logger.debug("New connection %s from %s", conn_id, peer)

        try:
            # Peek at the first bytes to determine protocol
            header = await asyncio.wait_for(reader.read(2), timeout=10.0)
            if not header:
                return

            # Detect protocol from header bytes
            protocol = self._detect_protocol(header)

            if protocol == "vless":
                await vless.handle(reader, writer, header, self.config, self.quota)
            elif protocol == "trojan":
                await trojan.handle(reader, writer, header, self.config, self.quota)
            elif protocol == "shadowsocks":
                await shadowsocks.handle(
                    reader, writer, header, self.config, self.quota
                )
            else:
                # Try plugin registry
                plugin = PluginRegistry.detect(header)
                if plugin:
                    await plugin.handle(reader, writer, None)
                else:
                    logger.warning("Unknown protocol from %s", peer)

        except asyncio.TimeoutError:
            logger.debug("Connection %s timed out during handshake", conn_id)
        except ConnectionResetError:
            logger.debug("Connection %s reset by peer", conn_id)
        except Exception as exc:
            logger.error("Error handling connection %s: %s", conn_id, exc)
        finally:
            self.state.unregister(conn_id)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    def _detect_protocol(header: bytes) -> str:
        """Detect protocol type from initial bytes."""
        if len(header) < 1:
            return "unknown"

        first_byte = header[0]

        # VLESS protocol version byte is 0x00
        if first_byte == 0x00:
            return "vless"

        # Trojan starts with a hex-encoded SHA224 hash (ASCII printable)
        if 0x30 <= first_byte <= 0x66:  # '0'-'f' range
            return "trojan"

        # Shadowsocks AEAD starts with payload length (typically high bytes)
        return "shadowsocks"
