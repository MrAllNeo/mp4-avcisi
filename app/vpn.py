"""On-demand Tor or WireGuard gateway; the host route is untouched."""
import asyncio
import base64
import configparser
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil

from app import diagnostics
from app.errors import MediaError

IMAGE = 'qmcgaw/gluetun:v3.41.3'
PROXY_PORT = 18989
TOR_SOCKS_PORT = 19050
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
        raise MediaError('vpn_config', 'WireGuard VPN yapılandırması eksik veya geçersiz. Sunucu ayarlarını kontrol et.') from None


class VpnGateway:
    def __init__(self, root):
        self.root = Path(root)
        self.config = self.root / 'wg0.conf'
        suffix = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:10]
        self.name = f'mp4-vpn-{suffix}'
        self.label = f'com.toywes.mp4.gateway={suffix}'
        self.lock = asyncio.Lock()
        self.users = 0
        self.state = 'idle'
        self.proxy = None
        self.process = None
        self.tor_process = None
        self.privoxy_process = None
        self.prewarm_task = None
        self.prewarm_failed = False
        self.tor_bootstrap_percent = 0
        self.env_config_written = False

    @property
    def mode(self):
        return os.environ.get('MP4_VPN_MODE', 'docker').strip().lower()

    @property
    def wireproxy_binary(self):
        return os.environ.get('MP4_WIREPROXY_BINARY', '/usr/local/bin/wireproxy')

    @property
    def tor_binary(self):
        return os.environ.get('MP4_TOR_BINARY', '/usr/bin/tor')

    @property
    def privoxy_binary(self):
        return os.environ.get('MP4_PRIVOXY_BINARY', '/usr/sbin/privoxy')

    @property
    def tor_exit_countries(self):
        raw = os.environ.get('MP4_TOR_EXIT_COUNTRIES', 'nl,fr,ro')
        countries = [value.strip().lower() for value in raw.split(',') if value.strip()]
        if not countries or len(countries) > 8 or any(not re.fullmatch(r'[a-z]{2}', value) for value in countries):
            raise MediaError('vpn_config', 'Tor çıkış ülkeleri geçersiz. İki harfli ülke kodları kullan.')
        return countries

    @property
    def persistent(self):
        return self.mode == 'tor' and os.environ.get('MP4_TOR_PERSISTENT', '1') == '1'

    @property
    def tor_bootstrap_timeout(self):
        try:
            return max(30, min(int(os.environ.get('MP4_TOR_BOOTSTRAP_TIMEOUT', '300')), 600))
        except (TypeError, ValueError):
            return 300

    @property
    def tor_strict_nodes(self):
        return os.environ.get('MP4_TOR_STRICT_NODES', '0') == '1'

    @property
    def configured(self):
        if os.environ.get('MP4_VPN_AUTO', '1') != '1':
            return False
        if self.mode == 'tor':
            return True
        supplied = bool(os.environ.get('MP4_VPN_CONFIG_B64', '').strip())
        return self.config.is_file() or supplied

    def public(self):
        if self.mode == 'tor':
            country = '/'.join(value.upper() for value in self.tor_exit_countries)
            default_provider = 'tor'
        else:
            country = os.environ.get('MP4_VPN_COUNTRY', '').strip().upper()[:8] or None
            default_provider = 'wireguard'
        provider = re.sub(r'[^a-z0-9._-]', '', os.environ.get('MP4_VPN_PROVIDER', default_provider).strip().lower())[:32]
        return {'provider': provider or 'wireguard', 'transport': self.mode, 'country': country,
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
            raise MediaError('vpn_config', 'WireGuard VPN yapılandırma değişkeni geçersiz.') from None
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

    async def proxy_ready(self, username, password):
        reader, writer = await asyncio.open_connection('127.0.0.1', PROXY_PORT)
        try:
            token = base64.b64encode(f'{username}:{password}'.encode()).decode()
            target = 'check.torproject.org' if self.mode == 'tor' else '1.1.1.1'
            writer.write((f'CONNECT {target}:443 HTTP/1.1\r\n'
                          f'Host: {target}:443\r\nProxy-Authorization: Basic {token}\r\n\r\n').encode())
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
                        await asyncio.wait_for(self.proxy_ready(username, password), timeout=8)
                        break
                    except (OSError, asyncio.IncompleteReadError, TimeoutError):
                        await asyncio.sleep(1)
        except TimeoutError:
            raise MediaError('vpn_unavailable', 'WireGuard VPN bağlantısı zaman aşımına uğradı.', True) from None
        except OSError:
            raise MediaError('vpn_unavailable', 'WireProxy çalıştırılamadı.', True) from None

    async def start_tor_proxy(self, username, password):
        for binary, label in ((self.tor_binary, 'Tor'), (self.privoxy_binary, 'Privoxy')):
            if not Path(binary).is_file() and shutil.which(binary) is None:
                raise MediaError('vpn_unavailable', f'{label} çalıştırıcısı sunucuda bulunamadı.', True)
        data_directory = self.root / 'tor-data'
        data_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        data_directory.chmod(0o700)
        exits = ','.join(f'{{{country}}}' for country in self.tor_exit_countries)
        tor_config = self.root / 'torrc'
        tor_log = self.root / 'tor-notice.log'
        privoxy_config = self.root / 'privoxy.conf'
        tor_log.unlink(missing_ok=True)
        self.write_private(tor_config,
            f'ClientOnly 1\nSocksPort 127.0.0.1:{TOR_SOCKS_PORT}\n'
            f'DataDirectory {data_directory.resolve()}\nAvoidDiskWrites 1\n'
            f'ExitNodes {exits}\nStrictNodes {int(self.tor_strict_nodes)}\n'
            f'Log notice file {tor_log.resolve()}\n')
        self.write_private(privoxy_config,
            f'listen-address 127.0.0.1:{PROXY_PORT}\n'
            f'forward-socks5t / 127.0.0.1:{TOR_SOCKS_PORT} .\n'
            'toggle 1\nenable-remote-toggle 0\nenable-remote-http-toggle 0\n'
            'enable-edit-actions 0\nlogfile /dev/null\ndebug 0\n')
        try:
            self.tor_process = await asyncio.create_subprocess_exec(
                self.tor_binary, '-f', str(tor_config),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            self.privoxy_process = await asyncio.create_subprocess_exec(
                self.privoxy_binary, '--no-daemon', str(privoxy_config),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            last_percent = -1
            async with asyncio.timeout(self.tor_bootstrap_timeout):
                while True:
                    if self.tor_process.returncode is not None or self.privoxy_process.returncode is not None:
                        error = MediaError('vpn_unavailable', 'Tor alternatif bağlantısı başlatılamadı.', True)
                        error.diagnostic = {'stage': 'startup', 'percent': max(0, last_percent)}
                        raise error
                    try:
                        content = tor_log.read_text(encoding='utf-8')[-16384:]
                        matches = re.findall(r'Bootstrapped (\d+)%', content)
                        percent = int(matches[-1]) if matches else last_percent
                        if percent != last_percent and percent >= 0:
                            last_percent = percent
                            self.tor_bootstrap_percent = percent
                            diagnostics.record('tor_bootstrap', stage='startup', percent=percent)
                    except OSError:
                        pass
                    try:
                        await asyncio.wait_for(self.proxy_ready(username, password), timeout=8)
                        break
                    except (OSError, asyncio.IncompleteReadError, TimeoutError):
                        await asyncio.sleep(1)
        except TimeoutError:
            error = MediaError('vpn_unavailable', 'Tor ağına bağlantı zaman aşımına uğradı.', True)
            error.diagnostic = {'stage': 'startup', 'percent': max(0, locals().get('last_percent', -1))}
            raise error from None
        except OSError:
            raise MediaError('vpn_unavailable', 'Tor alternatif bağlantısı çalıştırılamadı.', True) from None

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
                raise MediaError('vpn_unavailable', 'WireGuard VPN başlatılamadı. Docker ve VPN yapılandırmasını kontrol et.', True)
            return output.decode().strip()
        except (OSError, TimeoutError):
            raise MediaError('vpn_unavailable', 'WireGuard VPN hizmetine ulaşılamadı. Daha sonra yeniden dene.', True) from None
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
        if self.mode not in {'docker', 'wireproxy', 'tor'}:
            raise MediaError('vpn_config', 'MP4_VPN_MODE değeri geçersiz.')
        if self.mode != 'tor':
            self.materialize_env_config()
            validate_config(self.config)
        self.state = 'starting'
        diagnostics.record('vpn_starting')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        username = 'mp4'
        password = secrets.token_hex(24)
        if self.mode == 'tor':
            await self.start_tor_proxy(username, password)
        elif self.mode == 'wireproxy':
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
                        raise MediaError('vpn_unavailable', 'WireGuard VPN bağlantısı hazır değil.', True)
                    await asyncio.sleep(1)
        self.proxy = {'host': '127.0.0.1', 'port': PROXY_PORT, 'username': username, 'password': password}
        self.state = 'connected'
        self.prewarm_failed = False
        diagnostics.record('vpn_connected')

    async def stop(self):
        try:
            processes = [process for process in (self.process, self.privoxy_process, self.tor_process)
                         if process is not None]
            if processes:
                for process in processes:
                    if process.returncode is None:
                        process.terminate()
                for process in processes:
                    if process.returncode is not None:
                        continue
                    try:
                        await asyncio.wait_for(process.wait(), timeout=10)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                self.process = None
                self.privoxy_process = None
                self.tor_process = None
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
                for name in ('proxy.env', 'wireproxy.conf', 'torrc', 'privoxy.conf', 'tor-notice.log'):
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
            if self.users == 0 and not self.persistent:
                await self.stop()

    async def prewarm(self):
        """Prepare persistent Tor without delaying the web health check."""
        if not self.configured or not self.persistent:
            return
        self.prewarm_task = asyncio.current_task()
        try:
            await self.start()
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as exc:
            error = exc if isinstance(exc, MediaError) else MediaError('vpn_unavailable', 'Tor bağlantısı hazırlanamadı.', True)
            detail = getattr(error, 'diagnostic', {})
            diagnostics.record('vpn_prewarm_failed', code=error.code,
                               stage='startup', percent=detail.get('percent', self.tor_bootstrap_percent))
            await self.stop()
            self.state = 'error'
            self.prewarm_failed = True
        finally:
            self.prewarm_task = None

    @asynccontextmanager
    async def connection(self):
        if self.persistent and self.prewarm_task is not None and not self.prewarm_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.prewarm_task), timeout=8)
            except TimeoutError:
                error = MediaError('vpn_unavailable',
                    f'Tor ağı hazırlanıyor (%{self.tor_bootstrap_percent}). Biraz sonra yeniden dene.', True)
                error.diagnostic = {'stage': 'startup', 'percent': self.tor_bootstrap_percent}
                raise error from None
        if self.persistent and self.prewarm_failed:
            error = MediaError('vpn_unavailable',
                f'Tor ağı hazırlanamadı (%{self.tor_bootstrap_percent}). Sunucu ağını kontrol et.', True)
            error.diagnostic = {'stage': 'startup', 'percent': self.tor_bootstrap_percent}
            raise error
        async with self.lock:
            if self.users == 0 and self.proxy is None:
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


# Import compatibility for integrations created before provider-neutral naming.
ProtonGateway = VpnGateway
