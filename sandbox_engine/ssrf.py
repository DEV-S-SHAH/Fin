"""SSRF protection for outbound HTTP calls.

Provides URL validation against allowlists and blocks:
- localhost / loopback addresses
- private IP ranges (RFC 1918)
- link-local addresses (RFC 3927)
- cloud metadata endpoints (AWS, GCP, Azure, etc.)
- arbitrary user-controlled URLs
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("graphrag.ssrf")


@dataclass(frozen=True)
class SSRFConfig:
    """Configuration for SSRF protection."""

    # Allowed hosts for outbound requests
    allowed_hosts: frozenset[str]

    # Whether to allow localhost in development
    allow_localhost: bool = False

    # Whether to follow redirects (if True, validates each redirect)
    follow_redirects: bool = False

    # Maximum redirect hops
    max_redirects: int = 5


# Default configuration with production-safe defaults
DEFAULT_CONFIG = SSRFConfig(
    allowed_hosts=frozenset({
        # SEC EDGAR
        "www.sec.gov",
        "data.sec.gov",
        # Yahoo Finance
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        # OAuth providers
        "accounts.google.com",
        "appleid.apple.com",
        # NVIDIA NIM
        "integrate.api.nvidia.com",
    }),
    allow_localhost=False,
    follow_redirects=False,
    max_redirects=5,
)


# Cloud metadata endpoints that must never be reachable
CLOUD_METADATA_HOSTS = frozenset({
    # AWS
    "169.254.169.254",
    "instance-data.ec2.internal",
    # GCP
    "metadata.google.internal",
    "metadata",
    # Azure
    "169.254.169.254",  # Same as AWS
    # DigitalOcean
    "169.254.169.254",
    # Alibaba Cloud
    "100.100.100.200",
    # Oracle Cloud
    "169.254.169.254",
})


# Private IP ranges (RFC 1918) that must be blocked
PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    # Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    # Link-local (RFC 3927)
    ipaddress.ip_network("169.254.0.0/16"),
    # Carrier-grade NAT (RFC 6598)
    ipaddress.ip_network("100.64.0.0/10"),
    # Unique local addresses (RFC 4193) - IPv6
    ipaddress.ip_network("fc00::/7"),
    # Loopback IPv6
    ipaddress.ip_network("::1/128"),
    # Link-local IPv6
    ipaddress.ip_network("fe80::/10"),
]


def _resolve_host(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve hostname to IP addresses."""
    try:
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        return [ipaddress.ip_address(info[4][0]) for info in infos]
    except (socket.gaierror, ValueError) as exc:
        log.warning("dns_resolution_failed", extra={"host": hostname, "error": str(exc)})
        return []


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address is in a private/reserved range."""
    for network in PRIVATE_NETWORKS:
        if ip in network:
            return True
    return False


def _is_cloud_metadata(hostname: str) -> bool:
    """Check if hostname is a cloud metadata endpoint."""
    hostname_lower = hostname.lower()
    return hostname_lower in CLOUD_METADATA_HOSTS


def validate_url(url: str, config: SSRFConfig = DEFAULT_CONFIG) -> tuple[bool, Optional[str]]:
    """Validate a URL against SSRF protection rules.

    Returns:
        (is_valid, error_message) - error_message is None if valid
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:
        return False, f"invalid_url: {exc}"

    # Must use HTTP or HTTPS
    if parsed.scheme not in ("http", "https"):
        return False, f"invalid_scheme: {parsed.scheme}"

    hostname = parsed.hostname or ""
    if not hostname:
        return False, "missing_hostname"

    # Check against cloud metadata endpoints first (highest priority block)
    if _is_cloud_metadata(hostname):
        log.warning("ssrf_blocked_cloud_metadata", extra={"url": url, "host": hostname})
        return False, "cloud_metadata_endpoint_blocked"

    # Check if host is explicitly allowed
    if hostname not in config.allowed_hosts:
        # If not in allowlist, check if it's a private IP (which would be SSRF)
        ips = _resolve_host(hostname)
        for ip in ips:
            if _is_private_ip(ip):
                log.warning("ssrf_blocked_private_ip", extra={"url": url, "host": hostname, "ip": str(ip)})
                return False, f"private_ip_blocked: {ip}"

        # Not in allowlist and not a private IP - still block for safety
        log.warning("ssrf_blocked_not_allowed", extra={"url": url, "host": hostname})
        return False, f"host_not_allowed: {hostname}"

    # Host is in allowlist - but still verify it doesn't resolve to private IPs
    # (DNS rebinding protection). Skip for explicit localhost addresses since
    # they are expected to resolve to loopback/private IPs.
    hostname_lower = hostname.lower()
    is_localhost = hostname_lower in ("127.0.0.1", "localhost", "::1", "[::1]")
    if not is_localhost:
        ips = _resolve_host(hostname)
        for ip in ips:
            if _is_private_ip(ip):
                log.warning("ssrf_blocked_dns_rebinding", extra={"url": url, "host": hostname, "ip": str(ip)})
                return False, f"dns_rebinding_detected: {hostname} -> {ip}"

    return True, None


