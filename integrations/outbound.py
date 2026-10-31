"""Outbound HTTPS to business-supplied URLs.

Business administrators choose these URLs, so every request is treated as a
possible server-side request forgery: only HTTPS on port 443, only public
addresses (never loopback, private, link-local, or the cloud metadata
server), the connection pinned to the address that was checked, no redirects,
and bounded time and size.
"""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit
from django.conf import settings

TIMEOUT = 10
MAX_BYTES = 2_000_000


class OutboundError(Exception):
    pass


def local_targets_allowed():
    # Development only, so a sample website on localhost can be tested.
    return settings.DEBUG and getattr(settings, "MANDALI_ALLOW_LOCAL_INTEGRATIONS", False)


def check_url(url):
    parts = urlsplit(url)
    local = local_targets_allowed()
    if parts.scheme != "https" and not (local and parts.scheme == "http"):
        raise OutboundError("Use an https:// address.")
    if not parts.hostname or parts.username or parts.password:
        raise OutboundError("Use a plain https:// address without a username or password.")
    default_port = 443 if parts.scheme == "https" else 80
    if (parts.port or default_port) != 443 and not local:
        raise OutboundError("Only the standard HTTPS port (443) is supported.")
    return parts


def public_address(host, port):
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise OutboundError("The address could not be found.")
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global and not local_targets_allowed():
            raise OutboundError("That address points to a private or internal network.")
        addresses.append(str(address))
    if not addresses:
        raise OutboundError("The address could not be found.")
    return addresses[0]


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, **kwargs):
        super().__init__(host, **kwargs)
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, address, **kwargs):
        super().__init__(host, **kwargs)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


def request(method, url, headers=None, body=None):
    """Send one request and return (status, body bytes). Never follows redirects."""
    parts = check_url(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    address = public_address(parts.hostname, port)
    if parts.scheme == "https":
        connection = PinnedHTTPSConnection(
            parts.hostname, address, port=port, timeout=TIMEOUT,
            context=ssl.create_default_context(),
        )
    else:
        connection = PinnedHTTPConnection(parts.hostname, address, port=port, timeout=TIMEOUT)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    headers = {"User-Agent": "Mandali-Integrations/1", **(headers or {})}
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read(MAX_BYTES + 1)
    except (OSError, http.client.HTTPException, ssl.SSLError) as error:
        raise OutboundError(f"Connection failed: {type(error).__name__}.")
    finally:
        connection.close()
    if len(data) > MAX_BYTES:
        raise OutboundError("The response was larger than 2 MB.")
    return response.status, data
