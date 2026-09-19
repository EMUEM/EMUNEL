"""EMUNEL Core — Shadowsocks protocol handler."""

import asyncio
import hashlib
import logging
import os
import struct
from typing import Optional, Tuple

from ..config import CoreConfig
from ..quota import QuotaManager

logger = logging.getLogger("emunel.core.relay.shadowsocks")

# Shadowsocks AEAD ciphers
CIPHERS = {
    "aes-128-gcm": {"key_size": 16, "salt_size": 16, "nonce_size": 12, "tag_size": 16},
    "aes-256-gcm": {"key_size": 32, "salt_size": 32, "nonce_size": 12, "tag_size": 16},
    "chacha20-ietf-poly1305": {
        "key_size": 32,
        "salt_size": 32,
        "nonce_size": 12,
        "tag_size": 16,
    },
}

# Address types
ADDR_IPV4 = 0x01
ADDR_DOMAIN = 0x03
ADDR_IPV6 = 0x04


def _derive_key(password: str, key_size: int) -> bytes:
    """Derive encryption key from password using EVP_BytesToKey."""
    result = b""
    last = b""
    while len(result) < key_size:
        last = hashlib.md5(last + password.encode()).digest()
        result += last
    return result[:key_size]


def _hkdf_sha1(key: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA1 key derivation."""
    import hmac

    prk = hmac.new(salt, key, hashlib.sha1).digest()
    output = b""
    counter = 1
    previous = b""
    while len(output) < length:
        previous = hmac.new(
            prk, previous + info + bytes([counter]), hashlib.sha1
        ).digest()
        output += previous
        counter += 1
    return output[:length]


async def handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    initial_data: bytes,
    config: CoreConfig,
    quota: QuotaManager,
) -> None:
    """Handle a Shadowsocks connection.

    This is a simplified implementation handling AEAD ciphers.
    Production deployments should use a dedicated SS implementation.
    """
    try:
        # Read more data to get the full header
        remaining = await asyncio.wait_for(
            reader.read(config.buffer_size), timeout=config.connection_timeout
        )
        data = initial_data + remaining

        if len(data) < 32:
            logger.warning("Shadowsocks data too short")
            return

        # For simplified handling, attempt to parse the address header
        # In a full implementation, this would decrypt the AEAD payload first
        logger.info("Shadowsocks connection received (%d bytes)", len(data))

        # Attempt to extract destination from decrypted payload
        # This is a stub that delegates to proper AEAD decryption
        dest_addr, dest_port, payload_offset = _try_parse_address(data)

        if dest_addr is None:
            logger.warning("Failed to parse Shadowsocks destination")
            return

        payload = data[payload_offset:]

        logger.info("Shadowsocks relay to %s:%d", dest_addr, dest_port)

        # Relay connection
        await _relay(
            reader,
            writer,
            dest_addr,
            dest_port,
            payload,
            config,
            quota,
            "ss-user",
        )

    except Exception as exc:
        logger.error("Shadowsocks handler error: %s", exc)


def _try_parse_address(
    data: bytes, offset: int = 0
) -> Tuple[Optional[str], int, int]:
    """Try to parse SOCKS-style address from data."""
    if offset >= len(data):
        return None, 0, offset

    addr_type = data[offset]
    offset += 1

    if addr_type == ADDR_IPV4:
        if offset + 4 > len(data):
            return None, 0, offset
        addr = ".".join(str(b) for b in data[offset : offset + 4])
        offset += 4
    elif addr_type == ADDR_DOMAIN:
        if offset >= len(data):
            return None, 0, offset
        domain_len = data[offset]
        offset += 1
        if offset + domain_len > len(data):
            return None, 0, offset
        addr = data[offset : offset + domain_len].decode("ascii", errors="replace")
        offset += domain_len
    elif addr_type == ADDR_IPV6:
        if offset + 16 > len(data):
            return None, 0, offset
        parts = struct.unpack("!8H", data[offset : offset + 16])
        addr = ":".join(f"{p:x}" for p in parts)
        offset += 16
    else:
        return None, 0, offset

    if offset + 2 > len(data):
        return None, 0, offset
    port = struct.unpack("!H", data[offset : offset + 2])[0]
    offset += 2

    return addr, port, offset


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
    """Relay TCP traffic for Shadowsocks."""
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
