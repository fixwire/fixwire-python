"""DSNs: {scheme}://{key}@{host}[:{port}][/{path}]. The key is the
project's publishable key; the base URL (the DSN without the key) is where
every /v1 endpoint lives, behind the path for a self-hosted server under a
prefix."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

SDK_NAME = "fixwire.python"


class BadDsn(ValueError):
    pass


@dataclass(frozen=True)
class Dsn:
    scheme: str
    key: str
    host: str
    port: int | None
    path: str

    @classmethod
    def parse(cls, value: str) -> Dsn:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https"):
            raise BadDsn("the DSN's scheme is %r, not http or https" % parts.scheme)
        if not parts.username:
            raise BadDsn("the DSN has no key")
        if not parts.hostname:
            raise BadDsn("the DSN has no host")
        try:
            port = parts.port
        except ValueError:
            raise BadDsn("the DSN has a bad port") from None
        return cls(parts.scheme, parts.username, parts.hostname, port, parts.path.rstrip("/"))

    @property
    def netloc(self) -> str:
        return self.host if self.port is None else "%s:%d" % (self.host, self.port)

    @property
    def base_url(self) -> str:
        """The DSN without the key."""
        return "%s://%s%s" % (self.scheme, self.netloc, self.path)

    def url(self, path: str) -> str:
        """An endpoint's URL, e.g. ``url("/v1/logs")``."""
        return self.base_url + path

    def auth_header(self) -> str:
        return "Bearer " + self.key

    def __str__(self) -> str:
        return "%s://%s@%s%s" % (self.scheme, self.key, self.netloc, self.path)
