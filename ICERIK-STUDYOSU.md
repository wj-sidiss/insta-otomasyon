# İçerik Stüdyosu (M1)

Konu yaz, tek tuşla paket al: **başlık (3 alternatif), caption, hashtag, SEO, analiz, senaryo, kapak**.
Tamamen kendi bilgisayarında çalışır (`127.0.0.1`). Kayıt tutmaz, hiçbir yere veri göndermez
(trend sorguları hariç: konu kelimeleri Google/YouTube arama önerilerine sorulur).

## Çalıştırma

```bash
pip install -r requirements.txt
python -m playwright install chromium     # kapak çizimi için, bir kez
python -m src.icerik                      # http://127.0.0.1:8000
```

Seçenekler: `--port 8000`, `--paket-klasoru paketler`, `--imza "@kullanici"` (kapağa yazılır).
Paketler `paketler/<tarih-konu>/` altına düşer: `paket.json`, `kapak.jpg`, `kopyala.txt`.

## Yazı motoru

Kendi makinendekini kullanır, bizim anahtarımız yok. Panelin sağ üstündeki hap hangisinin hazır olduğunu gösterir.

| Motor | Gereken | Durum |
|---|---|---|
| `claude_cli` | `claude` kurulu **ve giriş yapılmış** (`claude auth login`) | **canlı denendi**, ~60 sn/paket |
| `codex_cli` | `codex` kurulu | **henüz gerçek cihazda denenmedi** |
| `anthropic_api` | `ANTHROPIC_API_KEY` ortam değişkeni | oturum/anahtar denetlenir, canlı çağrı denenmedi |
| `ornek_dosya` | `IC_ORNEK_YANIT=<json>` | yalnızca test/demo, otomatik seçilmez |

> `claude_cli` gerçek modelle iki konuda uçtan uca denendi (panel dahil, "otomatik" seçimle, ~60 sn).
> `codex_cli` ve `anthropic_api` yolları bu makinede **denenmedi**. `ornekler/ornek_yanit.json` yalnızca ağsız test/demo içindir.

**Windows notu (gerçek bir hata, düzeltildi):** `claude` npm ile `claude.CMD` olarak kurulur ve `cmd.exe` çok satırlı bir
komut satırı argümanını **ilk satırda keser**. Talimat argümanla gönderilince modele yalnızca ilk satır ulaşıyor, model
şemayı uyduruyordu. Bu yüzden hiçbir talimat argümanla gitmez; hepsi stdin'den verilir (`testler/test_icerik.py`'de regresyon testi var).
Araç ayrıca boş bir klasörden, MCP sunucuları ve beceriler kapalı çalıştırılır: kullanıcının kendi ayarları çıktıyı bozmaz.

## Trend ve hashtag: ne yapar, ne yapmaz

- Kaynak: Google ve YouTube arama önerileri (ücretsiz, anahtarsız). İki geçiş: konudan genel tarama, sonra modelin yazdığı SEO anahtar kelimeleriyle ikinci tarama.
- Bir hashtag, gerçek bir arama önerisinin 1-3 sözcüklük parçasıysa **yeşil** (doğrulandı). Aksi halde **"model önerisi, doğrulanmadı"**.
- **Hacim, popülerlik, "kaç gönderi" gibi sayı üretmez.** Instagram'ın herkese açık bir trend hashtag API'si yok; elimizde olmayan veriyi uydurmuyoruz.
- Çok spesifik konularda az veri gelebilir, panel bunu uyarı olarak gösterir.
- Otomatik tamamlama kısa sözcük çiftlerinde alakasız ifadeler de döndürür ("tekrar eden …"). Model bunları eler, ama
  "Trend arama ifadeleri" listesinde görünebilirler.

## Kalite denetimi

Model çıktısı düzeltilmez, ama gözle yakalanabilecek kusurlar **uyarı** olarak paketin üstünde görünür: yan yana tekrar eden
kelime, 125 karakteri aşan caption girişi, 70 karakteri aşan başlık, 15-60 sn dışı senaryo, 5'ten az hashtag.
Uzun alanlar cümle sınırında kesilir, ortadan bölünmez.

## Güvenlik

Yerelde çalışan bir servise tarayıcıdaki herhangi bir site istek atabilir, bu yüzden: Host denetimi (DNS rebinding),
yabancı `Origin` reddi, POST yalnızca JSON, dosya yolu yalnızca doğrulanmış paket kimliğiyle,
model çıktısı arayüzde yalnızca `textContent` ile basılır (HTML olarak çalışmaz).

## Videoyu üret: ücretsiz siteler

Panelde **"Videoyu üret · ücretsiz başlangıç"** kartı, senaryoyu ve yapay zeka istemlerini kopyalayıp kullanabileceğiniz dış siteleri listeler.
Her kayıt **yalnızca resmî fiyat sayfasında açıkça yazanı** söyler; sayfada yazmayan sınır "belirtilmemiş" diye işaretlidir, tahmin edilmez.

