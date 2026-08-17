"""Explicit, TLS-verifying network transport configuration for public market data."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx


class NetworkMode(StrEnum):
    DIRECT = "direct"
    ENV = "env"
    PROXY = "proxy"


@dataclass(frozen=True, slots=True)
class NetworkConfiguration:
    """One shared source of proxy behavior for public REST and WebSocket clients."""

    mode: NetworkMode = NetworkMode.ENV
    proxy_url: str | None = None

    def __post_init__(self) -> None:
        if self.mode is NetworkMode.PROXY:
            if not self.proxy_url:
                raise ValueError("OKX_PROXY_URL is required when OKX_NETWORK_MODE=proxy")
            parsed = urlsplit(self.proxy_url)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("OKX_PROXY_URL has an invalid port") from exc
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("OKX_PROXY_URL must be an http or https proxy URL")
            if port is not None and not 1 <= port <= 65535:
                raise ValueError("OKX_PROXY_URL has an invalid port")
        elif self.proxy_url is not None:
            raise ValueError("OKX_PROXY_URL is only valid when OKX_NETWORK_MODE=proxy")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> NetworkConfiguration:
        values = os.environ if environment is None else environment
        try:
            mode = NetworkMode(values.get("OKX_NETWORK_MODE", NetworkMode.ENV).lower())
        except ValueError as exc:
            raise ValueError("OKX_NETWORK_MODE must be direct, env, or proxy") from exc
        proxy_url = values.get("OKX_PROXY_URL")
        return cls(mode=mode, proxy_url=proxy_url)

    @property
    def redacted_proxy_url(self) -> str | None:
        if self.proxy_url is None:
            return None
        parsed = urlsplit(self.proxy_url)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        if parsed.username is not None or parsed.password is not None:
            host = f"***@{host}"
        return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))

    def create_http_client(self, *, timeout: httpx.Timeout) -> httpx.Client:
        """Create a client without weakening TLS verification."""
        if self.mode is NetworkMode.DIRECT:
            return httpx.Client(timeout=timeout, trust_env=False)
        if self.mode is NetworkMode.ENV:
            return httpx.Client(timeout=timeout, trust_env=True)
        return httpx.Client(timeout=timeout, trust_env=False, proxy=self.proxy_url)

    @property
    def websocket_proxy(self) -> str | Literal[True] | None:
        if self.mode is NetworkMode.DIRECT:
            return None
        if self.mode is NetworkMode.ENV:
            return True
        return self.proxy_url

    def probe_proxy_listener(self, *, timeout_seconds: float = 2.0) -> bool:
        """Check only the configured proxy listener; it doesn't contact a remote host."""
        if self.mode is not NetworkMode.PROXY or self.proxy_url is None:
            return False
        parsed = urlsplit(self.proxy_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            with socket.create_connection((parsed.hostname or "", port), timeout_seconds):
                return True
        except OSError:
            return False
