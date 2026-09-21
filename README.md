# TOYWES · MP4 Avcısı

TOYWES (Toplum Yararına Web Siteleri) için video bağlantısını analiz eden, desteklenen herkese açık sayfalardaki gömülü kaynakları bulan ve MP4 dosyası hazırlayan yerel web uygulaması.

Arayüz koyu tema kullanır. İsteğe bağlı Proton VPN bağlantısı, uygun erişim hatalarında otomatik devreye girer ve son kullanan işlem bittiğinde kapanır. PornHub, XVideos, Eporner ve xHamster gibi doğrulanmış herkese açık kaynaklarda yt-dlp'nin desteklediği Chrome TLS taklidi kullanılır; bunun için kilitli bağımlılıklarda `curl-cffi` bulunur. Brazzers için yalnız giriş istemeyen herkese açık video veya fragman sayfaları denenir; üyelik ya da DRM koruması aşılmaz.

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
3. Analiz sırasında oluşan cookie'ler, istek başlıkları ve çözümlenmiş formatlar yalnız sunucuda, 0600 izinli kısa ömürlü bir indirme planında saklanır. İndirme kaynak sayfasını ikinci kez çözümlemeden bu oturumu kullanır.
4. Seçilen çözünürlüğü aşmayan en iyi kaynak indirilir. `En iyi kalite` üst sınır koymaz.
5. Ayrı görüntü/ses varsa FFmpeg birleştirir. MP4 kapsayıcısına kayıpsız aktarım denenir; gerekirse H.264/AAC dönüşümü yapılır.
6. Kullanıcı videoyu kuyruğa ekler. Aynı anda iki indirme çalışır, diğerleri FIFO sırasıyla başlar. İndirme sürerken başka bir bağlantı analiz edilebilir.
7. “İndirmelerim” bölümü her işin durumunu, bekleme sırasını ve dosya temizlenme saatini gösterir. Duraklat, devam et, iptal et, indir ve sil işlemleri ayrı ayrı yapılabilir.
8. Sayfa kapansa veya sunucu yeniden başlatılsa da kayıtlar kalır. Yeniden başlatmada yarım kalan işler duraklatılmış olarak açılır; kullanıcı “Devam et” ile sürdürür.

### Devam etme ve hata davranışı

- Duraklatma worker ve FFmpeg işlem grubunu durdurur; `.part` ve segment kayıtları korunur. yt-dlp, kaynak destekliyorsa HTTP Range veya segment bilgisiyle kaldığı yerden devam eder. Kaynak desteklemiyorsa ilgili dosya/parça yeniden indirilir. MP4 dönüştürme aşaması yeniden başlar.
- Ağ kesintisi ve zaman aşımı gibi geçici hatalarda kısmi dosyalar saklanır ve “Yeniden dene” sunulur. Desteklenmeyen kaynak, erişim engeli veya koruma durumlarında neden açıkça gösterilir; kaynak yanıtları, imzalı URL'ler ve tokenlar kullanıcıya aktarılmaz.
- Boyut aşımı `size_limit` koduyla ve daha düşük kalite seçme önerisiyle gösterilir. Kesin kaynak boyutu ve indirilen gerçek baytlar kontrol edilir; HLS gibi akışların değişken boyut tahmini tek başına indirmeyi durdurmaz. yt-dlp'nin dosyayı sessizce atlayan boyut filtresi yerine bu açık hata kullanılır.
- Tarayıcı sunucuya ulaşamazsa liste ekranda kalır ve en fazla 30 saniyeye çıkan aralıklarla yeniden sorgulanır. Sayfayı yenilemek gerekmez.
- “İptal et” kısmi dosyaları kaldırır. “Sil” hem iş kaydını hem dosyalarını siler. Cihaza daha önce kaydettiğin dosya etkilenmez.

