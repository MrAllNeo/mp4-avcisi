"""Network boundary for the isolated media worker, including redirected requests."""

import ipaddress
import base64
import http.client
import json
import socket
import ssl
import time
from urllib.parse import urlsplit, urlencode


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


def proxy_connect(sock, original, destination, proxy):
    """HTTP CONNECT to a pinned PUBLIC IP; the proxy never resolves the target.

    HTTP Host and TLS SNI still come from the original URL in yt-dlp. Both HTTP
    and HTTPS use CONNECT so redirects cannot sneak private hosts past the guard.
    """
    host, port = destination[0], destination[1]
    authority = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
    endpoints = socket.getaddrinfo(proxy['host'], proxy['port'], family=sock.family, type=socket.SOCK_STREAM,
                                   flags=socket.AI_V4MAPPED if sock.family == socket.AF_INET6 else 0)
    if not endpoints:
        raise OSError('VPN bağlantısına ulaşılamadı.')
    original(sock, endpoints[0][4])
    credentials = base64.b64encode(f"{proxy['username']}:{proxy['password']}".encode()).decode()
    socket.socket.sendall(sock, (f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n'
                  f'Proxy-Authorization: Basic {credentials}\r\n\r\n').encode('ascii'))
    response = bytearray()
    # Read exactly to the header boundary, retaining every upstream data byte.
    while not response.endswith(b'\r\n\r\n'):
        chunk = socket.socket.recv(sock, 1)
        if not chunk or len(response) >= 8192:
            raise OSError('VPN bağlantısı geçersiz yanıt verdi.')
        response.extend(chunk)
    status = bytes(response).split(b'\r\n', 1)[0].split()
    if len(status) < 2 or status[0] not in {b'HTTP/1.0', b'HTTP/1.1'} or status[1] != b'200':
        raise OSError('VPN bağlantısı kurulamadı.')


def dns_answers(data, record_type):
    try:
        if not isinstance(data, dict) or data.get('Status') != 0:
            raise ValueError
        addresses = [ipaddress.ip_address(item['data']) for item in data.get('Answer', [])
                     if item.get('type') == record_type]
        if not addresses:
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise socket.gaierror(socket.EAI_NONAME, 'VPN DNS çözümlemesi başarısız.') from None
    if any(not address.is_global for address in addresses):
        raise OSError('Yerel ve özel ağ adreslerine erişilemiyor.')
    return [str(address) for address in addresses]


def vpn_addresses(host, family, proxy, original_connect):
    """Resolve through HTTPS inside the VPN, with a pinned resolver IP and TLS.

    Never use the host's DNS as a fallback. Only the DNS query, not a source URL
    or credentials, reaches Cloudflare's resolver. Final addresses are checked
    before CONNECT, protecting against private answers and DNS rebinding.
    """
    record_type = 28 if family == socket.AF_INET6 else 1
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(10)
        proxy_connect(raw, original_connect, ('1.1.1.1', 443), proxy)
        with ssl.create_default_context().wrap_socket(raw, server_hostname='cloudflare-dns.com') as encrypted:
            connection = http.client.HTTPConnection('cloudflare-dns.com', timeout=10)
            connection.sock = encrypted
            try:
                query = urlencode({'name': host.encode('idna').decode(), 'type': record_type})
                connection.request('GET', '/dns-query?' + query, headers={'Accept': 'application/dns-json'})
                response = connection.getresponse()
                data = response.read(16385)
                if response.status != 200 or len(data) > 16384:
                    raise ValueError
                return dns_answers(json.loads(data), record_type)
            except (ValueError, http.client.HTTPException):
                raise socket.gaierror(socket.EAI_FAIL, 'VPN DNS çözümlemesi başarısız.') from None
            finally:
                connection.close()


def guard_network(proxy=None):
    """Resolve once, validate all answers, connect to a numeric public address.

    Applied only in a disposable subprocess; never in the web server. Guarding
    the actual socket covers manifests, segments, embeds and HTTP redirects.
    """
    original = socket.socket.connect
    if proxy:
        resolve = socket.getaddrinfo
        cache = {}

        def vpn_resolve(host, port, family=0, type=0, proto=0, flags=0):
            if isinstance(host, bytes):
                host = host.decode('ascii')
            try:
                ipaddress.ip_address(host)
            except ValueError:
                key = (host, family)
                cached = cache.get(key)
                if cached is None or cached[0] <= time.monotonic():
                    addresses = vpn_addresses(host, family, proxy, original)
                    cache[key] = (time.monotonic() + 30, addresses)
                else:
                    addresses = cached[1]
                # Numeric-only conversion preserves the caller's family/socket
                # requirements without another DNS lookup on the local network.
                return [entry for address in addresses for entry in
                        resolve(address, port, family, type, proto, flags | socket.AI_NUMERICHOST)]
            return resolve(host, port, family, type, proto, flags)

        socket.getaddrinfo = vpn_resolve

    def connect(sock, address):
        if sock.family not in {socket.AF_INET, socket.AF_INET6}:
            raise OSError("Bu bağlantı türü desteklenmiyor.")
        addresses = public_addresses(address[0], address[1])
        candidate = next((a for a in addresses if a[0] == sock.family), None)
        if candidate is None:
            raise OSError("Uygun bir genel ağ adresi bulunamadı.")
        if proxy:
            try:
                return proxy_connect(sock, original, candidate[4], proxy)
            except BaseException:
                sock.close()
                raise
        return original(sock, candidate[4])

    def connect_ex(sock, address):
        try:
            connect(sock, address)
            return 0
        except OSError as exc:
            return exc.errno or 13

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
