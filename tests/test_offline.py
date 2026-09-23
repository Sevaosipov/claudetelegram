"""conftest's _offline guard: the suite can't reach the network, through requests or
through yfinance's curl_cffi session."""
from __future__ import annotations

import pytest
import requests


def test_requests_cannot_connect():
    with pytest.raises(requests.ConnectionError):
        requests.get("https://example.com", timeout=5)


def test_curl_cffi_cannot_connect():
    curl_requests = pytest.importorskip("curl_cffi.requests")
    with pytest.raises(ConnectionRefusedError):
        curl_requests.Session().get("https://example.com")