Doğrudan MP4, yt-dlp'nin desteklediği site/oynatıcılar ve yerel indiriciyle çözülebilen korumasız HLS/DASH akışları hedeflenir. Bir sayfada birden fazla video varsa ilk video kullanılır. Site desteği, o sitenin güncel davranışına bağlıdır. yt-dlp için [resmî belgeler](https://github.com/yt-dlp/yt-dlp) ve [desteklenen siteler](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md).

## Sınırlar ve dağıtım

- Bu sürüm tek kullanıcı için **localhost** üzerinde çalışır; internete açık bir hizmet olarak yayımlanmamıştır. Host kontrolü localhost ile sınırlıdır.
- Geçici uzaktan testlerde `MP4_ALLOWED_HOSTS` ile host listesi, `MP4_ACCESS_USER` ve `MP4_ACCESS_PASSWORD` ile HTTP Basic giriş zorunluluğu ayarlanabilir. Ayrı bir sunucu proxy'si `MP4_INTERNAL_TOKEN` değerini `X-MP4-Internal-Token` başlığında göndererek aynı API'ye erişebilir; bu token tarayıcıya verilmemelidir. `/api/health` sağlık kontrolü için açık kalır.
- DRM, giriş isteyen kaynaklar ve canlı yayınlar desteklenmez. JavaScript çalıştırarak ağ trafiği yakalama henüz eklenmemiştir. Bölgesel/ağ erişim hatalarında yapılandırılmış Proton VPN üzerinden bir alternatif deneme yapılabilir; giriş, CAPTCHA veya DRM kaldırılmaz.
- Dosya başına varsayılan 2 GB kaynak sınırı, 2 saat video süresi, eşzamanlı iki indirme ve kuyruk beklemesi hariç en fazla 15 dakika hazırlama süresi vardır. Çalışanlar dahil en fazla 10 iş sıraya alınır; toplam 20 kayıt saklanır. Analiz için ayrı bir işlem yuvası bulunur. Kaynak sınırı `MP4_MAX_BYTES`, geçici işlem diski sınırı `MP4_MAX_DISK_BYTES` ile bayt cinsinden değiştirilebilir. Birleştirme/dönüşüm sonucunun boyutu kaynak boyutundan farklı olabilir.
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

`size_limit` olayında `basis`, ölçümün indirilen bayt (`downloaded`), kesin toplam (`content_length`), tamamlanan kaynak (`source_file`) veya geçici dosyalar (`temporary_files`) olduğunu gösterir. `limit_bytes` yapılandırılmış kesin sınırı verir; varsayılan kaynak sınırı 2147483648 bayttır (2 GiB). `estimated_bytes` yalnız teşhis içindir.

`stage_started` ve `ffmpeg_finished` olayları kaynak çözümleme, indirme, inceleme, MP4'e aktarım ve dönüşüm aşamalarını ayırır. FFmpeg çıkış kodu ve bilinen hata sınıfı kaydedilir; ham stderr kaydedilmez. `ffmpeg -i` incelemesinin çıkış kodu 1 olabilir; tek başına indirme başarısızlığı değildir. Sonuç için `job_finished.status` ve `job_failed.code` alanlarına bak.

Geçici testlerde `MP4_TEST_DETAILS=1` ayarlanırsa her indirme kartındaki **Test ayrıntıları** düğmesi yalnız o işe ait güvenli tanı zaman çizelgesini gösterir. Kaynak URL'leri, cookie/token değerleri ve uzak sunucu yanıt gövdeleri API'ye veya arayüze gönderilmez. Desteklenen tarayıcı kaynaklarında analizden kalan imzalı akış adresi 403/410 dönerse indirme, sayfayı aynı özel oturumla bir kez yeniden çözümler.

