# Instagram Hikaye & Reels Otomasyonu — Teknik Plan

> Karar verildi: **Facebook Login yolu**. Facebook Sayfası var kabul ediliyor, token sonra doldurulacak.
> Kaynak: Meta "Instagram Platform / Content Publishing" (güncelleme: 30 Haz 2026).

---

## 1. Seçilen yol

**Instagram API with Facebook Login**

| | |
|---|---|
| Host | `graph.facebook.com` |
| Video yükleme | `rupload.facebook.com` (resumable) |
| Token | Facebook Page access token |
| İzinler | `instagram_basic`, `instagram_content_publish`, `pages_read_engagement` |
| Lokal dosya yükleme | ✅ **var** — `upload_type=resumable` |

**Sonuç: depolama katmanı (R2/S3/tünel) YOK.** Dosya bilgisayardan doğrudan Meta'ya yükleniyor. Mimari tek parça, dışarıya bağımlılık yok.

> Reddedilen alternatif: Instagram Login yolu (Sayfa istemiyor) — ama medyayı public URL'den çektiği için araya bulut depolama girmesi gerekiyordu.

---

## 2. Klasör yapısı

```
insta-otomasyon/
├─ gelen/
│  ├─ reels/      ← .mp4 at → Reels olarak yayınlanır
│  └─ story/      ← .mp4 / .jpg at → Story olarak yayınlanır
├─ yayinlandi/    ← başarılılar YYYY-MM/ altına tarih damgalı taşınır
├─ hata/          ← başarısızlar + .log + otomasyon.log
├─ config/
│  ├─ .env            ← token'lar (örnek: .env.ornek)
│  └─ ayarlar.yaml    ← slotlar, limitler (örnek: ayarlar.ornek.yaml)
├─ src/
├─ testler/
└─ kuyruk.db      ← SQLite
```

### Dosya adı kuralları

| Ne istiyorsun | Dosya adı |
|---|---|
| Sıradaki uygun slotta yayınla | `tanitim.mp4` |
| Belirli tarih/saatte yayınla | `2026-09-25_1830_tanitim.mp4` |
| Caption ekle | yanına `tanitim.txt` |

Caption dosyası yoksa `ayarlar.yaml`'daki varsayılan şablon kullanılır.
`.txt` dosyaları kuyruğa görev olarak girmez, sadece caption kaynağıdır.

---

## 3. Akış

```
gelen/reels/ · gelen/story/
   │  watchdog — dosyanın kopyalanması bitene kadar bekler
   ▼
Kuyruk (SQLite)   bekliyor → yükleniyor → hazırlanıyor → yayınlandı / hata
   │
   ▼
Zamanlayıcı (APScheduler, dakikada bir)
   │  · planlanan saati gelen var mı?
   │  · günlük yayın limiti doldu mu?  → content_publishing_limit
   ▼
Publisher
   │  1. POST /{ig-id}/media          media_type=REELS|STORIES, upload_type=resumable
   │  2. POST rupload.facebook.com/…  ← lokal dosya, kesintide offset'ten devam
   │  3. GET  /{container}?fields=status_code   → FINISHED olana kadar poll
   │  4. POST /{ig-id}/media_publish  creation_id=…   ← retry YOK
   ▼
Arşiv: yayinlandi/YYYY-MM/ altına taşı, permalink'i DB'ye yaz
```

Her adımda DB durumu güncelleniyor — program çökerse kaldığı yerden devam ediyor.

---

## 4. Kritik detaylar

- **Limit:** hesap başına 24 saatlik kayan pencerede **100** API paylaşımı. `GET /{ig-id}/content_publishing_limit` ile publish öncesi kontrol ediliyor.
- **Token ömrü:** Page access token, uzun ömürlü user token'dan türetildiğinde süresiz — ama iptal/şifre değişimi ile düşebilir. Haftalık `debug_token` kontrolü var, geçersizse yüksek sesle uyarıyor. **En sık atlanan madde.**
- **Görsel formatı:** Meta sadece **JPEG** kabul ediyor → PNG/WebP otomatik dönüştürülüyor (Pillow).
- **Publish retry yok:** `media_publish` başarısız görünüp aslında geçmiş olabilir → duplicate post riski. Container oluşturma ve poll retry edilebilir, publish edilemez.
- **Yarım kopyalanan dosya:** watchdog dosyayı kopyalanırken yakalar. Boyut iki ölçümde sabitlenene kadar bekleniyor.
- **Container ömrü:** 24 saat sonra düşüyor, `EXPIRED` durumu kontrol ediliyor.
- **PPA:** Sayfanda Page Publishing Authorization isteniyorsa tamamlanmadan yayın reddedilir — Sayfayı açınca önden hallet.
- **Geliştirme modu:** kendi hesabınla test için App Review gerekmiyor, tester rolü yeterli.

---

## 5. Teknoloji

Python 3.12 — `watchdog`, `httpx`, `APScheduler`, `sqlite3`, `pydantic-settings`, `PyYAML`, `Pillow`, `pytest`.
ffmpeg zorunlu değil (varsa video doğrulama için kullanılıyor).

---

## 6. Durum

| Faz | İş | Durum |
|---|---|---|
| 0 | IG Business hesap · FB Sayfası · Meta app · token | **sende** |
| 1 | Temel: modeller, ayarlar, db | kodlanıyor |
| 2 | Modüller: instagram, medya, izleyici, zamanlayıcı, arşiv | kodlanıyor |
| 3 | main.py, testler, BASLA.md | kodlanıyor |
| 4 | Çok açılı inceleme + düzeltme | kodlanıyor |

Token gelene kadar her şey sahte (mock) API ile test ediliyor. Sen `.env`'i doldurunca çalışır hale geliyor.

---

## 7. Senin yapman gerekenler

1. Instagram hesabını **Business**'a çevir
2. **Facebook Sayfası** aç, Instagram hesabını ona bağla (+ PPA varsa tamamla)
3. https://developers.facebook.com → app oluştur → Instagram ürününü ekle
4. İzinler: `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`
5. Uzun ömürlü **Page access token** + **Instagram Business Account ID** al
6. `config/.env` dosyasına yaz (şablonu `.env.ornek`'te, adımlar `BASLA.md`'de)
