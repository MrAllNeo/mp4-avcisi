import asyncio
import base64
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator

from app.errors import MediaError, describe_error
from app import diagnostics
from app.jobstore import ACTIVE, TTL, Job, JobStore
from app.network import validate_url
from app.vpn import ProtonGateway, can_retry_via_vpn

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / '.data'
DEFAULT_MAX_DISK = 4600 * 1024 * 1024
try:
    MAX_DISK = max(1, int(os.environ.get('MP4_MAX_DISK_BYTES', DEFAULT_MAX_DISK)))
except (TypeError, ValueError):
    MAX_DISK = DEFAULT_MAX_DISK
MAX_PENDING = 10
MAX_JOBS = 20
slots = asyncio.Semaphore(2)
analysis_slots = asyncio.Semaphore(1)
jobs = {}
analyses = {}
tasks = {}
stop_reasons = {}
changing = set()
gateway = ProtonGateway(DATA / 'proton')


def save(job):
    try:
        JobStore(DATA).save(job)
    except OSError as exc:
        error = describe_error(exc)
        diagnostics.record('storage_failed', job_id=job.id, stage='storage', code=error.code, exc=exc)
        raise error from None


def finish(job, status, message, code=None, retryable=False):
    job.status, job.message = status, message
    job.code, job.retryable = code, retryable
    job.expires_at = time.time() + TTL
    try:
        save(job)
    except MediaError as error:
        # Keep an actionable in-memory status even when persistence is unavailable.
        job.status, job.message, job.code, job.retryable = 'error', error.message, error.code, error.retryable
    diagnostics.record('job_finished', job_id=job.id, status=job.status, code=job.code,
                       retryable=job.retryable, size=job.size, percent=job.percent)


def cleanup_expired(now=None):
    now = now if now is not None else time.time()
    for key, value in list(analyses.items()):
        if now - value['created'] > TTL:
            analyses.pop(key, None)
            directory = value.get('directory')
            if directory:
                shutil.rmtree(directory, ignore_errors=True)
    for key, job in list(jobs.items()):
        if job.status not in ACTIVE and job.expires_at is not None and job.expires_at <= now:
            JobStore(DATA).remove(key)
            jobs.pop(key, None)
            diagnostics.record('job_expired', job_id=key)


async def reap():
    while True:
        await asyncio.sleep(60)
        try:
            cleanup_expired()
        except OSError as exc:
            diagnostics.record('cleanup_failed', exc=exc)


@asynccontextmanager
async def lifespan(app):
    global slots, analysis_slots, gateway
    gateway = ProtonGateway(DATA / 'proton')
    diagnostics.configure(DATA / 'logs')
    slots, analysis_slots = asyncio.Semaphore(2), asyncio.Semaphore(1)
    jobs.clear()
    jobs.update(JobStore(DATA).load())
    # Analysis tokens live only in memory. A restarted server cannot safely
    # associate old private plans with a browser, so discard those remnants.
    shutil.rmtree(DATA / 'analyses', ignore_errors=True)
    analyses.clear()
    tasks.clear()
    stop_reasons.clear()
    changing.clear()
    diagnostics.record('server_started', count=len(jobs))
    cleaner = asyncio.create_task(reap())
    yield
    cleaner.cancel()
    pending = list(tasks.items())
    for key, task in pending:
        stop_reasons[key] = 'paused'
        task.cancel()
    await asyncio.gather(cleaner, *(task for _, task in pending), return_exceptions=True)
    # A task can be cancelled before its coroutine has entered its try/finally.
    for key, _ in pending:
        if jobs[key].status in ACTIVE:
            finish(jobs[key], 'paused', 'Sunucu kapatıldı. İndirmeye devam edebilirsin.', 'interrupted', True)
    if gateway.state not in {'idle'}:
        await gateway.stop()
    diagnostics.record('server_stopped')


app = FastAPI(title='TOYWES · MP4 Avcısı', lifespan=lifespan, docs_url=None, redoc_url=None)
allowed_hosts = [host.strip() for host in os.environ.get(
    'MP4_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]').split(',') if host.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)


