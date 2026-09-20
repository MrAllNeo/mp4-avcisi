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

from app import diagnostics
from app.errors import MediaError

IMAGE = 'qmcgaw/gluetun:v3.41.3'
PROXY_PORT = 18989
FALLBACK_CODES = {'geo_blocked', 'access_denied', 'network', 'timeout'}


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
        ipaddress.ip_interface(config['Interface']['Address'].split(',')[0].strip())
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

    @property
    def configured(self):
        return os.environ.get('MP4_VPN_AUTO', '1') == '1' and self.config.is_file()

    def public(self):
        return {'provider': 'proton', 'configured': self.configured,
                'state': self.state if self.configured else 'unconfigured', 'active_jobs': self.users}

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
        validate_config(self.config)
        self.state = 'starting'
        diagnostics.record('vpn_starting')
        await self.remove_container()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        password = secrets.token_hex(24)
        envfile = self.root / 'proxy.env'
        fd = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(f'HTTPPROXY_USER=mp4\nHTTPPROXY_PASSWORD={password}\n')
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
                status = await self.docker('inspect', '--format', '{{.State.Health.Status}}', self.name)
                if status == 'healthy':
                    break
                if status not in {'starting', 'unhealthy'}:
                    raise MediaError('vpn_unavailable', 'Proton VPN bağlantısı hazır değil.', True)
                await asyncio.sleep(1)
        self.proxy = {'host': '127.0.0.1', 'port': PROXY_PORT, 'username': 'mp4', 'password': password}
        self.state = 'connected'
        diagnostics.record('vpn_connected')

    async def stop(self):
        try:
            await self.remove_container()
        except MediaError as exc:
            self.state = 'error'
            diagnostics.record('vpn_cleanup_failed', code=exc.code)
        else:
            self.state = 'idle'
            diagnostics.record('vpn_stopped')
        finally:
            self.proxy = None
            try:
                (self.root / 'proxy.env').unlink(missing_ok=True)
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