- Ücretsiz başlangıç: Clipchamp, CapCut, VEED, Runway (tek seferlik 125 kredi), HeyGen (ayda 3 video, filigranlı), ElevenLabs (ayda 10.000 kredi).
- **Artık ücretsiz değil** (doğrulandı): Luma, Pika (Free planda 0 kredi), InVideo.
- Doğrulanamadı (sayfa okunamadı): Kling, Canva.

Doğrulama tarihi `src/icerik/video_siteleri.py` içindedir ve her kayıtta görünür. Fiyatlar sık değişir: kayıt olmadan önce siteyi kontrol edin.

## Stok video ve görsel (kendi API anahtarınızla)

Her sahne için model, İngilizce bir **stok arama ifadesi** ve bir **yapay zeka video istemi** üretir. Sahnenin altından Pexels veya Pixabay'de
arayıp (dikey kadrajlar önce gelir) tek tıkla seçersiniz; dosya `paketler/<paket>/medya/` altına inip `krediler.txt`'ye yazılır.

**API anahtarları panelden eklenir ("API anahtarları" kartı), herkes kendi anahtarını kullanır, maliyet ve kullanım hakkı kullanıcıya aittir:**

| Sağlayıcı | Ne için | Maliyet (resmî belgeye göre) |
|---|---|---|
| Pexels | stok video + görsel | Ücretsiz. Varsayılan sınır saatte 200, ayda 20.000 istek. Pexels'e belirgin bağlantı ve içerik sahibine kredi istenir. |
| Pixabay | stok video + görsel | Ücretsiz. 60 sn'de 100 istek, sonuçlar 24 saat önbelleğe alınır. Kaynak gösterilmesi istenir. |
| Anthropic API | yazı motoru (isteğe bağlı) | Kullandıkça ödenir (ücretli). |