Hata yığınında yalnız kod dosyası, fonksiyon ve satır numarası bulunur; istisna metni, yerel değişkenler, kaynak URL'si, başlık, cookie, token veya sunucu yanıtı yazılmaz. Worker stderr'i bellekte en fazla son 16 KiB tutularak sınıflandırılır. Log dosyası 2 MiB'de döndürülür ve üç yedek saklanır (yaklaşık 8 MiB toplam); dizin 0700, dosyalar 0600 izinlidir. Logların tek yazıcısı API sürecidir. İşlerin bir saatlik temizliği logları silmez; loglar boyut sınırıyla döner.

`worker_failed.causes`, yt-dlp'nin `exc_info`/`cause` alanları dahil en fazla sekiz bağlı istisnanın türünü ve varsa HTTP hata kodunu tutar. Ham hata metni kaydedilmez. `source_parse` sayfa yanıtının çözümlenemediğini, `tls_failed` güvenli bağlantı sorununu ayırır. Geçmişte yalnız `source_failed` olarak yazılmış kayıtların asıl nedeni geriye dönük çıkarılamaz; aynı kaynak yeniden denenmelidir.

### Kaynak isteğini ve denemeleri ayırma

Her üst düzey işlem `request_id` alır; doğrudan ve Proton denemeleri aynı kimliği paylaşır, her worker'ın `operation_id` değeri ayrıdır. Hata ekranındaki kısa işlem kodu loglarda bu kimliğin başlangıcını aramak içindir. `routing_failed` son denemenin rotasını ve hata nedenini özetler. Kaynağın VPN üzerinden reddettiği istek, arayüzde açıkça belirtilir.

`request_finished` / `request_failed` olayları HTTP yöntemini, durum kodunu, süreyi ve kaynak türünü ayırır. `resource`, istenen adresin dosya uzantısından ve başarılı yanıtlarda içerik türünden çıkarılır; belirsiz adresler `other` olarak kalır. `HEAD 200`, sonraki `GET` veya medya isteğinin başarılı olduğunu göstermez. İstek gövdeleri, başlık değerleri, URL, çerez ve token kaydedilmez. `target_ref` yalnız aynı worker içinde tekrarlanan hedefleri ilişkilendiren, her çalıştırmada rastgele tuzla yenilenen bir özettir.

Başarılı istek kayıtları ilk 40 istekle sınırlıdır; hatalar ve bir hatadan sonraki toparlanma ayrıca görünür. `cookie_count`, `has_referer` ve `user_agent_changed` oturum tutarlılığını değerleri ifşa etmeden gözlemlemek içindir. Bunlar tek başına bir sitenin beklediği oturumun geçerli olduğunu kanıtlamaz. `engine_ready` motor sürümünü; `source_warning.source_hint` genel çözümleyiciye dönüş veya kullanılamayan isteğe bağlı tarayıcı aktarımı uyarısını ayırır. Bu uyarı tek başına 403 nedeni sayılmaz.

Kontrollü test, oturum çerezi ve doğru Referer olmadan 403 veren yerel bir sayfadan gerçek MP4 indirir. Çerez ve başlık aktarımı mevcut yt-dlp oturumunda korunur; analiz ve indirme ayrı süreçler olduğundan indirme kaynak sayfasını tekrar çözümler. Tarayıcının kişisel oturumu içe aktarılmaz.

Uyumluluk incelemesinde (20 Eylül 2026) kurulu `2026.08.19` sürümü, PyPI ve GitHub'daki son kararlı sürümle aynıydı; kanıtlanmış bir güncelleme farkı olmadığı için sürüm değiştirilmedi. Otomatik strateji en fazla bir doğrudan ve bir Proton denemesiyle sınırlıdır; aynı işlemde rastgele sağlayıcı/sürüm değişimi veya sınırsız yeniden deneme yoktur.

HTTP erişim logu yalnız isteğin kabulünü gösterir: indirmeye verilen `202`, dosyanın tamamlandığı anlamına gelmez. Ayrıntılı loglama eklenmeden önceki hatalarda kesin boyut ve aşama geriye dönük belirlenemez.

