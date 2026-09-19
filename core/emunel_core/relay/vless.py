"""EMUNEL Core — VLESS protocol handler."""

import asyncio
import hashlib
import logging
import struct
import uuid as uuid_lib
from typing import Optional, Tuple

from ..config import CoreConfig
from ..quota import QuotaManager

logger = logging.getLogger("emunel.core.relay.vless")

# VLESS protocol constants
VLESS_VERSION = 0x00
CMD_TCP = 0x01
CMD_UDP = 0x02

ADDR_IPV4 = 0x01
ADDR_DOMAIN = 0x02
ADDR_IPV6 = 0x03


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    initial_data: bytes,
    config: CoreConfig,
    quota: QuotaManager,
) -> None:
    """Handle a VLESS connection."""
    try:
        # Read the full VLESS request header
        # initial_data contains the first 2 bytes (version + uuid length indicator)
        remaining_header = await asyncio.wait_for(
            reader.read(config.buffer_size), timeout=config.connection_timeout
        )
        data = initial_data + remaining_header

        if len(data) < 24:
            logger.warning("VLESS header too short")
            return

        # Parse VLESS header
        version = data[0]
        if version != VLESS_VERSION:
            logger.warning("Unsupported VLESS version: %d", version)
            return

        # Extract UUID (16 bytes at offset 1)
        user_uuid = str(uuid_lib.UUID(bytes=data[1:17]))

        # Addon data length
        addon_len = data[17]
        offset = 18 + addon_len

        if offset >= len(data):
            logger.warning("VLESS header truncated after addon")
            return

        # Command
        command = data[offset]
        offset += 1

        # Destination port (big-endian uint16)
        if offset + 2 > len(data):
            return
        dest_port = struct.unpack("!H", data[offset : offset + 2])[0]
        offset += 2

        # Address type and address
        dest_addr, offset = _parse_address(data, offset)
        if dest_addr is None:
            logger.warning("Failed to parse VLESS destination address")
            return

        # Remaining data after header is payload
        payload = data[offset:]

        logger.info(
            "VLESS connection: uuid=%s dest=%s:%d cmd=%d",
            user_uuid[:8],
            dest_addr,
            dest_port,
            command,
        )

        # Check quota
        quota_result = await quota.check(user_uuid)
        if not quota_result.allowed:
            logger.info("Quota exceeded for user %s", user_uuid[:8])
            return

        # Connect to destination
        if command == CMD_TCP:
            await _relay_tcp(
                reader, writer, dest_addr, dest_port, payload, config, quota, user_uuid
            )
        elif command == CMD_UDP:
            await _relay_udp(
                reader, writer, dest_addr, dest_port, payload, config, quota, user_uuid
            )
        else:
            logger.warning("Unknown VLESS command: %d", command)

    except Exception as exc:
        logger.error("VLESS handler error: %s", exc)


def _parse_address(data: bytes, offset: int) -> Tuple[Optional[str], int]:
    """Parse VLESS address field."""
    if offset >= len(data):
        return None, offset

    addr_type = data[offset]
    offset += 1

    if addr_type == ADDR_IPV4:
        if offset + 4 > len(data):
            return None, offset
        addr = ".".join(str(b) for b in data[offset : offset + 4])
        return addr, offset + 4

    elif addr_type == ADDR_DOMAIN:
        if offset >= len(data):
            return None, offset
        domain_len = data[offset]
        offset += 1
        if offset + domain_len > len(data):
            return None, offset
        addr = data[offset : offset + domain_len].decode("ascii", errors="replace")
        return addr, offset + domain_len

    elif addr_type == ADDR_IPV6:
        if offset + 16 > len(data):
            return None, offset
        parts = struct.unpack("!8H", data[offset : offset + 16])
        addr = ":".join(f"{p:x}" for p in parts)
        return addr, offset + 16

    return None, offset


async def _relay_tcp(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    dest_addr: str,
    dest_port: int,
    initial_payload: bytes,
    config: CoreConfig,
    quota: QuotaManager,
    user_id: str,
) -> None:
    """Relay TCP traffic between client and destination."""
    try:
        remote_reader, remote_writer = await asyncio.wait_for(
            asyncio.open_connection(dest_addr, dest_port),
            timeout=config.connection_timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        logger.debug("Cannot connect to %s:%d — %s", dest_addr, dest_port, exc)
        return

    try:
        # Send VLESS response header (version + addon_len=0)
        client_writer.write(bytes([VLESS_VERSION, 0]))
        await client_writer.drain()

        # Send initial payload to remote
        if initial_payload:
            remote_writer.write(initial_payload)
            await remote_writer.drain()
            await quota.consume(user_id, len(initial_payload))

        # Bidirectional relay
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


async def _relay_udp(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    dest_addr: str,
    dest_port: int,
    initial_payload: bytes,
    config: CoreConfig,
    quota: QuotaManager,
    user_id: str,
) -> None:
    """Handle UDP over VLESS (simplified)."""
    # UDP handling uses the same TCP relay for tunneled data
    await _relay_tcp(
        client_reader,
        client_writer,
        dest_addr,
        dest_port,
        initial_payload,
        config,
        quota,
        user_id,
    )


async def _pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    buffer_size: int,
    quota: QuotaManager,
    user_id: str,
) -> None:
    """Pipe data from reader to writer."""
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
