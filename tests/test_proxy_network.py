"""Exercise real CONNECT sockets, including TLS, without an external service."""
import base64
import socket
import socketserver
import ssl
import subprocess
import threading

import pytest

from app import network


@pytest.fixture
def tunnel(tmp_path):
    key, cert = tmp_path / 'key.pem', tmp_path / 'cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=video.example', '-addext', 'subjectAltName=DNS:video.example'],
                   check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    requests = []
    settings = {'tls': False, 'status': 200}
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            headers = b''
            while not headers.endswith(b'\r\n\r\n'):
                part = self.request.recv(1)
                if not part:
                    return
                headers += part
            requests.append(headers)
            self.request.sendall(f"HTTP/1.1 {settings['status']} Result\r\n\r\n".encode())
            if settings['status'] != 200:
                return
            connection = context.wrap_socket(self.request, server_side=True) if settings['tls'] else self.request
            try:
                data = connection.recv(100)
                if data:
                    connection.sendall(b'answer:' + data)
            finally:
                if settings['tls']:
                    connection.close()
    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {'host': '127.0.0.1', 'port': server.server_address[1], 'username': 'mp4', 'password': 'test-only'}, requests, settings, cert
    server.shutdown()
    server.server_close()
    thread.join()


def install_guard(monkeypatch, proxy, public_ip='93.184.216.34'):
    real_resolve = socket.getaddrinfo
    def resolve(host, port, *args, **kwargs):
        if host == proxy['host'] and port == proxy['port']:
            return real_resolve(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (public_ip, port))]
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(network, 'vpn_addresses', lambda *args: [public_ip])
    # Record the original functions so pytest restores the disposable guard.
    monkeypatch.setattr(socket.socket, 'connect', socket.socket.connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', socket.socket.connect_ex)
    network.guard_network(proxy=proxy)


@pytest.mark.parametrize('tls', [False, True])
def test_guarded_proxy_preserves_http_and_tls(monkeypatch, tunnel, tls):
    proxy, requests, settings, cert = tunnel
    settings['tls'] = tls
    install_guard(monkeypatch, proxy)
    connection = socket.socket()
    if tls:
        context = ssl.create_default_context(cafile=str(cert))
        connection = context.wrap_socket(connection, server_hostname='video.example')
    with connection:
        connection.settimeout(3)
        connection.connect(('video.example', 443 if tls else 80))
        connection.sendall(b'hello')
        assert connection.recv(100) == b'answer:hello'
    assert requests[0].startswith(f"CONNECT 93.184.216.34:{443 if tls else 80} HTTP/1.1".encode())
    assert base64.b64encode(b'mp4:test-only') in requests[0]
    assert b'video.example' not in requests[0]  # destination DNS is never delegated


@pytest.mark.parametrize('ip', ['127.0.0.1', '169.254.169.254', '10.0.0.1', '::1'])
def test_proxy_cannot_bypass_private_destination_checks(monkeypatch, tunnel, ip):
    proxy, requests, _, _ = tunnel
    install_guard(monkeypatch, proxy, ip)
    with socket.socket() as connection:
        with pytest.raises(OSError, match='Yerel ve özel'):
            connection.connect(('video.example', 443))
    assert not requests


def test_proxy_failure_closes_socket_without_direct_fallback(monkeypatch, tunnel):
    proxy, requests, settings, _ = tunnel
    settings['status'] = 407
    install_guard(monkeypatch, proxy)
    with socket.socket() as connection:
        with pytest.raises(OSError, match='VPN bağlantısı'):
            connection.connect(('video.example', 443))
        assert connection.fileno() == -1
    assert len(requests) == 1


@pytest.mark.parametrize('answers', [['10.0.0.1'], ['8.8.8.8', '127.0.0.1'], ['169.254.169.254']])
def test_encrypted_dns_rejects_private_and_mixed_answers(answers):
    with pytest.raises(OSError, match='Yerel ve özel'):
        network.dns_answers({'Status': 0, 'Answer': [{'type': 1, 'data': ip} for ip in answers]}, 1)


@pytest.mark.parametrize('data', [{}, {'Status': 3}, {'Status': 0, 'Answer': []},
                                {'Status': 0, 'Answer': [{'type': 1, 'data': 'invalid-ip'}]}])
def test_bad_dns_answers_fail_closed(data):
    with pytest.raises(socket.gaierror):
        network.dns_answers(data, 1)


def test_public_dns_answers_ignore_cname_but_validate_addresses():
    data = {'Status': 0, 'Answer': [{'type': 5, 'data': 'cdn.example'}, {'type': 1, 'data': '8.8.8.8'}]}
    assert network.dns_answers(data, 1) == ['8.8.8.8']
