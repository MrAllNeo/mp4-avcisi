import asyncio
import base64
from contextlib import asynccontextmanager
import json

import pytest

from app import main
from app.configure_vpn import import_config
from app.errors import MediaError, describe_error
from app.vpn import ProtonGateway, validate_config


def config_text():
    key = base64.b64encode(bytes(range(32))).decode()
    return f'[Interface]\nPrivateKey = {key}\nAddress = 10.2.0.2/32\n[Peer]\nPublicKey = {key}\nAllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = 8.8.8.8:51820\n'


def test_config_import_is_private_atomic_and_preserves_previous_on_error(tmp_path):
    source = tmp_path / 'download.conf'
    source.write_text(config_text())
    target = tmp_path / 'proton/wg0.conf'
    import_config(source, target)
    assert target.read_text() == config_text()
    assert target.stat().st_mode & 0o777 == 0o600
    source.write_text('PrivateKey = do-not-log-me')
    with pytest.raises(MediaError) as caught:
        import_config(source, target)
    assert 'do-not-log-me' not in str(caught.value)
    assert target.read_text() == config_text()
    assert not list(target.parent.glob('.wireguard-*'))


@pytest.mark.parametrize('replace', [('8.8.8.8', '127.0.0.1'), ('0.0.0.0/0', '10.0.0.0/8'), ('51820', '99999')])
def test_rejects_invalid_or_local_vpn_endpoint(tmp_path, replace):
    path = tmp_path / 'wg0.conf'
    path.write_text(config_text().replace(*replace))
    with pytest.raises(MediaError) as caught:
        validate_config(path)
    assert caught.value.code == 'vpn_config'


def test_dual_stack_proton_import_uses_ipv4_and_preserves_original(tmp_path):
    import configparser
    source = tmp_path / 'proton.conf'
    original = config_text().replace('10.2.0.2/32', '10.2.0.2/32, 2a07:b944::2:2/128')
    source.write_text(original)
    target = tmp_path / 'gateway/wg0.conf'
    import_config(source, target)
    config = configparser.ConfigParser()
    config.read(target)
    assert config['Interface']['Address'] == '10.2.0.2/32'
    assert config['Interface']['PrivateKey'] == base64.b64encode(bytes(range(32))).decode()
    assert source.read_text() == original
    assert target.stat().st_mode & 0o777 == 0o600


def test_ipv6_only_import_preserves_existing_config(tmp_path):
    source = tmp_path / 'proton.conf'
    target = tmp_path / 'gateway/wg0.conf'
    source.write_text(config_text())
    import_config(source, target)
    previous = target.read_text()
    source.write_text(config_text().replace('10.2.0.2/32', '2a07:b944::2:2/128'))
    with pytest.raises(MediaError):
        import_config(source, target)
    assert target.read_text() == previous


def test_simultaneous_jobs_share_tunnel_and_last_release_stops_it(tmp_path, monkeypatch):
    gateway = ProtonGateway(tmp_path)
    actions = []
    async def start():
        actions.append('start')
        gateway.proxy = {'password': 'private'}
    async def stop():
        actions.append('stop')
    monkeypatch.setattr(gateway, 'start', start)
    monkeypatch.setattr(gateway, 'stop', stop)
    async def scenario():
        async with gateway.connection():
            async with gateway.connection():
                assert gateway.users == 2 and actions == ['start']
            assert gateway.users == 1 and actions == ['start']
        assert gateway.users == 0 and actions == ['start', 'stop']
    asyncio.run(scenario())
    assert 'private' not in json.dumps(gateway.public())


def test_cancellation_releases_tunnel(tmp_path, monkeypatch):
    gateway = ProtonGateway(tmp_path)
    stopped = []
    async def start():
        gateway.proxy = {}
    async def stop():
        stopped.append(True)
    monkeypatch.setattr(gateway, 'start', start)
    monkeypatch.setattr(gateway, 'stop', stop)
    async def scenario():
        ready = asyncio.Event()
        async def job():
            async with gateway.connection():
                ready.set()
                await asyncio.sleep(30)
        task = asyncio.create_task(job())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway.users == 0 and stopped == [True]
    asyncio.run(scenario())


def test_cancel_during_start_cleans_up_without_acquiring_lease(tmp_path, monkeypatch):
    gateway = ProtonGateway(tmp_path)
    actions = []
    async def start():
        raise asyncio.CancelledError
    async def stop():
        actions.append('stop')
    monkeypatch.setattr(gateway, 'start', start)
    monkeypatch.setattr(gateway, 'stop', stop)
    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            async with gateway.connection():
                pytest.fail('must not run')
    asyncio.run(scenario())
    assert gateway.users == 0 and actions == ['stop']