Anahtarlar **yalnızca bu bilgisayarda `config/anahtarlar.json` dosyasında düz metin** olarak durur (git'e girmez; şifrelenmez). Ortam değişkeni
(`PEXELS_API_KEY`, `PIXABAY_API_KEY`, `ANTHROPIC_API_KEY`) dosyadan önceliklidir. Anahtar hiçbir yanıtta, hata mesajında veya logda tam görünmez.
İndirme yalnızca bizim aramamızın döndürdüğü adaylardan yapılır (istemci URL veremez). Her kaynağın **kendi** sunucu izin listesi vardır (Wikimedia adayı NASA sunucusuna işaret edemez); izin dışı adres, yönlendirme ve yerel/özel ağa çözülen adlar (SSRF) reddedilir.

**Denenmedi (dürüst not):** Bu makinede gerçek bir Pexels/Pixabay anahtarı yok (hesap açmak benim yapabileceğim bir şey değil). Gerçek servislerle
yalnızca **hata yolu** denendi (uydurma anahtarı ikisi de reddediyor, panel bunu anlaşılır mesaja çeviriyor). **Başarılı arama ve indirme**, resmî belgedeki
yanıt biçimlerine göre yazılmış sahte sunucuyla test edildi. Pixabay video dosyalarının `cdn.pixabay.com` üzerinden geldiği varsayımı doğrulanamadı: farklı bir
sunucudan gelirse indirme güvenlik izin listesi nedeniyle reddedilir ve hata mesajı gösterilir.

## Anahtarsız açık kaynak görseller

Anahtar gerekmeyen beş kaynak daha vardır (yalnızca **görsel**; video yok): **Wikimedia Commons, NASA, The Met, Art Institute of Chicago, Cleveland Museum of Art.**
Anahtarınız yoksa panel varsayılan olarak bunlardan birini seçer. **2026-10-25'te gerçek ağda doğrulandı** (arama + indirme, beşi de).

- **Lisans süzgeci** aday listesine girmeden çalışır: NC (ticari değil), ND (türetilemez), GFDL ve "serbest olmayan" dosyalar elenir. Müzeler yalnızca CC0 / kamu malı eserleri döndürür.
- **CC BY ve CC BY-SA atıf ZORUNLU kılar.** Seçtiğiniz görselin lisansı bunlardan biriyse panel "atıf zorunlu" uyarısı verir, tam atıf satırını üretir
  ("Başlık" — Yazar — Lisans — adres), `krediler.txt` dosyasına yazar ve kopyalanabilir bir kutuda gösterir. CC BY-SA ayrıca "video türev eser sayılabilir" uyarısı taşır.
  Bu hukuki görüş değildir; kaynak sayfasındaki koşullar esastır.
- Başlığında logo, harita, diyagram, ekran görüntüsü gibi sözcükler geçenler elenir; heykel/çizim/belge gibi "zayıf" sonuçlar listenin sonuna konur. Dikey kadrajlar öne gelir.
- NASA'da üçüncü taraf telifli (© / copyright) görseller elenir; ana adres olmazsa `~orig → ~large → ~medium` yedekleri sırayla denenir.
- Kaynaklara nazik davranılır: kaynak başına en kısa aralık ve **429 sonrası "soğutma"** (kullanıcı ardı ardına tıklayıp servisi dövmez).

Canlı doğrulamada çıkan, sahte sunucuyla asla görülemeyecek gerçek kurallar (kodda ve testlerde kayıtlıdır):
Wikimedia, User-Agent'ta **iletişim bilgisi (URL/e-posta)** olmayan istekleri 403 ile reddeder; Art Institute of Chicago ise tam tersine UA'da `python`/`httpx` gibi
**kütüphane adı** geçerse 403 verir ve görsel sunucusu da `AIC-User-Agent` başlığını ister; The Met'in eski `v1/search` ucu kapandı (`v1.1`); Cleveland boyutları metin olarak verir.
Varsayılan iletişim adresi proje depodur; kendi adresinizi `IC_ILETISIM` ortam değişkeniyle verebilirsiniz.

## Carousel (kaydırmalı gönderi)

Paket kartındaki **"Carousel üret"** bloğu aynı konudan 4-10 slaytlık bir gönderi yazar ve çizer (1080×1350, 4:5; Instagram API'si en fazla 10 görsel kabul eder).
İlk slayt her zaman kapak, son slayt her zaman kapanış (kaydet/paylaş çağrısı) olur; model fazla slayt yazarsa ortadakiler atılır, gerçek kapanış korunur.
Çıktı `paketler/<paket>/carousel/` altına düşer (`slayt-01.jpg` …, `carousel.json`) ve **tek .zip** olarak (slaytlar + caption + ilk 10 hashtag) indirilir.
Slayt metinleri model çıktısıdır: HTML'e basılmadan önce kaçırılır, uydurma sayı/vaat/emoji yasağı istemde vardır.
**Denenmedi:** carousel'in Instagram'a API ile yayımlanması (görsellerin herkese açık bir adres olması gerekebilir); şimdilik zip'i elle yüklersiniz.

## Reels videosu (otomatik birleştirme)

Paket kartındaki **"Videoyu birleştir"** bloğu senaryoyu 1080×1920 (9:16) mp4'e çevirir: her sahne için sesli anlatım, sahneye seçtiğin stok görsel/video
(görsel yavaşça yakınlaşır, video dikeye kırpılıp döngülenir) ve üstüne ekran yazısı; sahneler birleştirilip ses seviyesi normalize edilir (loudnorm −16 LUFS).
Medya seçilmemiş sahneler ekran yazısını taşıyan düz karta dönüşür, böylece video her durumda çıkar; uyarıda hangi sahnelerin kart olduğu yazar.
Çıktı `paketler/<paket>/video/reels.mp4`. Süre anlatımın uzunluğundan gelir (sahne başına 2-20 sn); 90 sn üstü uyarı verir.

- **Gereken:** `ffmpeg` + `ffprobe` (yoksa panel kurulum komutunu söyler), Playwright/Chromium (yazı katmanı için), sesli anlatım için `pip install edge-tts`.
- **Gizlilik:** Sesli anlatım, anlatım metnini edge-tts üzerinden Microsoft'un okuma servisine gönderir (anahtarsız, **resmî olmayan** bir servis; bir gün çalışmayabilir). "Sessiz" seçersen hiçbir şey gönderilmez.
  Ses gelmezse sahne sessiz devam eder ve uyarı verir; Emel sesi gelmezse Ahmet denenir.
- **Denendi (2026-10-26):** 7 sahne, 61 sn, h264+aac, gerçek Türkçe ses, tarayıcıda oynatma. **Denenmedi:** gerçek Pexels/Pixabay videosuyla birleştirme (anahtar yok; test klipleriyle denendi), Instagram'a yükleme, GPU kodlayıcılar (yalnızca libx264).
- **Yok (henüz):** kelime kelime altyazı, arka plan müziği, sahneler arası geçiş. Ekran yazısı sahne boyunca sabit durur.

## Panel sertleştirmesi

Betik ve stil ayrı dosyalardır (`panel.js`, `panel.css`); CSP'de `script-src 'self'` ve `style-src 'self'` vardır, **`'unsafe-inline'` yoktur**: bir yerde HTML
enjeksiyonu olsa bile satır içi betik çalışmaz. Tarayıcı `Sec-Fetch-Site` gönderiyorsa, başka siteden gelen API/medya istekleri ve **tüm değiştirici istekler** reddedilir.
Bu koruma ve SSRF tablosu, aynı sahibin Otopilot projesinden (MIT) tasındı.

## Henüz yok

Kelime kelime altyazı ve müzik, Instagram bağlantısı ve paylaşım (M3), MCP sunucusu (M4). Ayrıntı: [URUN-PLANI.md](URUN-PLANI.md).
