# Ürün Planı: Yerel İçerik Otomasyonu (açık kaynak)

> Repoyu alan kişi kendi bilgisayarına kurar, `localhost`'ta çalıştırır.
> Bizim sunucumuz yok, kayıt tutmuyoruz, telemetri yok.
> Önceki teknik plan: [PLAN.md](PLAN.md) (Instagram yayın motoru, hazır).

---

## 1. Ne yapacak

Kullanıcı bir **konu** yazar, tek tuşa basar. Sistem üretir:

| Çıktı | Not |
|---|---|
| Reels videosu (9:16 mp4) | senaryo + seslendirme + altyazı + görsel |
| Kapak (thumbnail) | kart sistemimizle üretilir |
| Başlık | 3 alternatif, biri seçili gelir |
| Caption | hook + gövde + CTA |
| Hashtag seti | trend verisiyle, gerekçesiyle |
| SEO | arama anahtar kelimeleri, alt metin |
| Analiz | neden bu açı, hangi kelimeler yükseliyor, ne eksik |

Sonra iki yoldan biri:

- **Instagram'a bağlıysa:** aynı tuş paylaşır (isteğe bağlı önizleme açık/kapalı).
- **Bağlı değilse:** paket bir klasöre ve panele düşer, kopyala/indir butonlarıyla kullanıcı kendisi paylaşır.

Giriş **zorunlu değil**. Giriş yapılmazsa her şey yine üretilir.

---

## 2. Instagram girişi

**Kullanıcı adı/şifre istemiyoruz.** Üç sebep:

1. Şifreyi bir betiğe verip tarayıcıyı otomatikleştirmek Instagram kurallarına aykırı ve ban sebebi.
2. Açık kaynak bir repoda başkalarının şifresini işlemek, kimsenin üstlenmek istemeyeceği bir sorumluluk.
3. Gerek yok: resmi yol var.

**Resmi yol: OAuth (Meta Graph API).** Kullanıcı panelde "Instagram'a bağlan" der, Meta'nın kendi sayfasında giriş yapıp onaylar. Biz sadece token alırız.

