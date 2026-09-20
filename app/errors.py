"""Public error messages never include a remote URL, token or response body."""
from dataclasses import dataclass
import errno
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


def describe_error(error):
    if isinstance(error, MediaError):
        return error
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return MediaError("timeout", "Kaynak zamanında yanıt vermedi. Biraz sonra yeniden dene.", True)
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return MediaError("disk_full", "Diskte yeterli yer yok. Yer açtıktan sonra yeniden dene.", True)
    if isinstance(error, PermissionError):
        return MediaError("storage_permission", "İşlem dosyalarına erişilemiyor. Uygulamanın dosya izinlerini kontrol et.", True)
    if isinstance(error, subprocess.CalledProcessError):
        return MediaError("conversion_failed", "Video MP4 biçimine dönüştürülemedi. Başka bir kalite dene.")
    text = str(error).lower()
    if "yerel ve özel ağ" in text:
        return MediaError("private_network", "Yerel ağ bağlantıları desteklenmiyor. Herkese açık bir video bağlantısı gir.")
    if "drm" in text:
        return MediaError("protected", "Bu video korumalı olduğu için indirilemiyor.")
    if any(part in text for part in ("sign in", "log in", "login", "authentication", "http error 401")):
        return MediaError("authentication", "Bu kaynak giriş istiyor. Herkese açık bir bağlantı kullan.")
    if any(part in text for part in ("http error 403", "forbidden", "captcha", "bot")):
        return MediaError("access_denied", "Kaynak erişime izin vermedi. Bağlantının tarayıcında açıldığını kontrol et.")
    if any(part in text for part in ("http error 404", "http error 410", "not found", "removed")):
        return MediaError("not_found", "Video bulunamadı veya kaldırılmış. Güncel bağlantıyı kontrol et.")
    if any(part in text for part in ("http error 429", "too many requests")):
        return MediaError("rate_limited", "Kaynak çok fazla istek aldığı için bekletiyor. Biraz sonra yeniden dene.", True)
    if any(part in text for part in ("unsupported url", "no video", "no formats", "requested format is not available")):
        return MediaError("unsupported", "Bu bağlantıda desteklenen video bulunamadı. Doğrudan video veya başka bir kalite dene.")
    if any(part in text for part in ("timed out", "timeout", "connection", "resolve", "network", "http error 5")):
        return MediaError("network", "Kaynağa bağlantı kesildi. Kısmi dosyalar saklandı; yeniden deneyebilirsin.", True)
    # ValueError can also originate in third-party parsers and include secrets.
    if isinstance(error, ValueError) and str(error) in {
        "Video bulunamadı.", "Geçerli bir http:// veya https:// video bağlantısı gir.",
        "FFmpeg bulunamadı. FFmpeg'i kur veya FFMPEG_BINARY ile tam yolunu belirt.",
    }:
        return MediaError("validation", str(error))
    return MediaError("source_failed", "Video hazırlanamadı. Bağlantıyı kontrol edip yeniden deneyebilirsin.", True)
