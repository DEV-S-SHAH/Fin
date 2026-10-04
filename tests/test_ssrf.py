"""Tests for SSRF protection in sandbox_engine.ssrf."""

import socket
import unittest
from unittest.mock import patch

from sandbox_engine.ssrf import (
    DEFAULT_CONFIG,
    CLOUD_METADATA_HOSTS,
    PRIVATE_NETWORKS,
    SSRFConfig,
    _is_cloud_metadata,
    _is_private_ip,
    _resolve_host,
    validate_url,
    validate_redirect_url,
)
import ipaddress


class IsPrivateIPTests(unittest.TestCase):
    """Test private IP detection."""

    def test_loopback_ipv4(self):
        ip = ipaddress.IPv4Address("127.0.0.1")
        self.assertTrue(_is_private_ip(ip))

    def test_loopback_ipv6(self):
        ip = ipaddress.IPv6Address("::1")
        self.assertTrue(_is_private_ip(ip))

    def test_private_10_network(self):
        ip = ipaddress.IPv4Address("10.0.0.1")
        self.assertTrue(_is_private_ip(ip))

    def test_private_172_network(self):
        ip = ipaddress.IPv4Address("172.16.0.1")
        self.assertTrue(_is_private_ip(ip))

    def test_private_192_network(self):
        ip = ipaddress.IPv4Address("192.168.1.1")
        self.assertTrue(_is_private_ip(ip))

    def test_link_local(self):
        ip = ipaddress.IPv4Address("169.254.169.254")
        self.assertTrue(_is_private_ip(ip))

    def test_carrier_grade_nat(self):
        ip = ipaddress.IPv4Address("100.64.0.1")
        self.assertTrue(_is_private_ip(ip))

    def test_public_ip(self):
        ip = ipaddress.IPv4Address("8.8.8.8")
        self.assertFalse(_is_private_ip(ip))

    def test_public_ipv6(self):
        ip = ipaddress.IPv6Address("2001:4860:4860::8888")
        self.assertFalse(_is_private_ip(ip))


class IsCloudMetadataTests(unittest.TestCase):
    """Test cloud metadata endpoint detection."""

    def test_aws_metadata(self):
        self.assertTrue(_is_cloud_metadata("169.254.169.254"))

    def test_gcp_metadata(self):
        self.assertTrue(_is_cloud_metadata("metadata.google.internal"))

    def test_azure_metadata(self):
        self.assertTrue(_is_cloud_metadata("169.254.169.254"))

    def test_digitalocean_metadata(self):
        self.assertTrue(_is_cloud_metadata("169.254.169.254"))

    def test_oracle_metadata(self):
        self.assertTrue(_is_cloud_metadata("169.254.169.254"))

    def test_regular_hostname(self):
        self.assertFalse(_is_cloud_metadata("www.google.com"))

    def test_case_insensitive(self):
        self.assertTrue(_is_cloud_metadata("METADATA.GOOGLE.INTERNAL"))


