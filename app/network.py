"""Network boundary for the isolated media worker, including redirected requests."""

import ipaddress
import socket
from urllib.parse import urlsplit


def validate_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
            raise ValueError
        if len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError
    except ValueError:
        raise ValueError("Geçerli bir http:// veya https:// video bağlantısı gir.") from None
    return value


def public_addresses(host: str, port: int):
    if port not in {80, 443}:
        raise OSError("Yalnızca HTTP ve HTTPS bağlantılarına izin veriliyor.")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0].split('%')[0]).is_global for a in addresses):
        raise OSError("Yerel ve özel ağ adreslerine erişilemiyor.")
    return addresses


def guard_network():
    """Resolve once, validate all answers, connect to a numeric public address.

    Applied only in a disposable subprocess; never in the web server. Guarding
    the actual socket covers manifests, segments, embeds and HTTP redirects.
    """
    original = socket.socket.connect

    def connect(sock, address):
        if sock.family not in {socket.AF_INET, socket.AF_INET6}:
            raise OSError("Bu bağlantı türü desteklenmiyor.")
        addresses = public_addresses(address[0], address[1])
        candidate = next((a for a in addresses if a[0] == sock.family), None)
        if candidate is None:
            raise OSError("Uygun bir genel ağ adresi bulunamadı.")
        return original(sock, candidate[4])

    def connect_ex(sock, address):
        try:
            connect(sock, address)
            return 0
        except OSError as exc:
            return exc.errno or 13

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