- Token **yalnızca kullanıcının kendi diskinde** (`config/.env`, git'e girmez) durur. Bize hiçbir şey gelmez.
- "Çıkış yap" token'ı siler.

**Bilmen gereken kısıt:** OAuth için bir **Meta uygulaması** gerekir ve iki seçenek var:

| Seçenek | Artı | Eksi |
|---|---|---|
| **Her kullanıcı kendi Meta app'ini açar** (varsayılan) | Onay süreci yok, bize bağımlılık yok, gizlilik tam | Kurulumu 10-15 dk, panel adım adım yönlendirecek |
| Biz tek bir app yayınlarız | Kullanıcı sadece "bağlan" der | Meta App Review + iş doğrulaması + gizlilik politikası + sunucu gerekir; "kayıt tutmuyoruz" ilkesiyle çelişir |

Varsayılan: **her kullanıcı kendi app'ini açar.** Kendi hesabı için geliştirme modunda App Review gerekmiyor.

Hesap koşulu: Instagram **Business/Creator** ve Facebook Sayfası'na bağlı olmalı (Facebook Login yolu).

---

## 3. Trend ve hashtag verisi: neyi bulabiliriz, neyi bulamayız

Instagram'ın herkese açık bir "trend hashtag" API'si **yok**. Dürüst tablo:

| Kaynak | Ne verir | Maliyet | Durum |
|---|---|---|---|
| Google arama önerileri | insanların gerçekten yazdığı sorgular | ücretsiz, anahtarsız | **test edildi, çalışıyor** |
| YouTube arama önerileri | video odaklı yükselen sorgular | ücretsiz, anahtarsız | **test edildi, çalışıyor** |
| vidIQ anahtar kelime araştırması | arama hacmi, rekabet | kullanıcının vidIQ hesabı | isteğe bağlı |
| Instagram hashtag arama API'si | hashtag'e ait gönderiler | haftalık sınır + App Review | MVP'ye girmiyor |

**Bunu yapmayacağız:** hashtag için uydurma "gönderi sayısı" veya "hacim" rakamı yazmak. Elimizde olmayan veriyi LLM'e tahmin ettirmeyeceğiz. Analiz ekranında her kelimenin **kaynağı** yazar ("YouTube önerisi", "Google önerisi"), hacim rakamı sadece vidIQ bağlıysa gösterilir.

---

## 4. Yazı motoru (Claude veya Codex, fark etmez)

Tek bir **sağlayıcı adaptörü**. Kurulumda kullanıcı kendi makinesindekini seçer:

1. `claude` CLI (kurulu ve giriş yapılmışsa, API anahtarı gerekmez)
2. `codex` CLI
3. API anahtarı (Anthropic veya OpenAI)

Bizim anahtarımız yok, repoda hiçbir anahtar bulunmaz.

---

## 5. Video üretimi: karışık

- **Yerel çekirdek (varsayılan, ücretsiz):** hareketli yazı + rakam kartları + altyazı + Türkçe TTS (edge-tts) + ffmpeg birleştirme. İnternet gerekmez, ses hariç.
- **Opsiyonel b-roll:** kullanıcı vidIQ bağlarsa belirli sahnelerde AI görüntü eklenir.

Gerçekçi beklenti: yerel çekirdek "motion graphics / anlatım videosu" üretir. Gerçek çekim gibi görünmez. AI, finans, pazarlama, eğitim anlatımına uygundur.

---

## 6. Mimari

```
Panel (localhost) ──┐
MCP sunucusu ───────┼──> Üretici ──> Paket (mp4 + kapak + metinler)
(Claude/Codex)      │       │                  │
                    │       ├─ Yazı motoru     ├─ Panelden indir/kopyala
                    │       ├─ Trend toplayıcı └─ Kuyruk ──> Yayıncı ──> Instagram
                    │       └─ Video üretici        (mevcut sistem)
```

**Yeniden kullanılan:** kuyruk, yayıncı, zamanlayıcı, süreç kilidi, arşiv (hepsi yazıldı, 208 test).
**Eklenecek:** Reels cover/thumb_offset desteği, feed görseli ve carousel (yayıncı şu an sadece Reels ve Story biliyor).

Sohbetten çağırmak için ayrı bir **yerel MCP sunucusu** (`uret_reels`, `paketi_goster`, `paylas`). Tarayıcı eklentisi MVP dışı: sohbet sitelerinin arayüzüne bağımlı, güncellemede bozulur.

---

## 7. Aşamalar

| | İş | Meta hesabı gerekir mi |
|---|---|---|
| **M1** ✅ | Panel + yazı motoru + trend toplayıcı + paket (başlık, caption, hashtag, SEO, analiz, senaryo, kapak). Giriş yok. **Bitti, claude ile canlı denendi.** Ayrıntı: [ICERIK-STUDYOSU.md](ICERIK-STUDYOSU.md) | hayır |
| **M2** | Video üretici (senaryo → ses → altyazı → mp4) | hayır |
| **M3** | Instagram bağlantısı (OAuth sihirbazı) + tek tuşla paylaş + feed/carousel/cover desteği | **evet** |
| **M4** | MCP sunucusu, vidIQ b-roll, isteğe bağlı eklenti | kısmen |

M1 ve M2 Meta hesabı olmadan baştan sona test edilebilir. M3, hesap Business'a çevrilip token alınana kadar sahte API ile ilerler.

---

## 8. Açık riskler

- **Yeni hesapta seri otomatik paylaşım** Instagram'ın spam desenine benzer. Varsayılan: önizleme açık, günlük 1-2 paylaşım, ısınma süresi uyarısı.
- **Meta'ya karşı hiç gerçek yayın denenmedi.** Tüm testler sahte API ile. İlk gerçek yayında sürpriz çıkabilir.
- **AI üretimi içerik etiketi:** Meta gerçekçi AI görüntü için etiket ister, paylaşım adımında hatırlatma koyacağız.
- **Repo public olacağı için:** hiçbir token, `.env`, kuyruk veritabanı ya da kullanıcı medyası depoya girmez (`.gitignore` hazır, push öncesi sızıntı taraması yapılacak).
- Kullanıcı TTS ve trend uç noktalarının koşullarına kendi makinesinden bağlıdır, bunların değişmesi kırılma sebebi olabilir.