class FakeGateway:
    configured = True
    entered = 0
    exited = 0
    @asynccontextmanager
    async def connection(self):
        self.entered += 1
        try:
            yield {'password': 'internal-secret'}
        finally:
            self.exited += 1


@pytest.mark.parametrize('code', ['geo_blocked', 'access_denied', 'network', 'timeout', 'source_parse', 'tls_failed'])
def test_retry_once_via_vpn_and_release(monkeypatch, code):
    gateway = FakeGateway()
    monkeypatch.setattr(main, 'gateway', gateway)
    calls, progress = [], []
    async def attempt(payload, *args, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            raise MediaError(code, 'Safe error')
        return {'event': 'result', 'size': 123}
    monkeypatch.setattr(main, '_worker_once', attempt)
    result = asyncio.run(main.worker({'mode': 'download'}, progress.append))
    assert result['route'] == 'proton'
    assert len(calls) == 2 and 'vpn_proxy' not in calls[0]
    assert calls[1]['vpn_proxy']['password'] == 'internal-secret'
    assert gateway.entered == gateway.exited == 1
    assert 'internal-secret' not in json.dumps(result) + json.dumps(progress)


@pytest.mark.parametrize('code', ['protected', 'authentication', 'bot_blocked', 'rate_limited', 'not_found',
                                 'size_limit', 'private_network', 'conversion_failed', 'unsupported'])
def test_ineligible_errors_never_start_vpn(monkeypatch, code):
    gateway = FakeGateway()
    monkeypatch.setattr(main, 'gateway', gateway)
    async def attempt(*args, **kwargs):
        raise MediaError(code, 'Safe error')
    monkeypatch.setattr(main, '_worker_once', attempt)
    with pytest.raises(MediaError) as caught:
        asyncio.run(main.worker({'mode': 'download'}))
    assert caught.value.code == code and gateway.entered == 0


def test_conversion_timeout_does_not_trigger_vpn(monkeypatch):
    gateway = FakeGateway()
    monkeypatch.setattr(main, 'gateway', gateway)
    async def attempt(*args, **kwargs):
        error = MediaError('timeout', 'Safe error')
        error.operation_stage = 'transcode'
        raise error
    monkeypatch.setattr(main, '_worker_once', attempt)
    with pytest.raises(MediaError):
        asyncio.run(main.worker({'mode': 'download'}))
    assert gateway.entered == 0


def test_failed_vpn_attempt_is_not_retried_directly(monkeypatch):
    gateway = FakeGateway()
    monkeypatch.setattr(main, 'gateway', gateway)
    calls = []
    async def attempt(payload, *args, **kwargs):
        calls.append(payload)
        raise MediaError('network', 'Safe error')
    monkeypatch.setattr(main, '_worker_once', attempt)
    with pytest.raises(MediaError) as caught:
        asyncio.run(main.worker({'mode': 'download'}))
    assert len(calls) == 2 and gateway.entered == gateway.exited == 1
    assert caught.value.diagnostic['route'] == 'proton' and caught.value.diagnostic['attempt'] == 2
    assert 'Proton VPN üzerinden' in caught.value.message
    assert calls[0]['request_id'] == calls[1]['request_id'] == caught.value.diagnostic['request_id']


def test_known_vpn_route_skips_direct_attempt(monkeypatch):
    gateway = FakeGateway()
    monkeypatch.setattr(main, 'gateway', gateway)
    async def attempt(payload, *args, **kwargs):
        assert payload['vpn_proxy'] and payload['route'] == 'proton'
        return {'event': 'result', 'size': 1}
    monkeypatch.setattr(main, '_worker_once', attempt)
    assert asyncio.run(main.worker({'mode': 'download', 'route': 'proton'}))['route'] == 'proton'
    assert gateway.entered == gateway.exited == 1


def test_unconfigured_vpn_preserves_original_error(monkeypatch):
    gateway = FakeGateway()
    gateway.configured = False
    monkeypatch.setattr(main, 'gateway', gateway)
    async def attempt(*args, **kwargs):
        raise MediaError('geo_blocked', 'Source is unavailable')
    monkeypatch.setattr(main, '_worker_once', attempt)
    with pytest.raises(MediaError) as caught:
        asyncio.run(main.worker({'mode': 'analyze'}))
    assert caught.value.code == 'geo_blocked' and gateway.entered == 0
    assert 'yapılandırılmadığı' in caught.value.message


def test_analysis_parser_failure_retries_during_first_step(monkeypatch):
    gateway = FakeGateway()
    monkeypatch.setattr(main, 'gateway', gateway)
    calls = []
    async def attempt(payload, *args, **kwargs):
        calls.append(payload)
        if len(calls) == 1:
            error = MediaError('source_parse', 'Unable to parse')
            error.operation_stage = 'extract'
            raise error
        return {'metadata': {'title': 'test', 'qualities': []}}
    monkeypatch.setattr(main, '_worker_once', attempt)
    result = asyncio.run(main.worker({'mode': 'analyze'}))
    assert result['route'] == 'proton'
    assert len(calls) == 2 and gateway.entered == gateway.exited == 1


@pytest.mark.parametrize('text,code', [
    ('Video not available in your country', 'geo_blocked'),
    ('HTTP Error 451', 'geo_blocked'),
    ("Sign in to confirm you're not a bot", 'authentication'),
    ('HTTP Error 403 CAPTCHA', 'bot_blocked'),
])
def test_access_error_classification(text, code):
    assert describe_error(Exception(text)).code == code


def test_gateway_starts_isolated_and_removes_only_its_container(tmp_path, monkeypatch):
    gateway = ProtonGateway(tmp_path)
    gateway.config.write_text(config_text())
    calls = []
    async def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'ps':
            assert args == ('ps', '-aq', '--filter', f'label={gateway.label}')
            return 'owned-container'
        if args[0] == 'inspect':
            return 'healthy'
        return ''
    monkeypatch.setattr(gateway, 'docker', docker)
    async def scenario():
        async with gateway.connection() as proxy:
            assert gateway.state == 'connected'
            envfile = tmp_path / 'proxy.env'
            assert envfile.stat().st_mode & 0o777 == 0o600
            assert proxy['password'] in envfile.read_text()
            assert proxy['password'] not in repr(calls)
            assert proxy['password'] not in json.dumps(gateway.public())
        assert gateway.state == 'idle' and gateway.proxy is None
        assert not (tmp_path / 'proxy.env').exists()
    asyncio.run(scenario())
    run = next(args for args in calls if args[0] == 'run')
    assert '--pull=never' in run and '--rm' in run and 'FIREWALL=on' in run
    assert run[run.index('--publish') + 1] == '127.0.0.1:18989:8888/tcp'
    assert run[run.index('--mount') + 1].endswith('wg0.conf,readonly')
    assert [args for args in calls if args[0] == 'rm'] == [('rm', '-f', 'owned-container')] * 2


def test_failed_start_removes_credentials_and_releases_lease(tmp_path, monkeypatch):
    gateway = ProtonGateway(tmp_path)
    gateway.config.write_text(config_text())
    async def docker(*args, **kwargs):
        if args[0] == 'run':
            raise MediaError('vpn_unavailable', 'Unavailable')
        return ''
    monkeypatch.setattr(gateway, 'docker', docker)
    async def scenario():
        with pytest.raises(MediaError):
            async with gateway.connection():
                pytest.fail('must not run')
        assert gateway.users == 0 and gateway.state == 'idle'
        assert gateway.proxy is None and not (tmp_path / 'proxy.env').exists()
    asyncio.run(scenario())


def test_cleanup_failure_is_visible_without_leaking_credentials(tmp_path, monkeypatch):
    gateway = ProtonGateway(tmp_path)
    gateway.proxy = {'password': 'private'}
    (tmp_path / 'proxy.env').write_text('private')
    async def remove():
        raise MediaError('vpn_unavailable', 'Unavailable')
    monkeypatch.setattr(gateway, 'remove_container', remove)
    asyncio.run(gateway.stop())
    assert gateway.state == 'error' and gateway.proxy is None
    assert not (tmp_path / 'proxy.env').exists()


def test_overall_deadline_releases_vpn(monkeypatch):
    gateway = FakeGateway()
    monkeypatch.setattr(main, 'gateway', gateway)
    async def attempt(payload, *args, **kwargs):
        if 'vpn_proxy' not in payload:
            raise MediaError('geo_blocked', 'Unavailable')
        await asyncio.sleep(30)
    monkeypatch.setattr(main, '_worker_once', attempt)
    with pytest.raises(TimeoutError):
        asyncio.run(main.worker({'mode': 'analyze'}, timeout=0.05))
    assert gateway.entered == gateway.exited == 1
