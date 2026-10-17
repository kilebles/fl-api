from pydantic import BaseModel


class ProxyInfo(BaseModel):
    """A proxy from proxies.txt (host:port:user:password)."""

    host: str
    port: int
    user: str
    password: str
    type: str = "http"

    @classmethod
    def from_line(cls, line: str) -> "ProxyInfo":
        """Parse a proxies.txt line in format: host:port:user:password.

        Supports passwords and usernames containing colons (e.g. Thordata rotating proxies).
        Format: host:port:<user_with_possible_colons>:<password_with_possible_colons>
        Splits on first two colons (host, port) and last colon (password).
        """
        parts = line.strip().split(":")
        if len(parts) < 4:
            raise ValueError(f"Invalid proxy line: {line!r}. Expected host:port:user:password")
        host = parts[0]
        port = int(parts[1])
        password = parts[-1]
        user = ":".join(parts[2:-1])
        return cls(host=host, port=port, user=user, password=password)

    @property
    def key(self) -> str:
        """`host:port`, the identity used in dead_proxies.txt."""
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.user}:{self.password}@{self.host}:{self.port}"

    def to_line(self) -> str:
        return f"{self.host}:{self.port}:{self.user}:{self.password}"