def validate_redirect_url(
    redirect_url: str,
    original_url: str,
    config: SSRFConfig = DEFAULT_CONFIG,
    redirect_count: int = 0,
) -> tuple[bool, Optional[str]]:
    """Validate a redirect URL during HTTP redirect following.

    Args:
        redirect_url: The URL we're being redirected to
        original_url: The original URL we requested
        config: SSRF configuration
        redirect_count: Number of redirects followed so far

    Returns:
        (is_valid, error_message)
    """
    if redirect_count >= config.max_redirects:
        return False, f"max_redirects_exceeded: {config.max_redirects}"

    if not config.follow_redirects:
        return False, "redirects_not_allowed"

    # Validate the redirect URL with the same rules
    return validate_url(redirect_url, config)


class SSRFProtectedOpener:
    """urllib opener with SSRF protection for redirects."""

    def __init__(self, config: SSRFConfig = DEFAULT_CONFIG, headers: dict | None = None):
        self.config = config
        self.headers = headers or {}
        self._redirect_count = 0

    def open(self, url: str, timeout: float = 30.0):
        """Open a URL with SSRF protection."""
        # Initial validation
        valid, error = validate_url(url, self.config)
        if not valid:
            raise URLError(f"SSRF validation failed: {error}")

        # Use custom opener that validates redirects
        import urllib.request

        class RedirectHandler(urllib.request.HTTPRedirectHandler):
            def __init__(self, outer):
                self.outer = outer

            def http_error_302(self, req, fp, code, msg, headers):
                return self._handle_redirect(req, fp, code, msg, headers)

            def http_error_301(self, req, fp, code, msg, headers):
                return self._handle_redirect(req, fp, code, msg, headers)

            def http_error_303(self, req, fp, code, msg, headers):
                return self._handle_redirect(req, fp, code, msg, headers)

            def http_error_307(self, req, fp, code, msg, headers):
                return self._handle_redirect(req, fp, code, msg, headers)

            def http_error_308(self, req, fp, code, msg, headers):
                return self._handle_redirect(req, fp, code, msg, headers)

            def _handle_redirect(self, req, fp, code, msg, headers):
                location = headers.get("Location")
                if not location:
                    return urllib.request.HTTPRedirectHandler.http_error_302(self, req, fp, code, msg, headers)

                self.outer._redirect_count += 1
                valid, error = validate_redirect_url(
                    location, req.full_url, self.outer.config, self.outer._redirect_count
                )
                if not valid:
                    raise URLError(f"SSRF redirect validation failed: {error}")

                return urllib.request.HTTPRedirectHandler.http_error_302(self, req, fp, code, msg, headers)

        class HeaderHandler(urllib.request.BaseHandler):
            def __init__(self, outer):
                self.outer = outer

            def http_request(self, req):
                for key, value in self.outer.headers.items():
                    req.add_header(key, value)
                return req

            https_request = http_request

        opener = urllib.request.build_opener(RedirectHandler(self), HeaderHandler(self))
        return opener.open(url, timeout=timeout)


# Convenience function for simple validation
def safe_urlopen(url: str, timeout: float = 30.0, config: SSRFConfig = DEFAULT_CONFIG, headers: dict | None = None):
    """Safe urlopen with SSRF protection.

    Usage:
        with safe_urlopen(url, headers={"User-Agent": "MyBot"}) as resp:
            data = resp.read()
    """
    opener = SSRFProtectedOpener(config, headers=headers)
    return opener.open(url, timeout=timeout)


# For backward compatibility with urllib.error.URLError
from urllib.error import URLError

__all__ = [
    "SSRFConfig",
    "DEFAULT_CONFIG",
    "validate_url",
    "validate_redirect_url",
    "SSRFProtectedOpener",
    "safe_urlopen",
    "CLOUD_METADATA_HOSTS",
    "PRIVATE_NETWORKS",
]