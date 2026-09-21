"""On-demand Proton WireGuard gateway; the host's default route is untouched."""
import asyncio
import base64
import configparser
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import os
from pathlib import Path
import secrets
import shutil

from app import diagnostics
from app.errors import MediaError

IMAGE = 'qmcgaw/gluetun:v3.41.3'
PROXY_PORT = 18989
FALLBACK_CODES = {'geo_blocked', 'access_denied', 'network', 'timeout', 'tls_failed', 'source_parse'}


def can_retry_via_vpn(error):
    return (error.code in FALLBACK_CODES
            and getattr(error, 'operation_stage', 'download') in {'startup', 'extract', 'download'})


def validate_config(path):
    """Validate without including config values in exceptions or command lines."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(path.read_text())
        for section, key in [('Interface', 'PrivateKey'), ('Peer', 'PublicKey')]:
            if len(base64.b64decode(config[section][key], validate=True)) != 32:
                raise ValueError
        for value in config['Interface']['Address'].split(','):
            ipaddress.ip_interface(value.strip())
        endpoint, port = config['Peer']['Endpoint'].rsplit(':', 1)
        if not ipaddress.ip_address(endpoint.strip('[]')).is_global or not 1 <= int(port) <= 65535:
            raise ValueError
        if '0.0.0.0/0' not in config['Peer']['AllowedIPs'].replace(' ', '').split(','):
            raise ValueError
        path.chmod(0o600)
    except (OSError, ValueError, KeyError, configparser.Error):
        raise MediaError('vpn_config', 'Proton VPN yapılandırması eksik veya geçersiz. Sunucu ayarlarını kontrol et.') from None


class ProtonGateway:
    def __init__(self, root):
        self.root = Path(root)
        self.config = self.root / 'wg0.conf'
        suffix = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:10]
        self.name = f'mp4-proton-{suffix}'
        self.label = f'com.toywes.mp4.gateway={suffix}'
        self.lock = asyncio.Lock()
        self.users = 0
        self.state = 'idle'
        self.proxy = None
        self.process = None
        self.env_config_written = False

    @property
    def mode(self):
        return os.environ.get('MP4_VPN_MODE', 'docker').strip().lower()

    @property
    def wireproxy_binary(self):
        return os.environ.get('MP4_WIREPROXY_BINARY', '/usr/local/bin/wireproxy')

    @property
    def configured(self):
        supplied = bool(os.environ.get('MP4_VPN_CONFIG_B64', '').strip())
        return os.environ.get('MP4_VPN_AUTO', '1') == '1' and (self.config.is_file() or supplied)

    def public(self):
        country = os.environ.get('MP4_VPN_COUNTRY', '').strip().upper()[:8] or None
        return {'provider': 'proton', 'transport': self.mode, 'country': country,
                'configured': self.configured,
                'state': self.state if self.configured else 'unconfigured', 'active_jobs': self.users}

    def materialize_env_config(self):
        """Write a Railway secret to a private file without ever logging its value."""
        encoded = os.environ.get('MP4_VPN_CONFIG_B64', '').strip()
        if not encoded:
            return
        try:
            if len(encoded) > 32768:
                raise ValueError
            content = base64.b64decode(encoded, validate=True).decode('utf-8')
            if len(content.encode()) > 16384:
                raise ValueError
        except (ValueError, UnicodeError):
            raise MediaError('vpn_config', 'Proton VPN yapılandırma değişkeni geçersiz.') from None
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        temporary = self.root / '.wg0.conf.tmp'
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
            validate_config(temporary)
            os.replace(temporary, self.config)
            self.config.chmod(0o600)
            self.env_config_written = True
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def write_private(self, path, content):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)

    async def wireproxy_ready(self, username, password):
        reader, writer = await asyncio.open_connection('127.0.0.1', PROXY_PORT)
        try:
            token = base64.b64encode(f'{username}:{password}'.encode()).decode()
            writer.write((f'CONNECT 1.1.1.1:443 HTTP/1.1\r\n'
                          f'Host: 1.1.1.1:443\r\nProxy-Authorization: Basic {token}\r\n\r\n').encode())
            await writer.drain()
            response = await reader.readuntil(b'\r\n\r\n')
            if not response.startswith((b'HTTP/1.1 200', b'HTTP/1.0 200')):
                raise OSError
        finally:
            writer.close()
            await writer.wait_closed()

    async def start_wireproxy(self, username, password):
        binary = self.wireproxy_binary
        if not Path(binary).is_file() and shutil.which(binary) is None:
            raise MediaError('vpn_unavailable', 'WireProxy çalıştırıcısı sunucuda bulunamadı.', True)
        proxy_config = self.root / 'wireproxy.conf'
        self.write_private(proxy_config,
            f'WGConfig = {self.config.resolve()}\n\n[http]\n'
            f'BindAddress = 127.0.0.1:{PROXY_PORT}\nUsername = {username}\nPassword = {password}\n')
        try:
            self.process = await asyncio.create_subprocess_exec(
                binary, '-c', str(proxy_config), '-s', stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(45):
                while True:
                    if self.process.returncode is not None:
                        raise MediaError('vpn_unavailable', 'WireGuard VPN bağlantısı başlatılamadı.', True)
                    try:
                        await asyncio.wait_for(self.wireproxy_ready(username, password), timeout=8)
                        break
                    except (OSError, asyncio.IncompleteReadError, TimeoutError):
                        await asyncio.sleep(1)
        except TimeoutError:
            raise MediaError('vpn_unavailable', 'WireGuard VPN bağlantısı zaman aşımına uğradı.', True) from None
        except OSError:
            raise MediaError('vpn_unavailable', 'WireProxy çalıştırılamadı.', True) from None

    async def docker(self, *args, timeout=20):
        process = None
        try:
            # Explicit local daemon: proxy ports and bind mounts must be on this host.
            process = await asyncio.create_subprocess_exec(
                'docker', '--host', 'unix:///var/run/docker.sock', *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(timeout):
                output, _ = await process.communicate()
            if process.returncode:
                raise MediaError('vpn_unavailable', 'Proton VPN başlatılamadı. Docker ve VPN yapılandırmasını kontrol et.', True)
            return output.decode().strip()
        except (OSError, TimeoutError):
            raise MediaError('vpn_unavailable', 'Proton VPN hizmetine ulaşılamadı. Daha sonra yeniden dene.', True) from None
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()

    async def remove_container(self):
        # Remove only this project's labelled gateway, including a previous crash's orphan.
        ids = (await self.docker('ps', '-aq', '--filter', f'label={self.label}')).split()
        for container in ids:
            await self.docker('rm', '-f', container)

    async def start(self):
        if self.mode not in {'docker', 'wireproxy'}:
            raise MediaError('vpn_config', 'MP4_VPN_MODE değeri geçersiz.')
        self.materialize_env_config()
        validate_config(self.config)
        self.state = 'starting'
        diagnostics.record('vpn_starting')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        username = 'mp4'
        password = secrets.token_hex(24)
        if self.mode == 'wireproxy':
            await self.start_wireproxy(username, password)
        else:
            await self.remove_container()
            envfile = self.root / 'proxy.env'
            self.write_private(envfile, f'HTTPPROXY_USER={username}\nHTTPPROXY_PASSWORD={password}\n')
            await self.docker(
                'run', '--detach', '--rm', '--pull=never', '--name', self.name, '--label', self.label,
                '--cap-add=NET_ADMIN', '--device=/dev/net/tun:/dev/net/tun',
                '--publish', f'127.0.0.1:{PROXY_PORT}:8888/tcp',
                '--mount', f'type=bind,src={self.config.resolve()},dst=/gluetun/wireguard/wg0.conf,readonly',
                '--env-file', str(envfile),
                '--env', 'VPN_SERVICE_PROVIDER=custom', '--env', 'VPN_TYPE=wireguard',
                '--env', 'HTTPPROXY=on', '--env', 'HTTPPROXY_LOG=off',
                '--env', 'FIREWALL=on', '--log-driver=none', IMAGE)
            async with asyncio.timeout(45):
                while True:
                    status = await self.docker('inspect', '--format',
                        '{{if .State.Running}}{{.State.Health.Status}}{{else}}exited{{end}}', self.name)
                    if status == 'healthy':
                        break
                    if status not in {'starting', 'unhealthy'}:
                        raise MediaError('vpn_unavailable', 'Proton VPN bağlantısı hazır değil.', True)
                    await asyncio.sleep(1)
        self.proxy = {'host': '127.0.0.1', 'port': PROXY_PORT, 'username': username, 'password': password}
        self.state = 'connected'
        diagnostics.record('vpn_connected')

    async def stop(self):
        try:
            if self.process is not None:
                if self.process.returncode is None:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=10)
                    except TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                self.process = None
            elif self.mode == 'docker':
                await self.remove_container()
        except MediaError as exc:
            self.state = 'error'
            diagnostics.record('vpn_cleanup_failed', code=exc.code)
        except OSError:
            self.state = 'error'
            diagnostics.record('vpn_cleanup_failed', code='vpn_unavailable')
        else:
            self.state = 'idle'
            diagnostics.record('vpn_stopped')
        finally:
            self.proxy = None
            try:
                for name in ('proxy.env', 'wireproxy.conf'):
                    (self.root / name).unlink(missing_ok=True)
                if self.env_config_written:
                    self.config.unlink(missing_ok=True)
                    self.env_config_written = False
            except OSError:
                self.state = 'error'
                diagnostics.record('vpn_cleanup_failed', code='storage_failed')

    async def release(self):
        async with self.lock:
            self.users -= 1
            if self.users == 0:
                await self.stop()

    @asynccontextmanager
    async def connection(self):
        async with self.lock:
            if self.users == 0:
                try:
                    await self.start()
                except BaseException:
                    await self.stop()
                    raise
            self.users += 1
        try:
            yield self.proxy
        finally:
            # Cancellation of one job must neither strand the gateway nor stop
            # the connection still used by another job.
            cleanup = asyncio.create_task(self.release())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