class ValidateURLTests(unittest.TestCase):
    """Test URL validation against SSRF protection."""

    def test_valid_sec_url(self):
        valid, error = validate_url("https://www.sec.gov/files/company_tickers.json", DEFAULT_CONFIG)
        self.assertTrue(valid, error)
        self.assertIsNone(error)

    def test_valid_data_sec_url(self):
        valid, error = validate_url("https://data.sec.gov/submissions/CIK0000320193.json", DEFAULT_CONFIG)
        self.assertTrue(valid, error)

    def test_valid_yahoo_url(self):
        valid, error = validate_url("https://query1.finance.yahoo.com/v8/finance/chart/AAPL", DEFAULT_CONFIG)
        self.assertTrue(valid, error)

    def test_valid_google_oauth_url(self):
        valid, error = validate_url("https://accounts.google.com/o/oauth2/token", DEFAULT_CONFIG)
        self.assertTrue(valid, error)

    def test_valid_apple_oauth_url(self):
        valid, error = validate_url("https://appleid.apple.com/auth/keys", DEFAULT_CONFIG)
        self.assertTrue(valid, error)

    def test_valid_nvidia_url(self):
        valid, error = validate_url("https://integrate.api.nvidia.com/v1/models", DEFAULT_CONFIG)
        self.assertTrue(valid, error)

    def test_invalid_scheme(self):
        valid, error = validate_url("ftp://example.com/file", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("invalid_scheme", error)

    def test_missing_hostname(self):
        valid, error = validate_url("https:///path", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("missing_hostname", error)

    def test_cloud_metadata_blocked(self):
        valid, error = validate_url("http://169.254.169.254/latest/meta-data/", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("cloud_metadata_endpoint_blocked", error)

    def test_localhost_blocked(self):
        valid, error = validate_url("http://localhost:8080/admin", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("private_ip_blocked", error)

    def test_private_ip_blocked(self):
        valid, error = validate_url("http://10.0.0.1/internal", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("private_ip_blocked", error)

    def test_172_network_blocked(self):
        valid, error = validate_url("http://172.16.0.1/admin", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("private_ip_blocked", error)

    def test_192_network_blocked(self):
        valid, error = validate_url("http://192.168.1.1/admin", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("private_ip_blocked", error)

    def test_link_local_blocked(self):
        valid, error = validate_url("http://169.254.0.1/admin", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("private_ip_blocked", error)

    def test_not_in_allowlist_blocked(self):
        valid, error = validate_url("https://evil.com/steal", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("host_not_allowed", error)

    @patch("sandbox_engine.ssrf._resolve_host")
    def test_dns_rebinding_detected(self, mock_resolve):
        mock_resolve.return_value = [ipaddress.IPv4Address("10.0.0.1")]
        valid, error = validate_url("https://www.sec.gov/files/company_tickers.json", DEFAULT_CONFIG)
        self.assertFalse(valid)
        self.assertIn("dns_rebinding_detected", error)

    @patch("sandbox_engine.ssrf._resolve_host")
    def test_dns_rebinding_public_ip_allowed(self, mock_resolve):
        mock_resolve.return_value = [ipaddress.IPv4Address("192.0.2.1")]  # TEST-NET-1
        valid, error = validate_url("https://www.sec.gov/files/company_tickers.json", DEFAULT_CONFIG)
        self.assertTrue(valid, error)


class ValidateRedirectTests(unittest.TestCase):
    """Test redirect URL validation."""

    def test_redirect_not_allowed_by_default(self):
        valid, error = validate_redirect_url(
            "https://evil.com", "https://www.sec.gov", DEFAULT_CONFIG, 0
        )
        self.assertFalse(valid)
        self.assertIn("redirects_not_allowed", error)

    def test_max_redirects_exceeded(self):
        config = SSRFConfig(
            allowed_hosts=DEFAULT_CONFIG.allowed_hosts,
            allow_localhost=DEFAULT_CONFIG.allow_localhost,
            follow_redirects=True,
            max_redirects=5,
        )
        valid, error = validate_redirect_url(
            "https://www.sec.gov/redirect", "https://www.sec.gov", config, 5
        )
        self.assertFalse(valid)
        self.assertIn("max_redirects_exceeded", error)

    def test_valid_redirect_to_allowed_host(self):
        config = SSRFConfig(
            allowed_hosts=DEFAULT_CONFIG.allowed_hosts,
            allow_localhost=DEFAULT_CONFIG.allow_localhost,
            follow_redirects=True,
            max_redirects=5,
        )
        valid, error = validate_redirect_url(
            "https://www.sec.gov/new-path", "https://www.sec.gov", config, 0
        )
        self.assertTrue(valid, error)

    def test_redirect_to_blocked_host(self):
        config = SSRFConfig(
            allowed_hosts=DEFAULT_CONFIG.allowed_hosts,
            allow_localhost=DEFAULT_CONFIG.allow_localhost,
            follow_redirects=True,
            max_redirects=5,
        )
        valid, error = validate_redirect_url(
            "http://169.254.169.254/meta", "https://www.sec.gov", config, 0
        )
        self.assertFalse(valid)
        self.assertIn("cloud_metadata_endpoint_blocked", error)


class ResolveHostTests(unittest.TestCase):
    """Test host resolution."""

    @patch("socket.getaddrinfo")
    def test_resolve_host(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.1", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2001:db8::1", 0)),
        ]
        ips = _resolve_host("example.com")
        self.assertEqual(len(ips), 2)
        self.assertIn(ipaddress.IPv4Address("192.0.2.1"), ips)
        self.assertIn(ipaddress.IPv6Address("2001:db8::1"), ips)

    @patch("socket.getaddrinfo")
    def test_resolve_host_failure(self, mock_getaddrinfo):
        import socket
        mock_getaddrinfo.side_effect = socket.gaierror("Name or service not known")
        ips = _resolve_host("nonexistent.example.com")
        self.assertEqual(ips, [])


if __name__ == "__main__":
    unittest.main()