Canlı siteler zamanla değiştiği için bu testler belirli bir sitenin her zaman çalışacağı anlamına gelmez. Gerçek kaynak denemeleri ayrıca yapılmalıdır.

## İsteğe bağlı otomatik Proton VPN

İndirmeyi **sunucu** yapar. Kullanıcının ülkesinde bir site engelli olsa bile sunucu erişebiliyorsa VPN gerekmez. Sunucu bağlantısı başarısız olduğunda Proton üzerinden yeniden deneme yardımcı olabilir; bütün engellerin aşılması garanti edilmez.

Bu entegrasyon Linux üzerinde yerel Docker Engine ve `/dev/net/tun` kullanır. Proton'un masaüstü uygulamasını veya bilgisayarın genel ağ bağlantısını değiştirmez. WireGuard, Gluetun konteyneri içinde çalışır; yalnız ilgili worker'ın trafiği bu tünelden geçer. Docker Compose gerekmez. API'nin çalıştığı kullanıcı `/var/run/docker.sock` üzerinden Docker'a erişebilmelidir. Docker soketi web istemcilerine açılmaz ve konteynere bağlanmaz.

### Bir defalık hazırlık

1. [Proton hesabından Linux için WireGuard yapılandırması indir](https://protonvpn.com/support/wireguard-configurations). Hesabının erişebildiği, kaynağın açık olduğu bir sunucuyu seç. `.conf` dosyası gizli anahtar içerir; GitHub'a veya sohbete ekleme.
2. Gluetun imajını hazırla:

   ```bash
   docker pull qmcgaw/gluetun:v3.41.3
   ```

3. İndirilen dosyayı proje kökünden içe aktar:

   ```bash
   .venv/bin/python -m app.configure_vpn /tam/yol/proton.conf
   ```

Dosya doğrulanarak `.data/proton/wg0.conf` konumuna 0600 izinleriyle kaydedilir. Mevcut geçerli ayar, hatalı dosya içe aktarılırsa korunur. Dosya bulunmadığında uygulama normal bağlantıyla çalışır ve VPN başlatmaz. Dosya eklendikten sonra sonraki işlem otomatik kullanabilir; sunucuyu yeniden başlatmak gerekmez. Tamamen devre dışı bırakmak için sunucuyu `MP4_VPN_AUTO=0` ortam değişkeniyle başlat.

Proton masaüstü uygulamasını kurmak veya elle açmak bu yapılandırmanın yerini almaz. Arayüzde “Otomatik VPN ayarlanmamış” görünüyorsa uygulama kendi tünelini açamaz. Uygun erişim hatalarında logdaki `vpn_unconfigured` kaydı ve kullanıcıya gösterilen mesaj bu eksikliği belirtir.

Bu sürümün VPN ağ geçidi Docker'ın IPv4 köprüsünü kullanır. Proton'un çift adresli dosyası içe aktarılırken yalnız uygulamaya ait kopyadaki `Interface.Address` IPv4 ile sınırlandırılır; indirdiğin özgün dosya değiştirilmez. Böylece IPv6 desteği kapalı Docker ortamlarında Gluetun'un başlangıçta kapanması önlenir. Yalnız IPv6 adresi içeren yapılandırmalar kabul edilmez.

```bash
MP4_VPN_AUTO=0 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### İşlem davranışı

- Önce doğrudan bağlantı denenir. Bölge engeli, genel HTTP 403, ağ/TLS bağlantısı veya sayfa ayrıştırma sorunu uygun olduğunda **bir kez** Proton üzerinden denenir. Bu davranış ilk URL analizi için de geçerlidir; TLS doğrulaması kapatılmaz. Her 403 veya ayrıştırma sorunu ülke engeli anlamına gelmez. CAPTCHA/bot doğrulaması, giriş, 429, DRM, bulunamayan dosya ve boyut sınırı VPN'i tetiklemez. Dönüştürme aşamasının zaman aşımı da VPN'i başlatmaz.
- Doğrudan analiz, VPN yapılandırılmışsa 35 saniye ile sınırlanır; kalan analiz bütçesi VPN açılışı ve ikinci denemeye ayrılır. Toplam analiz bütçesi 90 saniye, indirme bütçesi 15 dakikadır. Temizlik için ayrıca sınırlı süre gerekebilir.
- VPN'de bulunan kaynağın indirmesi ve devam ettirilmesi yine VPN'den yapılır. VPN denemesi başarısızsa sessizce doğrudan bağlantıya dönülmez. API/arayüzde `route: proton` görünür; adres, parola ve anahtar görünmez.
- İlk ihtiyaçta projeye özel konteyner açılır ve sağlıklı olması beklenir. Eşzamanlı işler tek bağlantıyı paylaşır. Son iş başarıyla bittiğinde, hata aldığında, duraklatıldığında veya iptal edildiğinde konteyner kaldırılır. Bir işin bitmesi diğerinin tünelini kapatmaz.
- Gluetun güvenlik duvarı açık kalır. Proxy yalnız `127.0.0.1:18989` üzerinde yayımlanır ve her başlangıçta yeni parola alır. Parola özel bir geçici dosyadan konteynere, stdin üzerinden worker'a iletilir; işlem argümanlarına yazılmaz.
- VPN modunda DNS sorguları da tünelin içinden [Cloudflare DoH](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/) ile yapılır. TLS doğrulanır, genel IP'ler kontrol edilir ve HTTP CONNECT hedefi doğrulanan sayısal IP'ye sabitlenir. Özel ağ/metadata IP'leri, yönlendirmeler ve karışık DNS yanıtları VPN'de de engellenir. Yerel DNS'e veya doğrudan ağa sessiz geri dönüş yapılmaz.
- Kaynak bir engel sayfasını HTTP 200 ile döndürürse bunu güvenilir şekilde ülke engeli olarak tanımak mümkün olmayabilir. Bu yanıt `source_parse` hatasına yol açarsa alternatif bağlantı denenir; genel `source_failed` veya `unsupported` hataları otomatik VPN'i tetiklemez.
- Süreç zorla öldürülürse konteyner kalabilir; bir sonraki VPN başlangıcı yalnız aynı projeye ait etiketli eski konteyneri temizler. Normal sunucu kapanışında aktif işler ve bağlantı kapatılır. Konteynerin durdurulması başarısız olursa `vpn_cleanup_failed` loglanır; işletici Docker durumunu kontrol etmelidir.

`GET /api/network`, yapılandırmanın varlığını, bağlantı durumunu ve kullanan iş sayısını gösterir. `configured: true` dosyanın var olduğu anlamına gelir; canlı bağlantı ancak ilk kullanımda sağlık kontrolünden sonra doğrulanır. Anahtar/token döndürmez. Loglarda `vpn_starting`, `vpn_connected`, `vpn_fallback`, `vpn_stopped` ve başarısız temizlik olayları bulunur.

Uygulama tek sunucu süreci içindir. Free-Web-Tools'a çok kullanıcılı dağıtım yapılırken kuyruk, VPN bağlantı sayacı, kullanıcı kotaları ve yetkilendirme merkezi servis olarak ele alınmalıdır. Şimdiki değişiklik o repoya otomatik dağıtım yapmaz.

Entegrasyon [Gluetun özel WireGuard sağlayıcısı](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/custom.md), [yapılandırma dosyası](https://github.com/qdm12/gluetun-wiki/blob/main/setup/options/wireguard.md) ve [HTTP proxy](https://github.com/qdm12/gluetun-wiki/blob/main/setup/options/http-proxy.md) belgelerine dayanır. Proton anahtarı olmadan testler yerel proxy/TLS düzenekleriyle çalışır; gerçek Proton çıkışı ayrıca doğrulanmalıdır.