@app.exception_handler(MediaError)
async def media_error_handler(request, error):
    body = {'detail': error.message, 'code': error.code, 'retryable': error.retryable}
    if getattr(error, 'diagnostic', None):
        body['diagnostic'] = diagnostics.safe_fields(error.diagnostic)
    return JSONResponse(body, status_code=422)


@app.middleware('http')
async def headers(request: Request, call_next):
    access_user = os.environ.get('MP4_ACCESS_USER')
    access_password = os.environ.get('MP4_ACCESS_PASSWORD')
    if access_user and access_password and request.url.path != '/api/health':
        supplied = request.headers.get('authorization', '')
        expected = 'Basic ' + base64.b64encode(f'{access_user}:{access_password}'.encode()).decode()
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse({'detail': 'Bu test yayını giriş istiyor.'}, status_code=401,
                                headers={'WWW-Authenticate': 'Basic realm="MP4 Avcisi"'})
    if request.method in {'POST', 'DELETE'}:
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Bu kaynaktan işlem yapılamıyor.'}, status_code=403)
        try:
            oversized = int(request.headers.get('content-length', '0')) > 8192
        except ValueError:
            return JSONResponse({'detail': 'Geçersiz istek.'}, status_code=400)
        if oversized:
            return JSONResponse({'detail': 'İstek çok büyük.'}, status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


class AnalyzeInput(BaseModel):
    url: str = Field(max_length=4096)

    @field_validator('url')
    @classmethod
    def valid_url(cls, value):
        return validate_url(value)


class DownloadInput(BaseModel):
    analysis_id: str
    height: int | None = Field(default=None, ge=1, le=16384)


async def worker(payload, on_event=None, timeout=90):
    payload = {**payload, 'request_id': uuid4().hex}
    try:
        return await _routed_worker(payload, on_event, timeout)
    except TimeoutError:
        diagnostics.record('worker_failed', job_id=payload.get('job_id'), mode=payload['mode'],
                           request_id=payload['request_id'], code='timeout', retryable=True)
        raise
    except Exception as exc:
        error = describe_error(exc)
        detail = getattr(error, 'diagnostic', {})
        error.diagnostic = diagnostics.safe_fields({**detail, 'request_id': payload['request_id'],
            'mode': payload['mode'], 'route': detail.get('route', payload.get('route', 'direct'))})
        if error.code == 'source_failed' and payload['mode'] == 'analyze':
            error.message = 'Sayfadan video bilgileri alınamadı. Yanıtın neden çözümlenemediği henüz belirlenemedi.'
        if error.code == 'access_denied':
            resource = {'source_page': 'sayfa', 'embedded_page': 'gömülü sayfa',
                        'manifest': 'akış listesi', 'metadata': 'video bilgisi', 'media': 'medya dosyası'}.get(detail.get('resource'), 'kaynak')
            error.message = f'Kaynak site {resource} isteğini reddetti (HTTP 403).'
            if not gateway.configured and error.diagnostic['route'] == 'direct':
                error.message += ' Otomatik Proton VPN yapılandırılmadığı için alternatif bağlantı denenemedi.'
        if error.diagnostic['route'] == 'proton' and error.code not in {'vpn_config', 'vpn_unavailable'}:
            error.message += ' Bu hata Proton VPN üzerinden yapılan denemede oluştu.'
        diagnostics.record('routing_failed', **error.diagnostic, code=error.code)
        raise error from None


async def _routed_worker(payload, on_event=None, timeout=90):
    # A single deadline covers the direct attempt, gateway startup and VPN retry.
    # Credentials are internal-only and reach the child via stdin, never argv.
    async with asyncio.timeout(timeout):
        if payload.get('route') != 'proton':
            try:
                direct_budget = min(timeout, 35) if payload['mode'] == 'analyze' and gateway.configured else timeout
                result = await _worker_once(payload, on_event, timeout=direct_budget)
                return {**result, 'route': 'direct'}
            except Exception as exc:
                error = describe_error(exc)
                error.operation_stage = getattr(exc, 'operation_stage', 'download')
                if not can_retry_via_vpn(error):
                    raise
                if not gateway.configured:
                    diagnostics.record('vpn_unconfigured', job_id=payload.get('job_id'), request_id=payload.get('request_id'), code=error.code)
                    error.message += ' Otomatik Proton VPN yapılandırılmadığı için alternatif bağlantı denenemedi.'
                    raise error from None
                diagnostics.record('vpn_fallback', job_id=payload.get('job_id'), request_id=payload.get('request_id'), code=error.code, route='proton')
        elif not gateway.configured:
            raise MediaError('vpn_config', 'Bu işlem Proton VPN gerektiriyor. Sunucunun VPN ayarlarını kontrol et.', True)
        if on_event:
            on_event({'event': 'progress', 'message': 'Proton VPN bağlantısı hazırlanıyor…', 'percent': None, 'route': 'proton'})
        try:
            async with gateway.connection() as proxy:
                result = await _worker_once({**payload, 'vpn_proxy': proxy, 'route': 'proton'}, on_event, timeout=timeout)
                return {**result, 'route': 'proton'}
        except Exception as exc:
            error = describe_error(exc)
            error.diagnostic = {**getattr(error, 'diagnostic', {}), 'route': 'proton',
                                'attempt': 1 if payload.get('route') == 'proton' else 2}
            raise error from None


async def _worker_once(payload, on_event=None, timeout=90):
    context = {'operation_id': uuid4().hex, 'job_id': payload.get('job_id'),
               'request_id': payload.get('request_id'),
               'mode': payload['mode'], 'stage': 'startup', 'route': payload.get('route', 'direct')}
    started = time.monotonic()
    diagnostics.record('worker_started', **context, timeout_seconds=timeout, height=payload.get('height'))
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'app.worker', cwd=ROOT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        diagnostics.record('worker_failed', **context, exc=exc, code='worker_start_failed')
        raise MediaError('worker_start_failed', 'İndirme işlemi başlatılamadı. Yeniden dene.', True) from None

    async def drain_stderr():
        # Drain continuously to avoid a full pipe deadlocking the subprocess.
        # Keep only a bounded tail in memory for classification, never raw logs.
        count, tail = 0, b''
        while chunk := await process.stderr.read(8192):
            count += len(chunk)
            tail = (tail + chunk)[-16384:]
        if count:
            code = describe_error(Exception(tail.decode('utf-8', errors='replace'))).code
            diagnostics.record('worker_stderr', **context, stderr_bytes=count, code=code)

    def protocol_error():
        diagnostics.record('worker_protocol_error', **context)
        return MediaError('worker_protocol', 'İndirme işlemi geçersiz yanıt verdi. Yeniden dene.', True)

    async def consume():
        process.stdin.write((json.dumps(payload) + '\n').encode())
        await process.stdin.drain()
        process.stdin.close()
        result = None
        last_request_failure = {}
        while True:
            try:
                line = await process.stdout.readline()
                if not line:
                    break
                event = json.loads(line)
                if (not isinstance(event, dict) or not isinstance(event.get('event'), str)
                        or event['event'] not in {'error', 'result', 'progress', 'diagnostic'}):
                    raise ValueError
            except (ValueError, UnicodeError):
                raise protocol_error() from None
            if event['event'] == 'diagnostic':
                fields = event.get('fields')
                if (not isinstance(fields, dict) or not isinstance(event.get('name'), str)
                        or event['name'] not in diagnostics.EVENTS):
                    raise protocol_error()
                fields = diagnostics.safe_fields(fields)
                if event['name'] == 'request_failed':
                    last_request_failure = {key: fields[key] for key in ('resource', 'method', 'http_status', 'request_number') if key in fields}
                elif event['name'] in {'request_finished', 'stage_started'}:
                    last_request_failure = {}
                if 'stage' in fields:
                    context['stage'] = fields['stage']
                diagnostics.record(event.get('name'), **(fields | context))
                continue
            if event['event'] == 'error':
                if (not isinstance(event.get('message'), str) or not isinstance(event.get('code'), str)
                        or not isinstance(event.get('retryable', False), bool)):
                    raise protocol_error()
                error = MediaError(event.get('code', 'source_failed'), event['message'], event.get('retryable', False))
                error.diagnostic = {**last_request_failure, 'stage': context['stage'], 'route': context['route']}
                raise error
            if event['event'] == 'result':
                metadata = event.get('metadata')
                valid = ((isinstance(metadata, dict) and isinstance(metadata.get('title'), str)
                          and isinstance(metadata.get('qualities'), list)) if payload['mode'] == 'analyze'
                         else type(event.get('size')) is int and event['size'] > 0)
                if not valid or result is not None:
                    raise protocol_error()
                result = event
            if event['event'] == 'progress':
                percent = event.get('percent')
                if (not isinstance(event.get('message'), str)
                        or (percent is not None and (type(percent) is not int or not 0 <= percent <= 100))):
                    raise protocol_error()
            if on_event:
                on_event(event)
        await process.wait()
        if process.returncode or result is None:
            diagnostics.record('worker_failed', **context, returncode=process.returncode, code='worker_stopped')
            raise MediaError('worker_stopped', 'İşlem kesildi. Yeniden deneyebilirsin.', True)
        return result

    async def disk_watch():
        while True:
            await asyncio.sleep(1)
            if payload.get('directory'):
                size = 0
                for path in Path(payload['directory']).glob('*'):
                    try:
                        if path.is_file():
                            size += path.stat().st_size
                    except FileNotFoundError:
                        pass  # A completed merge may remove its input between scans.
                if size > MAX_DISK:
                    diagnostics.record('size_limit', **context, basis='temporary_files', size=size, limit_bytes=MAX_DISK)
                    raise MediaError('size_limit', 'İşlem dosya boyutu sınırını aştı. Daha küçük bir video seç.')

    consumer = asyncio.create_task(consume())
    watcher = asyncio.create_task(disk_watch())
    stderr_reader = asyncio.create_task(drain_stderr())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait([consumer, watcher], return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await watcher
            result = await consumer
            diagnostics.record('worker_finished', **context, returncode=process.returncode,
                               elapsed_ms=round((time.monotonic() - started) * 1000))
            return result
    except asyncio.CancelledError:
        diagnostics.record('worker_cancelled', **context)
        raise
    except Exception as exc:
        exc.operation_stage = context['stage']
        error = describe_error(exc)
        diagnostics.record('worker_failed', **context, code=error.code, retryable=error.retryable,
                           elapsed_ms=round((time.monotonic() - started) * 1000), exc=exc)
        raise
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        consumer.cancel()
        watcher.cancel()
        await asyncio.gather(consumer, watcher, return_exceptions=True)
        process.stdin.close()
        # A malformed oversized stdout line can pause its pipe transport. Drain
        # the remainder after killing the group, otherwise wait() can deadlock.
        while await process.stdout.read(8192):
            pass
        await process.wait()
        await stderr_reader


async def download(job):
    directory = DATA / job.id
    last_saved = 0
    try:
        async with slots:
            directory.mkdir(mode=0o700, exist_ok=True)
            job.status, job.message = 'processing', 'Kaynak hazırlanıyor…'
            save(job)
            diagnostics.record('job_started', job_id=job.id, height=job.height)

            def event(data):
                nonlocal last_saved
                if data['event'] == 'progress':
                    job.message = data['message']
                    job.percent = data.get('percent')
                    job.route = data.get('route', job.route)
                    if time.monotonic() - last_saved >= 2:
                        save(job)
                        last_saved = time.monotonic()

            result = await worker({'mode': 'download', 'url': job.url, 'height': job.height,
                                   'directory': str(directory), 'job_id': job.id, 'route': job.route}, event, timeout=900)
            job.route = result.get('route', job.route)
            job.percent, job.size = 100, result['size']
            finish(job, 'complete', 'MP4 dosyan hazır.')
    except asyncio.CancelledError:
        reason = stop_reasons.get(job.id, 'paused')
        finish(job, reason, 'Duraklatıldı. Kısmi dosyalar saklandı.' if reason == 'paused' else 'İşlem iptal edildi.',
               'interrupted' if reason == 'paused' else None, reason == 'paused')
        raise
    except Exception as exc:
        error = describe_error(exc)
        diagnostics.record('job_failed', job_id=job.id, code=error.code, retryable=error.retryable, exc=exc)
        finish(job, 'error', error.message, error.code, error.retryable)
    finally:
        if job.status == 'cancelled' or (job.status == 'error' and not job.retryable):
            shutil.rmtree(directory, ignore_errors=True)
        elif job.status == 'complete':
            try:
                for path in directory.iterdir():
                    if path.is_file() and path.name != 'video.mp4':
                        path.unlink(missing_ok=True)
            except OSError as exc:
                diagnostics.record('cleanup_failed', job_id=job.id, exc=exc)


def start_job(job):
    job.status, job.message, job.percent = 'queued', 'Sıraya alındı…', None
    job.code, job.retryable, job.expires_at = None, False, None
    job.queued_at = time.time()
    try:
        save(job)
    except MediaError as error:
        finish(job, 'error', error.message, error.code, error.retryable)
        raise
    diagnostics.record('job_queued', job_id=job.id, height=job.height)
    task = asyncio.create_task(download(job))
    tasks[job.id] = task
    def completed(done):
        if tasks.get(job.id) is done:
            tasks.pop(job.id, None)
        if not done.cancelled() and (exc := done.exception()) is not None:
            diagnostics.record('job_task_failed', job_id=job.id, exc=exc)
    task.add_done_callback(completed)


def ensure_queue_space():
    if sum(j.status in ACTIVE for j in jobs.values()) >= MAX_PENDING:
        raise HTTPException(429, 'Kuyruk dolu. Bir işlem tamamlandığında yeniden dene.')


def public_job(job):
    result = job.public()
    queued = [j.id for j in sorted(jobs.values(), key=lambda j: j.queued_at) if j.status == 'queued']
    result['queue_position'] = queued.index(job.id) + 1 if job.id in queued else None
    return result


@app.post('/api/analyze')
async def analyze(body: AnalyzeInput):
    cleanup_expired()
    if analysis_slots.locked():
        raise HTTPException(429, 'Başka bir bağlantı analiz ediliyor. Biraz sonra yeniden dene.')
    if len(analyses) >= 100:
        raise HTTPException(429, 'Analiz sınırına ulaşıldı. Biraz sonra yeniden dene.')
    key = uuid4().hex
    analysis_directory = DATA / 'analyses' / key
    analysis_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    analysis_directory.chmod(0o700)
    try:
        async with analysis_slots:
            result = await worker({'mode': 'analyze', 'url': body.url,
                                   'directory': str(analysis_directory)})
    except TimeoutError:
        shutil.rmtree(analysis_directory, ignore_errors=True)
        raise HTTPException(504, 'Kaynak zamanında yanıt vermedi. Tekrar dene.') from None
    except Exception as exc:
        shutil.rmtree(analysis_directory, ignore_errors=True)
        error = describe_error(exc)
        if error.code == 'source_failed' and not getattr(error, 'diagnostic', None):
            error.message = 'Sayfadan video bilgileri alınamadı. Kaynak çözümleme hatasının nedeni henüz belirlenemedi.'
            if not gateway.configured:
                error.message += ' Otomatik Proton VPN de henüz yapılandırılmamış.'
        raise error from None
    route = result.get('route', 'direct')
    analyses[key] = {'url': body.url, 'metadata': result['metadata'], 'created': time.time(),
                     'route': route, 'directory': str(analysis_directory)}
    return {'id': key, **result['metadata'], 'route': route}


@app.post('/api/downloads', status_code=202)
async def create_download(body: DownloadInput):
    cleanup_expired()
    entry = analyses.get(body.analysis_id)
    if not entry or time.time() - entry['created'] > TTL:
        raise HTTPException(404, 'Analizin süresi doldu. Bağlantıyı yeniden analiz et.')
    if body.height and body.height not in entry['metadata']['qualities']:
        raise HTTPException(422, 'Bu kalite kaynakta bulunamadı.')
    for job in jobs.values():
        if job.url == entry['url'] and job.height == body.height and job.status in ACTIVE:
            return public_job(job)
    ensure_queue_space()
    if len(jobs) >= MAX_JOBS:
        raise HTTPException(429, 'İşlem listesi dolu. Eski işlemlerden birini silip yeniden dene.')
    job = Job(uuid4().hex, entry['url'], entry['metadata']['title'], body.height)
    job.route = entry.get('route', 'direct')
    analysis_directory = Path(entry['directory']) if entry.get('directory') else None
    if analysis_directory:
        job_directory = DATA / job.id
        try:
            job_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            job_directory.chmod(0o700)
            for name in ('analysis.json', 'session.cookies'):
                source = analysis_directory / name
                if source.is_file() and not source.is_symlink():
                    destination = job_directory / name
                    shutil.copyfile(source, destination)
                    destination.chmod(0o600)
        except OSError as exc:
            shutil.rmtree(job_directory, ignore_errors=True)
            raise describe_error(exc) from None
    jobs[job.id] = job
    start_job(job)
    return public_job(job)


def get_job(key):
    cleanup_expired()
    if key not in jobs:
        raise HTTPException(404, 'İndirme bulunamadı veya süresi doldu.')
    return jobs[key]


@app.get('/api/downloads')
async def list_downloads():
    cleanup_expired()
    return {'jobs': [public_job(j) for j in reversed(list(jobs.values()))], 'parallel_limit': 2, 'queue_limit': MAX_PENDING}


@app.get('/api/downloads/{key}')
async def download_status(key: str):
    return public_job(get_job(key))


async def stop_job(job, reason):
    if job.status in ACTIVE:
        stop_reasons[job.id] = reason
        task = tasks.get(job.id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if job.status in ACTIVE:
            finish(job, reason, 'Duraklatıldı.' if reason == 'paused' else 'İşlem iptal edildi.',
                   'interrupted' if reason == 'paused' else None, reason == 'paused')
        stop_reasons.pop(job.id, None)
    elif reason == 'cancelled' and job.status in {'paused', 'error'}:
        finish(job, 'cancelled', 'İşlem iptal edildi.')
    if job.status == 'cancelled':
        shutil.rmtree(DATA / job.id, ignore_errors=True)


@asynccontextmanager
async def mutation(key):
    if key in changing:
        raise HTTPException(409, 'İşlem güncelleniyor. Biraz sonra yeniden dene.')
    job = get_job(key)
    changing.add(key)
    try:
        yield job
    finally:
        changing.discard(key)


@app.post('/api/downloads/{key}/pause')
async def pause_download(key: str):
    async with mutation(key) as job:
        if job.status not in ACTIVE | {'paused'}:
            raise HTTPException(409, 'Bu işlem artık duraklatılamıyor.')
        await stop_job(job, 'paused')
        return public_job(job)


@app.post('/api/downloads/{key}/resume', status_code=202)
async def resume_download(key: str):
    async with mutation(key) as job:
        if job.status not in {'paused', 'error'} or not job.retryable:
            raise HTTPException(409, 'Bu işleme devam edilemiyor. Bağlantıyı yeniden analiz et.')
        ensure_queue_space()
        start_job(job)
        return public_job(job)


@app.delete('/api/downloads/{key}')
async def cancel_download(key: str):
    async with mutation(key) as job:
        await stop_job(job, 'cancelled')
        return public_job(job)


@app.delete('/api/downloads/{key}/purge', status_code=204)
async def purge_download(key: str):
    async with mutation(key) as job:
        await stop_job(job, 'cancelled')
        JobStore(DATA).remove(key)
        jobs.pop(key, None)


@app.get('/api/downloads/{key}/file')
async def download_file(key: str):
    job = get_job(key)
    if job.status != 'complete':
        raise HTTPException(409, 'Dosya henüz hazır değil.')
    path = DATA / job.id / 'video.mp4'
    if not path.is_file():
        raise HTTPException(410, 'Dosya bulunamadı. Bağlantıyı yeniden analiz et.')
    name = re.sub(r'[^\w\s.-]', '', job.title, flags=re.UNICODE).strip()[:100] or 'video'
    return FileResponse(path, filename=f'{name}.mp4', media_type='video/mp4')


@app.get('/api/health')
async def health():
    return {'status': 'ok'}


@app.get('/api/network')
async def network_status():
    return gateway.public()


app.mount('/', StaticFiles(directory=ROOT / 'static', html=True), name='static')
