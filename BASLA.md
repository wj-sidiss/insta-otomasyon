# BASLA - Kurulum ve Kullanim Rehberi

Bu program, bilgisayarinizdaki bir klasore attiginiz videolari Instagram'a
**Reels** veya **Story** olarak otomatik yayinlar.

Calisma mantigi cok basit:

```
gelen/reels/ klasorune video at   ->  program gorur  ->  siraya koyar  ->  saati gelince yayinlar
gelen/story/ klasorune video at   ->  program gorur  ->  siraya koyar  ->  saati gelince yayinlar
```

Yayinlanan dosya `yayinlandi/` klasorune, yayinlanamayan dosya `hata/`
klasorune tasinir. Hicbir dosya bulut depolamaya yuklenmez; videolar
dogrudan bilgisayarinizdan Meta'nin sunucusuna gider.

> Not: Butun metinler Turkce ama Turkce karakter kullanilmaz (i, s, g, u, o, c).
> Bunun sebebi Windows konsolunda yazi bozulmasini onlemektir.

---

## Icindekiler

1. [Neye ihtiyaciniz var](#1-neye-ihtiyaciniz-var)
2. [Kurulum (venv + pip)](#2-kurulum-venv--pip)
3. [Meta tarafinda hazirlik](#3-meta-tarafinda-hazirlik)
4. [config/.env dosyasini doldurma](#4-configenv-dosyasini-doldurma)
5. [config/ayarlar.yaml ile davranisi ayarlama](#5-configayarlaryaml-ile-davranisi-ayarlama)
6. [Programi calistirma](#6-programi-calistirma)
7. [Klasore dosya atma kurallari](#7-klasore-dosya-atma-kurallari)
8. [Dosya adi ornekleri](#8-dosya-adi-ornekleri)
9. [Aciklama (caption) yazma](#9-aciklama-caption-yazma)
10. [Klasor haritasi](#10-klasor-haritasi)
11. [Sorun giderme](#11-sorun-giderme)
12. [Testleri calistirma](#12-testleri-calistirma)

---

## 1. Neye ihtiyaciniz var

| Gereken | Zorunlu mu | Aciklama |
|---|---|---|
| Python 3.12 veya ustu | EVET | `python --version` ile kontrol edin |
| Bir **Facebook Sayfasi** | EVET | Kisisel profil yetmez |
| **Instagram isletme hesabi** | EVET | Instagram > Ayarlar > Hesap turu > Isletme/Icerik ureticisi |
| Instagram hesabinin Sayfaya bagli olmasi | EVET | Sayfa ayarlarindan baglanir |
| Meta gelistirici uygulamasi | EVET | Token uretmek icin |
| ffmpeg / ffprobe | HAYIR (tavsiye edilir) | Video dogrulama ve gorsel -> video cevrimi icin |

**ffmpeg neden tavsiye edilir?**
- ffprobe varsa video suresi, en-boy orani, kare hizi ve kodek yayin
  ONCESINDE kontrol edilir. Yoksa uyumsuz dosya ancak Meta tarafinda hata
  verir ve bir kapsayici (container) hakki bosa gider.
- ffmpeg varsa `gelen/story/` klasorune attiginiz JPEG/PNG gorseller kisa bir
  videoya cevrilip Story olarak yayinlanabilir. Yoksa gorsel yayinlanamaz.

Kurulum (Windows):

```powershell
winget install Gyan.FFmpeg
```

Kurduktan sonra **yeni bir PowerShell penceresi** acin (PATH guncellensin).
Kontrol:

```powershell
ffmpeg -version
ffprobe -version
```

---

## 2. Kurulum (venv + pip)

Proje klasorunde PowerShell acin:

```powershell
cd insta-otomasyon

# 1) Sanal ortam olustur (bir kez yapilir)
python -m venv .venv

# 2) Sanal ortami etkinlestir (her yeni pencerede yapilir)
.\.venv\Scripts\Activate.ps1

# 3) Bagimliliklari kur
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Etkinlestirme calismazsa (PowerShell calistirma politikasi engelliyorsa):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Komut isteminin basinda `(.venv)` yazdigini gorunce kurulum tamamdir.

---

## 3. Meta tarafinda hazirlik

Bu bolum bir kez yapilir ve en cok vakit alan kisimdir. Sonunda elinizde
**uzun omurlu bir Facebook Sayfa erisim anahtari** (Page access token) ve
**Instagram isletme hesabi kimligi** olacak.

### 3.1 Uygulama olusturun

1. <https://developers.facebook.com/apps> adresine gidin.
2. **Create App** deyin, kullanim amaci olarak **Other** -> tip olarak
   **Business** secin.
3. Uygulama panelinde **App ID** ve **App Secret** degerlerini not edin
   (Settings > Basic). Bunlar zorunlu degildir ama token saglik kontrolu
   icin cok isinize yarar.

### 3.2 Izinleri secip kullanici anahtari uretin

1. Sol menuden **Tools > Graph API Explorer** acin
   (<https://developers.facebook.com/tools/explorer>).
2. Sag ustten kendi uygulamanizi secin.
3. **Permissions** kutusuna su uc izni ekleyin:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_read_engagement`

   > Sayfa rolunuz Business Manager uzerinden verildiyse `ads_management` ve
   > `ads_read` izinleri de gerekebilir.
4. **Generate Access Token** deyin ve Facebook hesabinizla onaylayin.
5. Ciktiyi kopyalayin. Bu **KISA OMURLU** bir kullanici anahtaridir
   (yaklasik 1-2 saat gecerli).

### 3.3 Kisa omurlu anahtari UZUN omurluye cevirin

Bu adimi atlamayin. Atlarsaniz otomasyon birkac saat sonra sessizce durur.

Graph API Explorer'da adres cubuguna su istegi yazip **Submit** deyin
(bastaki `/` dahil):

```
/oauth/access_token
  ?grant_type=fb_exchange_token
  &client_id=<APP_ID>
  &client_secret=<APP_SECRET>
  &fb_exchange_token=<KISA_OMURLU_TOKEN>
```

Donen `access_token` degeri uzun omurlu **kullanici** anahtaridir (60 gun).

### 3.4 Sayfa anahtarini alin

Simdi **uzun omurlu** anahtari kullanarak su istegi yapin:

```
/me/accounts?access_token=<UZUN_OMURLU_KULLANICI_TOKENI>
```

Donen listede sayfanizi bulun ve icindeki `access_token` degerini kopyalayin.
**Iste `FB_SAYFA_TOKEN` budur.**

> DIKKAT: `/me/accounts` cagrisini KISA omurlu anahtarla yaparsaniz donen
> sayfa anahtari da kisa omurlu olur. Once 3.3 adimini yapmis olmalisiniz.

### 3.5 Instagram hesap kimligini alin

Ayni listede sayfanizin `id` degerini gorursunuz. Onu kullanarak:

```
/<SAYFA_ID>?fields=instagram_business_account&access_token=<SAYFA_TOKENI>
```

Yanit soyle gorunur:

```json
{
  "instagram_business_account": { "id": "17841400000000000" },
  "id": "1010101010"
}
```

Icerideki `id` (17 haneli sayi) **`IG_HESAP_ID`** degeridir.

### 3.6 Sayfa Yayin Yetkilendirmesi (PPA)

Facebook Sayfa ayarlarinda **Page Publishing Authorization** tamamlanmamissa
yayin istekleri reddedilir. Sayfa > Ayarlar > Sayfa Yayin Yetkilendirmesi
adimini bitirin.

---

## 4. config/.env dosyasini doldurma

Ornek dosyayi kopyalayin:

```powershell
Copy-Item config\.env.ornek config\.env
notepad config\.env
```

Doldurulacak alanlar:

```
FB_SAYFA_TOKEN=EAAG...buraya_3.4_adiminda_aldiginiz_sayfa_tokeni
IG_HESAP_ID=17841400000000000
FB_UYGULAMA_ID=1234567890
FB_UYGULAMA_SIRRI=abcdef0123456789
API_SURUMU=v23.0
```

| Alan | Zorunlu | Nereden gelir |
|---|---|---|
| `FB_SAYFA_TOKEN` | EVET | Adim 3.4 |
| `IG_HESAP_ID` | EVET | Adim 3.5 |
| `FB_UYGULAMA_ID` | Hayir | Adim 3.1 (Settings > Basic) |
| `FB_UYGULAMA_SIRRI` | Hayir | Adim 3.1 (Settings > Basic) |
| `API_SURUMU` | Hayir | Varsayilan `v23.0` |

`FB_UYGULAMA_ID` ve `FB_UYGULAMA_SIRRI` bos birakilirsa program calisir, ama
haftalik **token saglik kontrolu** yapilamaz; anahtarinizin suresi dolmak
uzereyken uyarilmazsiniz.

> `config/.env` dosyasi `.gitignore` ile korunur, depoya girmez.
> Bu dosyayi kimseyle paylasmayin: icindeki anahtar, hesabiniz adina
> Instagram'a icerik yayinlama yetkisidir.

---

## 5. config/ayarlar.yaml ile davranisi ayarlama

Sirlar `.env` dosyasinda, **davranis** ise `config/ayarlar.yaml` dosyasindadir.
Dosya yoksa ornekten kopyalayin:

```powershell
Copy-Item config\ayarlar.ornek.yaml config\ayarlar.yaml
```

En cok degistirilen ayarlar:

```yaml
zamanlama:
  zaman_dilimi: "Europe/Istanbul"
  yayin_slotlari:          # dosya adinda saat yoksa bu saatler kullanilir
    - "12:00"
    - "19:00"
  tarama_araligi_sn: 60    # kuyruk kac saniyede bir kontrol edilsin

yayin:
  gunluk_limit: 25         # 24 saatte en fazla kac yayin (kendi limitiniz)
  azami_deneme: 5          # gecici hatada bir gorev kac kez denensin
  akisa_paylas: true       # Reels ayrica akista da gorunsun mu
  varsayilan_aciklama: ""  # yaninda .txt yoksa kullanilacak metin
```

> Yanlis yazilmis bir anahtar (ornek `gunluk_limitt`) sessizce yok sayilmaz;
> program acilista Turkce hata verir. Bu bilincli bir karardir.

---

## 6. Programi calistirma

### 6.1 Once kuru calistirma (deneme kipi)

Hicbir yayin yapmadan her seyin dogru kurulup kurulmadigini gosterir:

```powershell
python -m src.main --kuru-calistirma
```

Ekranda sunlari gorursunuz:
- ayarlarin ozeti (anahtar maskelenmis halde),
- ffmpeg/ffprobe/watchdog var mi,
- gelen klasorlerinde bekleyen dosyalar ve **hangi saate planlanacaklari**,
- kuyruktaki gorevler,
- yerel yayin sayaci.

Bu kipte Meta'ya **tek bir istek bile** gonderilmez.

### 6.2 Gercek calistirma

```powershell
python -m src.main
```

Program acilir, klasorleri izlemeye baslar ve saati gelen videolari yayinlar.
Pencereyi acik birakin. Kapatirsaniz otomasyon durur (kuyruk kaybolmaz).

### 6.3 Durdurma

**Ctrl+C**'ye basin. Program:
1. yeni dosya almayi durdurur,
2. **o an devam eden yayin varsa bitmesini bekler**,
3. gecici dosyalari temizler ve kapanir.

Beklemek istemezseniz **Ctrl+C**'ye ikinci kez basin. Yarim kalan gorev
kuyrukta korunur; program tekrar acildiginda kaldigi adimdan devam eder.

### 6.4 Faydali secenekler

| Komut | Ne yapar |
|---|---|
| `python -m src.main --kuru-calistirma` | Yayin yapmadan durum raporu |
| `python -m src.main --seviye DEBUG` | Ayrintili gunluk (sorun ararken) |
| `python -m src.main --yoklama` | Klasor olaylari yerine periyodik tarama (ag surucusu / paylasim klasoru icin) |
| `python -m src.izleyici --tara` | Klasorleri bir kez tarayip cik |

---

## 7. Klasore dosya atma kurallari

| Klasor | Ne atilir | Yayin tipi |
|---|---|---|
| `gelen/reels/` | `.mp4`, `.mov` | Reels |
| `gelen/story/` | `.mp4`, `.mov`, `.jpg`, `.jpeg`, `.png`, `.webp` | Story |

### Video sinirlari

| | Reels | Story |
|---|---|---|
| Bicim | MP4 / MOV | MP4 / MOV |
| Sure | 3 sn - 15 dk | 3 sn - 60 sn |
| Dosya boyutu | en fazla 300 MB | en fazla 100 MB |
| Genislik | en fazla 1920 piksel | en fazla 1920 piksel |
| Kare hizi | 23 - 60 FPS | 23 - 60 FPS |
| Video kodegi | H264 veya HEVC | H264 veya HEVC |
| Ses kodegi | AAC | AAC |
| Onerilen oran | 9:16 (dikey, 1080x1920) | 9:16 (dikey, 1080x1920) |

Videonuz uymuyorsa tek komutla duzeltebilirsiniz:

```powershell
ffmpeg -i girdi.mov -c:v libx264 -c:a aac -movflags +faststart cikti.mp4
```

> `-movflags +faststart` cok onemlidir: `moov` atom dosyanin **basinda**
> olmazsa Meta isleme asamasinda "video format is not supported" (2207026)
> hatasi dondurur. Program bunu kendisi de duzeltmeye calisir (ffmpeg varsa).

### Dikkat edilecekler

- **Kopyalama bitene kadar bekleyin.** Program dosya boyutu sabitlenene kadar
  bekler, yarim kopyalanmis dosyayi kuyruga almaz. Yine de buyuk dosyalari
  once baska bir klasore kopyalayip sonra tasimak en guvenlisidir.
- **Ayni dosya iki kez kuyruga girmez.** Veritabaninda dosya yolu benzersizdir.
- **Su dosyalar yok sayilir:** `.` `_` `~` ile baslayanlar, `~` ile bitenler,
  `.tmp` `.part` `.crdownload` gibi gecici uzantilar ve `.txt` dosyalari.
- **Reels klasorune gorsel atmayin.** Gorseller yalnizca Story olarak
  yayinlanabilir (ve bunun icin ffmpeg gerekir).

---

## 8. Dosya adi ornekleri

Dosya adinin basina tarih/saat yazarsaniz video **tam o saatte** yayinlanir.
Yazmazsaniz `ayarlar.yaml` icindeki ilk bos yayin slotuna konur.

| Dosya adi | Ne zaman yayinlanir |
|---|---|
| `tanitim.mp4` | Siradaki bos slot (ornek bugun 19:00) |
| `2026-09-25_1830_tanitim.mp4` | 25 Eylul 2026, saat 18:30 |
| `20260925_1830_tanitim.mp4` | Ayni (kisa yazim) |
| `2026-09-25_18-30_tanitim.mp4` | Ayni (tire ile) |
| `2026-09-25_183000_tanitim.mp4` | Ayni (saniye yok sayilir) |
| `2026-09-25_tanitim.mp4` | 25 Eylul 2026, o gunun ilk uygun slotu |
| `2026-09-25.mp4` | Ayni (ad kismi bos olabilir) |
| `2026-13-45_bozuk.mp4` | Tarih gecersiz: uyari verilir, dosya **plansiz** kuyruga alinir |
| `yaz kampanyasi.mp4` | Bosluklu ad sorun degil, siradaki bos slota konur |

Kurallar:
- Saat **24 saat** biciminde yazilir (`0900`, `1830`, `2145`).
- Tarih ve saat arasina `_`, `-`, bosluk veya `T` konabilir.
- Tarihten sonraki kisim serbesttir; sadece aciklama sablonunda `{ad}`
  olarak kullanilir.
- Gecmis bir tarih yazarsaniz dosya atlanmaz; **ilk firsatta** yayinlanir.
- Ayni slota iki gorev konmaz; ikinci gorev bir sonraki slota kayar.

---

## 9. Aciklama (caption) yazma

Videonun yanina **ayni adli bir `.txt` dosyasi** koyun:

```
gelen/reels/
  2026-09-25_1830_tanitim.mp4
  2026-09-25_1830_tanitim.txt     <- aciklama buraya
```

Tarih oneksiz ad da calisir (birden fazla video ayni metni paylasabilir):

```
gelen/reels/
  2026-09-25_1830_tanitim.mp4
  tanitim.txt                     <- bu da bulunur
```

Kurallar:
- Dosya **UTF-8** olmali (Not Defteri varsayilani uygundur).
- En fazla **2200 karakter**; asarsa program kelime sinirindan kirpar.
- En fazla **30 hashtag** ve **20 kullanici etiketi** (`@`) kullanin.
- `.txt` dosyasi kuyruga gorev olarak eklenmez, sadece metin kaynagidir.
- **Story'de aciklama yoktur.** `gelen/story/` icine `.txt` koysaniz bile
  kullanilmaz (Instagram Story'de altyazi alani yoktur).

`.txt` yoksa `ayarlar.yaml` icindeki `varsayilan_aciklama` sablonu kullanilir:

```yaml
yayin:
  varsayilan_aciklama: "{ad} | {tarih} {saat}"
```

Kullanilabilecek alanlar: `{ad}`, `{dosya_adi}`, `{tip}`, `{tarih}`, `{saat}`.

---

## 10. Klasor haritasi

```
insta-otomasyon/
  BASLA.md                 <- bu dosya
  requirements.txt
  kuyruk.db                <- SQLite kuyrugu (silmeyin)
  config/
    .env                   <- SIRLAR (siz olusturursunuz, paylasmayin)
    .env.ornek
    ayarlar.yaml           <- davranis ayarlari
    ayarlar.ornek.yaml
  gelen/
    reels/                 <- Reels videolari buraya
    story/                 <- Story video/gorselleri buraya
  yayinlandi/
    2026-09/               <- basarili dosyalar aya gore arsivlenir
  hata/
    otomasyon.log          <- program gunlugu
    bozuk.mp4              <- yayinlanamayan dosya
    bozuk.log              <- neden yayinlanamadigi (Turkce aciklama + oneri)
  src/
    main.py                <- giris noktasi
    ayarlar.py  modeller.py  db.py
    izleyici.py  zamanlayici.py  instagram.py  medya.py  arsiv.py
  testler/
```

---

## 11. Sorun giderme

### "YAPILANDIRMA EKSIK - PROGRAM BASLATILAMADI"
`config/.env` dosyasi yok veya `FB_SAYFA_TOKEN` / `IG_HESAP_ID` bos.
Ekrandaki adimlari izleyin; ayrinti icin bu rehberin 3. ve 4. bolumu.

### "ERISIM ANAHTARI (TOKEN) GECERSIZ - KUYRUK DURDURULDU"
Anahtarin suresi doldu veya yetki kaldirildi. Sik sebepler:
- Facebook sifresi degistirildi,
- uygulama yetkisi kaldirildi,
- Sayfa rolunuz degisti,
- 90 gunluk veri erisim suresi doldu.

Cozum: 3.2 - 3.4 adimlarini tekrarlayip yeni anahtari `config/.env` icine
yazin ve programi yeniden baslatin. **Kuyruktaki gorevler silinmez.**

### "Yayin yetkisi problemi"
`instagram_basic`, `instagram_content_publish`, `pages_read_engagement`
izinlerinden biri eksik. Ayrica Sayfa Yayin Yetkilendirmesi (PPA)
tamamlanmis olmali (adim 3.6).

### Video "isleniyor" durumunda takildi
Kapsayici (container) **24 saat** gecerlidir. Program her turda durumu tekrar
sorgular. 24 saat dolarsa gorev otomatik olarak bastan alinir.

### "video format is not supported" (2207026)
`moov` atom dosyanin sonunda veya kodek uyumsuz. Duzeltme:

```powershell
ffmpeg -i girdi.mp4 -c copy -movflags +faststart cikti.mp4
```

Olmazsa tamamen yeniden kodlayin:

```powershell
ffmpeg -i girdi.mp4 -c:v libx264 -c:a aac -movflags +faststart cikti.mp4
```

### "Gunluk yayin kotasi doldu"
Bu bir **hata degil**, ertelemedir. Meta'nin kotasi 24 saatlik **kayan**
penceredir, gece yarisi sifirlanmaz. Gorev kuyrukta bekler ve pencere
acilinca kendiliginden yayinlanir.

### Dosya `hata/` klasorune tasindi
Yanindaki `.log` dosyasini acin: icinde Turkce hata aciklamasi, Meta kodu ve
**cozum onerisi** yazar. Duzelttikten sonra dosyayi tekrar `gelen/` klasorune
koymaniz yeterlidir.

### Gorsel Story yayinlanmiyor
ffmpeg kurulu degildir. `winget install Gyan.FFmpeg` ile kurun veya
`gelen/story/` klasorune dogrudan `.mp4` birakin.

### Klasore attigim dosyayi program gormuyor
- Dosya adi `.` `_` `~` ile mi basliyor?
- Uzanti `.mp4` / `.mov` mi?
- Kopyalama bitti mi?
- `--yoklama` secenegini deneyin (ag surucusu veya paylasim klasorunde
  klasor olaylari calismayabilir).
- `python -m src.main --kuru-calistirma` ciktisinda dosya gorunuyor mu?

### Gunlugu okumak
Tum ayrinti `hata/otomasyon.log` icindedir. Erisim anahtarlari otomatik
maskelenir, log dosyasini guvenle paylasabilirsiniz.

---

## 12. Testleri calistirma

```powershell
python -m pip install -r requirements.txt
python -m pytest testler/ -v
```

Testler ag'a **hicbir istek gondermez**; Meta API'si sahte (mock) bir HTTP
istemcisi ile taklit edilir. Gercek `kuyruk.db` ve gercek `gelen/` klasorleri
kullanilmaz, her test gecici bir klasorde calisir.

---

## Kisa ozet (her sey kuruluysa)

```powershell
cd insta-otomasyon
.\.venv\Scripts\Activate.ps1
python -m src.main --kuru-calistirma    # once kontrol
python -m src.main                      # sonra calistir
# videoyu gelen\reels\ veya gelen\story\ klasorune at
# Ctrl+C ile durdur
```
