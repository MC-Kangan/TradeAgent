"""Immutable provider lookup for the application composition root."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


class ProviderRegistry:
    """Names immutable provider capabilities without exposing mutation methods."""

    def __init__(self, providers: Mapping[str, object]) -> None:
        self._providers = MappingProxyType(dict(providers))

    @property
    def providers(self) -> Mapping[str, object]:
        return self._providers

    def require(self, name: str) -> object:
        try:
            return self._providers[name]
        except KeyError as error:
            raise KeyError(name) from error
