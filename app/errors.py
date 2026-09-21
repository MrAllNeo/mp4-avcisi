"""Public error messages never include a remote URL, token or response body."""
from dataclasses import dataclass
import errno
import json
import re
import ssl
import subprocess


@dataclass
class MediaError(ValueError):
    code: str
    message: str
    retryable: bool = False

    def __str__(self):
        return self.message

    def public(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def error_chain(error):
    """Include yt-dlp's saved causes without retaining text in diagnostics."""
    pending, seen = [error], set()
    while pending and len(seen) < 8:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        saved = getattr(current, 'exc_info', None)
        pending.extend([getattr(current, 'cause', None), current.__cause__,
                        saved[1] if isinstance(saved, tuple) and len(saved) > 1 else None])
        if not current.__suppress_context__:
            pending.append(current.__context__)


def http_status(error):
    for key in ('status', 'code'):
        value = getattr(error, key, None)
        if type(value) is int and 400 <= value <= 599:
            return value
    match = re.search(r'\bHTTP(?:\s+Error)?\s*:?\s*([45]\d\d)\b', str(error), re.I)
    return int(match[1]) if match else None


def describe_error(error):
    if isinstance(error, MediaError):
        return error
    classified = [_describe_single_error(item) for item in error_chain(error)]
    # Access controls and private-network errors must not be mistaken for a
    # retryable parser/transport failure in an outer library exception.
    protected = {'private_network', 'protected', 'authentication', 'bot_blocked', 'rate_limited'}
    for result in classified:
        if result.code in protected:
            return result
    for result in classified:
        if result.code not in {'source_failed', 'source_parse'}:
            return result
    return next((result for result in classified if result.code == 'source_parse'), classified[0])


def _describe_single_error(error):
    if isinstance(error, MediaError):
        return error
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) or type(error).__name__.lower() == 'timeout':
        return MediaError("timeout", "Kaynak zamanında yanıt vermedi. Biraz sonra yeniden dene.", True)
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return MediaError("disk_full", "Diskte yeterli yer yok. Yer açtıktan sonra yeniden dene.", True)
    if isinstance(error, PermissionError):
        return MediaError("storage_permission", "İşlem dosyalarına erişilemiyor. Uygulamanın dosya izinlerini kontrol et.", True)
    if isinstance(error, subprocess.CalledProcessError):
        return MediaError("conversion_failed", "Video MP4 biçimine dönüştürülemedi. Başka bir kalite dene.")
    text = str(error).lower()
    status = http_status(error)
    if status:
        text += f' http error {status}'
    if "yerel ve özel ağ" in text:
        return MediaError("private_network", "Yerel ağ bağlantıları desteklenmiyor. Herkese açık bir video bağlantısı gir.")
    if "drm" in text:
        return MediaError("protected", "Bu video korumalı olduğu için indirilemiyor.")
    if "vpn bağlantısı" in text:
        return MediaError("vpn_unavailable", "Proton VPN bağlantısı kurulamadı. Daha sonra yeniden dene.", True)
    if any(part in text for part in ("captcha", "confirm you're not a bot", "bot detection", "anti-bot")):
        return MediaError("bot_blocked", "Kaynak doğrulama istiyor; otomatik indirmeye izin vermiyor.")
    if "redirection detected; the video may be deleted or require login" in text:
        return MediaError(
            "access_denied",
            "Kaynak video yerine başka bir sayfaya yönlendirdi. Video kaldırılmış veya bölge/yaş doğrulama engeline takılmış olabilir.",
        )
    if any(part in text for part in ("sign in", "log in", "login", "authentication", "http error 401")):
        return MediaError("authentication", "Bu kaynak giriş istiyor. Herkese açık bir bağlantı kullan.")
    if any(part in text for part in ("not available in your country", "not available from your location", "geo-restricted", "geo restricted", "http error 451")):
        return MediaError("geo_blocked", "Kaynak bu bölgeden erişime izin vermiyor.")
    if any(part in text for part in ("http error 403", "forbidden")):
        return MediaError("access_denied", "Kaynak erişime izin vermedi. Bağlantının tarayıcında açıldığını kontrol et.")
    if any(part in text for part in ("http error 404", "http error 410", "not found", "removed")):
        return MediaError("not_found", "Video bulunamadı veya kaldırılmış. Güncel bağlantıyı kontrol et.")
    if any(part in text for part in ("http error 429", "too many requests")):
        return MediaError("rate_limited", "Kaynak çok fazla istek aldığı için bekletiyor. Biraz sonra yeniden dene.", True)
    if isinstance(error, ssl.SSLError) or any(part in text for part in ('certificate verify failed', 'ssl: certificate', 'tls handshake')):
        return MediaError('tls_failed', 'Kaynağa güvenli bağlantı kurulamadı. TLS veya sertifika doğrulaması başarısız.', True)
    if any(part in text for part in ("unsupported url", "no video", "no formats", "requested format is not available")):
        return MediaError("unsupported", "Bu bağlantıda desteklenen video bulunamadı. Doğrudan video veya başka bir kalite dene.")
    if any(part in text for part in ("timed out", "timeout", "connection", "resolve", "network", "http error 5", "dns", "name or service not known", "name resolution")):
        return MediaError("network", "Kaynağa bağlantı kesildi. Kısmi dosyalar saklandı; yeniden deneyebilirsin.", True)
    if isinstance(error, json.JSONDecodeError) or any(part in text for part in ('unable to extract', 'failed to extract', 'failed to parse', 'invalid json', 'jsondecodeerror', 'unexpected end of data')):
        return MediaError('source_parse', 'Sayfa yanıtından video bilgileri çözümlenemedi. Site değişmiş veya farklı bir erişim sayfası döndürmüş olabilir.', True)
    # ValueError can also originate in third-party parsers and include secrets.
    if isinstance(error, ValueError) and str(error) in {
        "Video bulunamadı.", "Geçerli bir http:// veya https:// video bağlantısı gir.",
        "FFmpeg bulunamadı. FFmpeg'i kur veya FFMPEG_BINARY ile tam yolunu belirt.",
    }:
        return MediaError("validation", str(error))
    return MediaError("source_failed", "Video hazırlanamadı. Bağlantıyı kontrol edip yeniden deneyebilirsin.", True)
