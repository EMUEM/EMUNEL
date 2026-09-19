"""EMUNEL Core — Modular protocol plugin system."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import ClassVar, Dict, Optional

logger = logging.getLogger("emunel.core.relay.plugin")


class ProtocolPlugin(ABC):
    """Base class for protocol plugins.

    Implement this class to add support for new protocols.
    Register plugins using PluginRegistry.register().
    """

    name: str = ""
    version: str = "0.0.0"
    description: str = ""

    @abstractmethod
    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        link: Optional[dict],
    ) -> None:
        """Handle an incoming connection for this protocol.

        Args:
            reader: Client stream reader.
            writer: Client stream writer.
            link: Optional link/subscription configuration.
        """
        ...

    @abstractmethod
    def create_link(self, config: dict) -> str:
        """Generate a subscription link for this protocol.

        Args:
            config: Protocol-specific configuration.

        Returns:
            URI string for the subscription link.
        """
        ...

    def detect(self, header: bytes) -> bool:
        """Check if the initial bytes match this protocol.

        Override this method to enable automatic protocol detection.

        Args:
            header: First bytes received from the client.

        Returns:
            True if these bytes belong to this protocol.
        """
        return False


class PluginRegistry:
    """Registry for protocol plugins.

    Usage:
        # Register a plugin
        PluginRegistry.register(MyPlugin())

        # Get a plugin by name
        plugin = PluginRegistry.get("my-protocol")

        # Detect protocol from header bytes
        plugin = PluginRegistry.detect(header_bytes)

        # List all registered plugins
        plugins = PluginRegistry.list_plugins()
    """

    _plugins: ClassVar[Dict[str, ProtocolPlugin]] = {}

    @classmethod
    def register(cls, plugin: ProtocolPlugin) -> None:
        """Register a protocol plugin."""
        if not plugin.name:
            raise ValueError("Plugin must have a non-empty name.")

        if plugin.name in cls._plugins:
            logger.warning(
                "Overwriting existing plugin: %s (v%s) -> v%s",
                plugin.name,
                cls._plugins[plugin.name].version,
                plugin.version,
            )

        cls._plugins[plugin.name] = plugin
        logger.info(
            "Registered protocol plugin: %s v%s", plugin.name, plugin.version
        )

    @classmethod
    def unregister(cls, name: str) -> Optional[ProtocolPlugin]:
        """Remove a plugin from the registry."""
        plugin = cls._plugins.pop(name, None)
        if plugin:
            logger.info("Unregistered protocol plugin: %s", name)
        return plugin

    @classmethod
    def get(cls, name: str) -> Optional[ProtocolPlugin]:
        """Retrieve a plugin by name."""
        return cls._plugins.get(name)

    @classmethod
    def detect(cls, header: bytes) -> Optional[ProtocolPlugin]:
        """Detect protocol from header bytes."""
        for plugin in cls._plugins.values():
            try:
                if plugin.detect(header):
                    return plugin
            except Exception as exc:
                logger.error(
                    "Plugin %s detection error: %s", plugin.name, exc
                )
        return None

    @classmethod
    def list_plugins(cls) -> list[dict]:
        """List all registered plugins."""
        return [
            {
                "name": p.name,
                "version": p.version,
                "description": p.description,
            }
            for p in cls._plugins.values()
        ]

    @classmethod
    def clear(cls) -> None:
        """Remove all registered plugins."""
        cls._plugins.clear()
