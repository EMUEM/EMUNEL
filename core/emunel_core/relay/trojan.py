"""EMUNEL Core — Trojan protocol handler."""

import asyncio
import hashlib
import logging
import struct
from typing import Optional, Tuple

from ..config import CoreConfig
from ..quota import QuotaManager

logger = logging.getLogger("emunel.core.relay.trojan")

# Trojan constants
CRLF = b"\r\n"
CMD_CONNECT = 0x01
CMD_UDP_ASSOCIATE = 0x03

ADDR_IPV4 = 0x01
ADDR_DOMAIN = 0x03
ADDR_IPV6 = 0x04


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    initial_data: bytes,
    config: CoreConfig,
    quota: QuotaManager,
) -> None:
    """Handle a Trojan connection."""
    try:
        # Read the rest of the Trojan request
        remaining = await asyncio.wait_for(
            reader.read(config.buffer_size), timeout=config.connection_timeout
        )
        data = initial_data + remaining

        # Trojan header: SHA224(password) + CRLF + CMD + ATYP + DST.ADDR + DST.PORT + CRLF
        crlf_pos = data.find(CRLF)
        if crlf_pos == -1 or crlf_pos != 56:
            logger.warning("Invalid Trojan header: missing or misplaced CRLF")
            return

        # Extract password hash (56 hex chars = SHA224)
        password_hash = data[:56].decode("ascii", errors="replace")

        offset = 58  # After first CRLF

        if offset >= len(data):
            logger.warning("Trojan header truncated")
            return

        # Command
        command = data[offset]
        offset += 1

        # Address type
        if offset >= len(data):
            return
        addr_type = data[offset]
        offset += 1

        # Parse destination address
        dest_addr: Optional[str] = None

        if addr_type == ADDR_IPV4:
            if offset + 4 > len(data):
                return
            dest_addr = ".".join(str(b) for b in data[offset : offset + 4])
            offset += 4

        elif addr_type == ADDR_DOMAIN:
            if offset >= len(data):
                return
            domain_len = data[offset]
            offset += 1
            if offset + domain_len > len(data):
                return
            dest_addr = data[offset : offset + domain_len].decode(
                "ascii", errors="replace"
            )
            offset += domain_len

        elif addr_type == ADDR_IPV6:
            if offset + 16 > len(data):
                return
            parts = struct.unpack("!8H", data[offset : offset + 16])
            dest_addr = ":".join(f"{p:x}" for p in parts)
            offset += 16
        else:
            logger.warning("Unknown Trojan address type: %d", addr_type)
            return

        # Destination port
        if offset + 2 > len(data):
            return
        dest_port = struct.unpack("!H", data[offset : offset + 2])[0]
        offset += 2

        # Skip trailing CRLF
        if offset + 2 <= len(data) and data[offset : offset + 2] == CRLF:
            offset += 2

        # Remaining data is payload
        payload = data[offset:]

        logger.info(
            "Trojan connection: hash=%s... dest=%s:%d cmd=%d",
            password_hash[:8],
            dest_addr,
            dest_port,
            command,
        )

        # Quota check using password hash as user identifier
        quota_result = await quota.check(password_hash)
        if not quota_result.allowed:
            logger.info("Quota exceeded for Trojan user %s...", password_hash[:8])
            return

        # Connect and relay
        if command == CMD_CONNECT:
            await _relay(
                reader,
                writer,
                dest_addr,
                dest_port,
                payload,
                config,
                quota,
                password_hash,
            )
        else:
            logger.warning("Unsupported Trojan command: %d", command)

    except Exception as exc:
        logger.error("Trojan handler error: %s", exc)


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    dest_addr: str,
    dest_port: int,
    initial_payload: bytes,
    config: CoreConfig,
    quota: QuotaManager,
    user_id: str,
) -> None:
    """Relay TCP traffic for Trojan."""
    try:
        remote_reader, remote_writer = await asyncio.wait_for(
            asyncio.open_connection(dest_addr, dest_port),
            timeout=config.connection_timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        logger.debug("Cannot connect to %s:%d — %s", dest_addr, dest_port, exc)
        return

    try:
        if initial_payload:
            remote_writer.write(initial_payload)
            await remote_writer.drain()
            await quota.consume(user_id, len(initial_payload))

        await asyncio.gather(
            _pipe(client_reader, remote_writer, config.buffer_size, quota, user_id),
            _pipe(remote_reader, client_writer, config.buffer_size, quota, user_id),
        )
    finally:
        remote_writer.close()
        try:
            await remote_writer.wait_closed()
        except Exception:
            pass


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    buffer_size: int,
    quota: QuotaManager,
    user_id: str,
) -> None:
    """Pipe data between streams."""
    try:
        while True:
            data = await reader.read(buffer_size)
            if not data:
                break
            writer.write(data)
            await writer.drain()
            await quota.consume(user_id, len(data))
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
