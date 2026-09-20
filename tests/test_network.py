import socket

import pytest

from app.network import public_addresses, validate_url


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a", "https://u:p@example.com/a", "https://example.com:8080/a", "not a url", "https://example.com/\nsecret"])
def test_rejects_unsafe_url_syntax(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_strips_surrounding_whitespace():
    assert validate_url(" https://example.com/movie.mp4 ") == "https://example.com/movie.mp4"


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "::ffff:127.0.0.1", "fc00::1", "0.0.0.0"])
def test_blocks_private_dns_answers(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))])
    with pytest.raises(OSError):
        public_addresses("innocent.example", 443)


def test_rejects_mixed_dns_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", (ip, 443)) for ip in ["8.8.8.8", "10.1.2.3"]])
    with pytest.raises(OSError):
        public_addresses("mixed.example", 443)


def test_public_dns_is_allowed(monkeypatch):
    answer = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: answer)
    assert public_addresses("public.example", 443) == answer
