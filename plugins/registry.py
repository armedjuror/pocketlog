"""
plugins/registry.py — Global plugin instance registry.

Each plugin registers itself on instantiation so auth_flow can reach
any other plugin's send_message without circular imports.

Usage:
    # In a plugin's __init__:
    from plugins.registry import register
    register(self)

    # In auth_flow:
    from plugins.registry import get as get_plugin
    plugin = get_plugin("telegram")
    if plugin:
        await plugin.send_message(chat_id, text)
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from plugins.base import BasePlugin

_registry: dict[str, "BasePlugin"] = {}


def register(plugin: "BasePlugin") -> None:
    _registry[plugin.name] = plugin


def get(name: str) -> Optional["BasePlugin"]:
    return _registry.get(name)


def all_names() -> list[str]:
    return list(_registry.keys())
