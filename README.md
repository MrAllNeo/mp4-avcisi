# TOYWES · MP4 Avcısı

TOYWES (Toplum Yararına Web Siteleri) için video bağlantısını analiz eden, desteklenen herkese açık sayfalardaki gömülü kaynakları bulan ve MP4 dosyası hazırlayan yerel web uygulaması.

## Çalıştırma

Python 3.11+ ve FFmpeg gerekir. Linux/macOS üzerinde:

```bash
# Debian / Ubuntu / Pardus için FFmpeg (macOS: brew install ffmpeg)
sudo apt install ffmpeg
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Tarayıcıda **http://127.0.0.1:8000** adresini aç. “Örnekle dene” düğmesi MDN'nin CC0 çiçek videosuyla gerçek analiz ve indirme akışını başlatır; sahte sonuç kullanılmaz. Çalışma sırasında üçüncü taraf ücretli servis veya API anahtarı gerekmez.

FFmpeg sistem PATH'inden, `FFMPEG_BINARY` ortam değişkenindeki tam yoldan veya proje içindeki `.tools/bin/ffmpeg` dosyasından bulunur. Mevcut çalışma alanında Pardus deposunun FFmpeg paketi `.tools/` altına çıkarılmıştır; sistem kurulumu yapılmamıştır. Başka bir makineye taşırken FFmpeg'i ayrıca kur. Bu ilk sürüm POSIX işlem grupları kullandığı için Windows'ta WSL kullan.

## Çalışan akış

1. FastAPI, bağlantıyı ayrı ve zaman sınırlı bir Python sürecinde yt-dlp ile analiz eder.
2. Başlık, varsa süre, yaklaşık boyut ve mevcut çözünürlükler döner. Her kaynak bu bilgileri sunmaz.
3. Seçilen çözünürlüğü aşmayan en iyi kaynak indirilir. `En iyi kalite` üst sınır koymaz.
4. Ayrı görüntü/ses varsa FFmpeg birleştirir. MP4 kapsayıcısına kayıpsız aktarım denenir; gerekirse H.264/AAC dönüşümü yapılır.
5. Kullanıcı videoyu kuyruğa ekler. Aynı anda iki indirme çalışır, diğerleri FIFO sırasıyla başlar. İndirme sürerken başka bir bağlantı analiz edilebilir.
6. “İndirmelerim” bölümü her işin durumunu, bekleme sırasını ve dosya temizlenme saatini gösterir. Duraklat, devam et, iptal et, indir ve sil işlemleri ayrı ayrı yapılabilir.
7. Sayfa kapansa veya sunucu yeniden başlatılsa da kayıtlar kalır. Yeniden başlatmada yarım kalan işler duraklatılmış olarak açılır; kullanıcı “Devam et” ile sürdürür.

### Devam etme ve hata davranışı

- Duraklatma worker ve FFmpeg işlem grubunu durdurur; `.part` ve segment kayıtları korunur. yt-dlp, kaynak destekliyorsa HTTP Range veya segment bilgisiyle kaldığı yerden devam eder. Kaynak desteklemiyorsa ilgili dosya/parça yeniden indirilir. MP4 dönüştürme aşaması yeniden başlar.
- Ağ kesintisi ve zaman aşımı gibi geçici hatalarda kısmi dosyalar saklanır ve “Yeniden dene” sunulur. Desteklenmeyen kaynak, erişim engeli veya koruma durumlarında neden açıkça gösterilir; kaynak yanıtları, imzalı URL'ler ve tokenlar kullanıcıya aktarılmaz.
- Boyut aşımı `size_limit` koduyla ve daha düşük kalite seçme önerisiyle gösterilir. Kesin kaynak boyutu ve indirilen gerçek baytlar kontrol edilir; HLS gibi akışların değişken boyut tahmini tek başına indirmeyi durdurmaz. yt-dlp'nin dosyayı sessizce atlayan boyut filtresi yerine bu açık hata kullanılır.
- Tarayıcı sunucuya ulaşamazsa liste ekranda kalır ve en fazla 30 saniyeye çıkan aralıklarla yeniden sorgulanır. Sayfayı yenilemek gerekmez.
- “İptal et” kısmi dosyaları kaldırır. “Sil” hem iş kaydını hem dosyalarını siler. Cihaza daha önce kaydettiğin dosya etkilenmez.

Doğrudan MP4, yt-dlp'nin desteklediği site/oynatıcılar ve yerel indiriciyle çözülebilen korumasız HLS/DASH akışları hedeflenir. Bir sayfada birden fazla video varsa ilk video kullanılır. Site desteği, o sitenin güncel davranışına bağlıdır. yt-dlp için [resmî belgeler](https://github.com/yt-dlp/yt-dlp) ve [desteklenen siteler](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

## Sınırlar ve dağıtım

- Bu sürüm tek kullanıcı için **localhost** üzerinde çalışır; internete açık bir hizmet olarak yayımlanmamıştır. Host kontrolü localhost ile sınırlıdır.
- DRM, giriş isteyen kaynaklar, canlı yayınlar ve koruma aşma desteklenmez. JavaScript çalıştırarak ağ trafiği yakalama henüz eklenmemiştir.
- Dosya başına 500 MB kaynak sınırı, 2 saat video süresi, eşzamanlı iki indirme ve kuyruk beklemesi hariç en fazla 15 dakika hazırlama süresi vardır. Çalışanlar dahil en fazla 10 iş sıraya alınır; toplam 20 kayıt saklanır. Analiz için ayrı bir işlem yuvası bulunur. Geçici işlem dosyalarına ayrıca disk sınırı uygulanır. Birleştirme/dönüşüm sonucunun boyutu kaynak boyutundan farklı olabilir.
- Bitmiş, duraklatılmış, iptal edilmiş ve başarısız işler **son durum değişiminden bir saat sonra** temizlenir. Çalışan işin dosyası süre doldu diye silinmez. Temizleme her 60 saniyede ve API erişimlerinde yapılır.
- İş kayıtları `.data/<id>.json` dosyalarında atomik olarak saklanır. Kaynak URL'si devam edebilmek için kayda yazılır; bu dosyalar yalnız işletim sistemindeki kullanıcı tarafından okunabilir (0600), `.data/` izinleri 0700'dür. API listesi kaynak URL'sini döndürmez. Analiz sonuçları bellektedir; sunucu yeniden başlatılınca yeni indirmeler için tekrar analiz gerekir.
- Bu sürüm **tek Uvicorn worker** içindir. Yerel uygulamayı açan tarayıcılar aynı iş listesini görür; çok kullanıcılı hesap/oturum ayrımı yoktur.
- Kaynak bağlantılarında yalnız HTTP/HTTPS ve 80/443 portlarına izin verilir. Worker'ın gerçek soket bağlantılarında genel IP kontrolü yapılır; yönlendirmeler, gömülü kaynaklar, segmentler ve DNS yanıtları da aynı kontrolden geçer. Sistem proxy'si kullanılmaz. FFmpeg uzak kaynak indiricisi devre dışıdır; son dönüşüm yalnız yerel dosyaları açar.
- İnternete açılacak sürüm için kullanıcı bazlı kota, çok süreçli iş koordinasyonu, egress firewall ile worker izolasyonu, toplam depolama bütçesi ve kimlik doğrulama eklenmelidir. Bu sürüm o dağıtım modelini kapsamıyor.
- Yalnızca sana ait veya indirme iznin olan içerikleri kullan.

## Geliştirme ve test

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

`app/network.py` ağ sınırını, `app/worker.py` kaynak çözümleme ve MP4 hazırlamayı, `app/main.py` API/kuyruk yönetimini, `app/jobstore.py` kalıcı iş kayıtlarını, `app/errors.py` güvenli hata mesajlarını ve `app/diagnostics.py` yapılandırılmış logları içerir. `static/` bağımsız Türkçe arayüzdür; frontend derleme adımı yoktur.

Otomatik testler ağ sınırını, API davranışlarını, kuyruk kapasitesini, duraklat/devam et, yeniden başlatma, silme ve süre sonu temizliğini kapsar. Gerçek yt-dlp + FFmpeg ile küçük yerel MP4, gömülü HTML, HLS ve ayrı ses/görüntülü DASH dosyaları indirilip çözümlenir. HTTP Range testi, kısmi bir indirmede yalnız kalan baytların istendiğini doğrular. Bu testlerde loopback fixture'ına erişmek için ağ koruması yalnız test kapsamında devre dışı bırakılır.

Ek hata testleri kesin/tahmini boyut ayrımını, sınıra eşit boyutu, MP4 ve HLS'de gerçek boyut reddini, FFmpeg dönüşüm yedeğini, disk dolmasını, kayıt yazma hatasını, bozuk worker yanıtlarını, beklenmedik süreç kapanmasını, zaman aşımını ve iptal sonrası sürecin sonlanmasını kapsar. Büyük stderr çıktısının boruyu kilitlemediği, logların döndürüldüğü ve gizli bilgilerin loga/API'ye sızmadığı da doğrulanır. Bu senaryolar harici siteye bağlanmaz.

## İndirme hatalarını inceleme

Uygulama logu **`.data/logs/events.jsonl`** dosyasındadır. Her satır bağımsız JSON'dur. UTC zaman damgası, `job_id`, her worker çalıştırması için ayrı `operation_id`, aşama, süre, hata kodu ve sayısal ölçümler kaydedilir. Aynı iş yeniden denenirse `job_id` değişmez, `operation_id` değişir.

```bash
tail -n 50 .data/logs/events.jsonl
# İlgili iş kimliğini API'deki id alanından al:
grep 'İŞ_KİMLİĞİ' .data/logs/events.jsonl*
```

`size_limit` olayında `basis`, ölçümün indirilen bayt (`downloaded`), kesin toplam (`content_length`), tamamlanan kaynak (`source_file`) veya geçici dosyalar (`temporary_files`) olduğunu gösterir. `limit_bytes` kesin sınırı verir: arayüzdeki 500 MB, 524288000 bayt olarak uygulanır. `estimated_bytes` yalnız teşhis içindir.

`stage_started` ve `ffmpeg_finished` olayları kaynak çözümleme, indirme, inceleme, MP4'e aktarım ve dönüşüm aşamalarını ayırır. FFmpeg çıkış kodu ve bilinen hata sınıfı kaydedilir; ham stderr kaydedilmez. `ffmpeg -i` incelemesinin çıkış kodu 1 olabilir; tek başına indirme başarısızlığı değildir. Sonuç için `job_finished.status` ve `job_failed.code` alanlarına bak.

Hata yığınında yalnız kod dosyası, fonksiyon ve satır numarası bulunur; istisna metni, yerel değişkenler, kaynak URL'si, başlık, cookie, token veya sunucu yanıtı yazılmaz. Worker stderr'i bellekte en fazla son 16 KiB tutularak sınıflandırılır. Log dosyası 2 MiB'de döndürülür ve üç yedek saklanır (yaklaşık 8 MiB toplam); dizin 0700, dosyalar 0600 izinlidir. Logların tek yazıcısı API sürecidir. İşlerin bir saatlik temizliği logları silmez; loglar boyut sınırıyla döner.

HTTP erişim logu yalnız isteğin kabulünü gösterir: indirmeye verilen `202`, dosyanın tamamlandığı anlamına gelmez. Ayrıntılı loglama eklenmeden önceki hatalarda kesin boyut ve aşama geriye dönük belirlenemez.

Canlı siteler zamanla değiştiği için bu testler belirli bir sitenin her zaman çalışacağı anlamına gelmez. Gerçek kaynak denemeleri ayrıca yapılmalıdır.
