# -*- coding: ascii -*-
"""Arsivleme ve gunlukleme (loglama) katmani.

Iki isi vardir:

  1) DOSYA ARSIVI
     - basarili_tasi(gorev) -> yayinlandi/YYYY-MM/ altina tarih damgali isimle
       tasir; yanindaki aciklama (.txt) dosyasini da birlikte tasir.
     - hatali_tasi(gorev, hata) -> hata/ altina tasir ve yanina ayni isimli
       .log dosyasi yazar (Turkce aciklama + zaman + deneme sayisi + oneri).
     - Isim cakismasinda USTUNE YAZILMAZ; "-1", "-2" eki verilir.
     - Windows'ta dosya baska bir surec (virus tarayici, medya oynatici,
       Explorer onizlemesi) tarafindan kilitli olabilir; tasima kisa araliklarla
       birkac kez tekrar denenir.

  2) GUNLUKLEME
     - gunlugu_yapilandir(ayar) -> konsol + dondurulen dosya logu
       (varsayilan: hata/otomasyon.log, 5 MB'ta devredilir).
     - Butun mesajlar Turkce ve ASCII'dir; bicimlendirici cikisi ASCII'ye
       zorlar, boylece Windows konsolunda encoding hatasi olusmaz.
     - Log satirlarindaki erisim anahtarlari (access_token=..., OAuth ...,
       Bearer ..., EAA... ile baslayan token'lar) otomatik maskelenir.

Baska modullerin arayuzu degistirilmemistir; bu modul modeller/ayarlar/db
katmanlarini yalnizca KULLANIR.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from src.ayarlar import Ayarlar, ayarlari_yukle
from src.db import VeriTabaniYolu, dosya_yolunu_guncelle
from src.modeller import Gorev, YayinHatasi, simdi

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

#: Projedeki kok gunlukcu adi. Diger moduller gunlukcu_al("instagram") gibi
#: cagirarak bunun alt gunlukculerini kullanir.
GUNLUKCU_ADI: Final[str] = "insta"

#: Kilitli dosya icin tasima/yazma deneme sayisi ve bekleme temeli (saniye).
TASIMA_DENEME: Final[int] = 5
TASIMA_BEKLEME_SN: Final[float] = 0.6

#: Arsiv dosya adindaki tarih damgasi bicimi (yerel zaman dilimi).
DAMGA_BICIMI: Final[str] = "%Y%m%d-%H%M%S"

#: Ayni klasorde en fazla kac "-N" eki denenir (sonsuz donguye karsi).
AZAMI_CAKISMA: Final[int] = 9999

_gunlukcu = logging.getLogger(GUNLUKCU_ADI + ".arsiv")


# ---------------------------------------------------------------------------
# Sir maskeleme - token asla log'a veya .log dosyasina duz yazilmaz
# ---------------------------------------------------------------------------

_SIR_DESENLERI: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(access_token=)([^\s&\"']{6,})"), r"\1<gizli>"),
    (re.compile(r"(?i)(authorization:\s*)(oauth|bearer)(\s+)([^\s\"']{6,})",),
     r"\1\2\3<gizli>"),
    (re.compile(r"(?i)\b(oauth|bearer)(\s+)([A-Za-z0-9_\-\.]{12,})"), r"\1\2<gizli>"),
    (re.compile(r"\bEAA[A-Za-z0-9_\-]{10,}"), "<gizli-token>"),
    (re.compile(r"(?i)(client_secret=)([^\s&\"']{6,})"), r"\1<gizli>"),
)


def sirlari_maskele(metin: str) -> str:
    """Metindeki erisim anahtarlarini/sirlari maskeler (log ve .log dosyasi icin)."""
    sonuc = metin
    for desen, yerine in _SIR_DESENLERI:
        sonuc = desen.sub(yerine, sonuc)
    return sonuc


def ascii_yap(metin: str) -> str:
    """Turkce/Unicode karakterleri ASCII'ye indirger (konsol encoding korumasi)."""
    return metin.encode("ascii", "backslashreplace").decode("ascii")


# ---------------------------------------------------------------------------
# Gunlukleme yapilandirmasi
# ---------------------------------------------------------------------------


class AsciiBicimlendirici(logging.Formatter):
    """Cikisi once sir maskeler, sonra ASCII'ye zorlar."""

    def format(self, kayit: logging.LogRecord) -> str:  # noqa: A003 - logging API
        return ascii_yap(sirlari_maskele(super().format(kayit)))


def _konsol_akisini_hazirla() -> None:
    """Windows konsolunda beklenmedik bir Unicode gelirse patlamasin."""
    for akis in (sys.stdout, sys.stderr):
        yeniden_ayarla = getattr(akis, "reconfigure", None)
        if yeniden_ayarla is None:
            continue
        try:
            yeniden_ayarla(errors="backslashreplace")
        except (ValueError, OSError):
            # Yonlendirilmis veya ozel bir akis olabilir; sessizce gec.
            pass


def gunlugu_yapilandir(
    ayar: Ayarlar | None = None,
    *,
    seviye: str | int | None = None,
    konsol: bool = True,
    dosya: bool = True,
    zorla: bool = False,
) -> logging.Logger:
    """Kok gunlukcuyu yapilandirir: konsol + dondurulen dosya logu.

    Parametreler:
        ayar    : Ayarlar nesnesi (verilmezse config'ten yuklenir)
        seviye  : "DEBUG"/"INFO"/... veya logging sabiti; verilmezse ayarlardan
        konsol  : konsola yazilsin mi
        dosya   : hata/otomasyon.log dosyasina yazilsin mi
        zorla   : True ise mevcut yoneticiler (handler) silinip yeniden kurulur

    Doner: yapilandirilmis kok gunlukcu ("insta").
    """
    ayar = ayar or ayarlari_yukle()
    gunlukcu = logging.getLogger(GUNLUKCU_ADI)
    gunlukcu.propagate = False  # kok logger'a ikinci kez yazdirma

    secilen_seviye = seviye if seviye is not None else ayar.kayit.seviye
    if isinstance(secilen_seviye, str):
        secilen_seviye = logging.getLevelName(secilen_seviye.strip().upper())
    if not isinstance(secilen_seviye, int):
        secilen_seviye = logging.INFO
    gunlukcu.setLevel(secilen_seviye)

    if gunlukcu.handlers and not zorla:
        return gunlukcu  # zaten kurulmus, ikinci kez kurma (cift satir olmasin)

    for eski in list(gunlukcu.handlers):
        gunlukcu.removeHandler(eski)
        try:
            eski.close()
        except OSError:
            pass

    bicim = AsciiBicimlendirici(
        fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if konsol:
        _konsol_akisini_hazirla()
        konsol_yonetici = logging.StreamHandler(sys.stdout)
        konsol_yonetici.setFormatter(bicim)
        konsol_yonetici.setLevel(secilen_seviye)
        gunlukcu.addHandler(konsol_yonetici)

    if dosya:
        log_yolu = Path(ayar.kayit.dosya)
        try:
            log_yolu.parent.mkdir(parents=True, exist_ok=True)
            dosya_yonetici = logging.handlers.RotatingFileHandler(
                filename=str(log_yolu),
                maxBytes=int(ayar.kayit.azami_bayt),
                backupCount=int(ayar.kayit.yedek_sayisi),
                encoding="ascii",
                errors="backslashreplace",
                delay=True,
            )
            dosya_yonetici.setFormatter(bicim)
            dosya_yonetici.setLevel(secilen_seviye)
            gunlukcu.addHandler(dosya_yonetici)
        except OSError as hata:
            # Log dosyasi acilamadiysa program durmamali; konsola uyar.
            gunlukcu.warning(
                "Gunluk dosyasi acilamadi (%s): %s. Sadece konsola yazilacak.",
                log_yolu,
                hata,
            )

    return gunlukcu


def gunlukcu_al(ad: str | None = None) -> logging.Logger:
    """Alt gunlukcu dondurur. Ornek: gunlukcu_al("instagram") -> 'insta.instagram'."""
    if not ad:
        return logging.getLogger(GUNLUKCU_ADI)
    temiz = str(ad).strip()
    if temiz == GUNLUKCU_ADI or temiz.startswith(GUNLUKCU_ADI + "."):
        return logging.getLogger(temiz)
    return logging.getLogger(f"{GUNLUKCU_ADI}.{temiz}")


# ---------------------------------------------------------------------------
# Dosya sistemi yardimcilari
# ---------------------------------------------------------------------------


def _yerel(an: datetime, ayar: Ayarlar) -> datetime:
    """UTC zamani kullaniciya gorunen yerel zamana cevirir."""
    return an.astimezone(ayar.zaman_dilimi)


def tarih_damgasi(an: datetime, ayar: Ayarlar) -> str:
    """Arsiv dosya adinda kullanilan tarih damgasi (yerel zaman)."""
    return _yerel(an, ayar).strftime(DAMGA_BICIMI)


def arsiv_klasoru(ayar: Ayarlar, an: datetime | None = None) -> Path:
    """yayinlandi/YYYY-MM klasoru (yerel zamana gore, yoksa olusturulur)."""
    zaman = an or simdi()
    klasor = Path(ayar.klasorler.yayinlandi) / _yerel(zaman, ayar).strftime("%Y-%m")
    klasor.mkdir(parents=True, exist_ok=True)
    return klasor


def bos_hedef_uret(klasor: Path, ad: str) -> Path:
    """Cakismayan bir hedef yol uretir: ad.mp4 -> ad-1.mp4 -> ad-2.mp4 ...

    USTUNE YAZMAZ. Mevcut dosyalara asla dokunulmaz.
    """
    hedef = klasor / ad
    if not hedef.exists():
        return hedef
    govde = hedef.stem
    uzanti = hedef.suffix
    for sayac in range(1, AZAMI_CAKISMA + 1):
        aday = klasor / f"{govde}-{sayac}{uzanti}"
        if not aday.exists():
            return aday
    raise OSError(
        f"Bos bir hedef dosya adi uretilemedi, klasorde cok fazla kopya var: {klasor}"
    )


def guvenli_tasi(
    kaynak: Path,
    hedef: Path,
    *,
    deneme: int = TASIMA_DENEME,
    bekleme_sn: float = TASIMA_BEKLEME_SN,
) -> Path:
    """Dosyayi tasir; Windows kilidine karsi kisa araliklarla tekrar dener.

    Hedefin var olmadigi varsayilir (bos_hedef_uret ile uretilmeli).
    Doner: tasinan dosyanin yeni yolu.
    """
    if not kaynak.exists():
        raise FileNotFoundError(f"Tasinacak dosya bulunamadi: {kaynak}")
    hedef.parent.mkdir(parents=True, exist_ok=True)

    son_hata: Exception | None = None
    for sira in range(1, max(1, deneme) + 1):
        try:
            shutil.move(str(kaynak), str(hedef))
            return hedef
        except FileNotFoundError:
            raise
        except (PermissionError, OSError) as hata:
            son_hata = hata
            if sira >= deneme:
                break
            _gunlukcu.warning(
                "Dosya su an kilitli olabilir, tekrar denenecek (%d/%d): %s (%s)",
                sira,
                deneme,
                kaynak.name,
                hata,
            )
            time.sleep(bekleme_sn * sira)

    raise OSError(
        f"Dosya tasinamadi (baska bir program kullaniyor olabilir): "
        f"{kaynak} -> {hedef} ({son_hata})"
    )


def _metni_yaz(
    yol: Path,
    metin: str,
    *,
    deneme: int = TASIMA_DENEME,
    bekleme_sn: float = TASIMA_BEKLEME_SN,
) -> bool:
    """Metni ASCII olarak yazar; kilit durumunda tekrar dener. Doner: basarili mi."""
    yol.parent.mkdir(parents=True, exist_ok=True)
    for sira in range(1, max(1, deneme) + 1):
        try:
            yol.write_text(metin, encoding="ascii", errors="backslashreplace")
            return True
        except OSError as hata:
            if sira >= deneme:
                _gunlukcu.error("Aciklama dosyasi yazilamadi: %s (%s)", yol, hata)
                return False
            time.sleep(bekleme_sn * sira)
    return False


def _aciklama_uzantisi(ayar: Ayarlar) -> str:
    """config'teki aciklama uzantisini nokta ile basliyor hale getirir."""
    uzanti = str(ayar.dosya.aciklama_uzantisi or ".txt").strip().lower()
    return uzanti if uzanti.startswith(".") else "." + uzanti


def _yan_dosyayi_tasi(kaynak_medya: Path, hedef_medya: Path, ayar: Ayarlar) -> Path | None:
    """Medyanin yanindaki aciklama (.txt) dosyasini ayni klasore tasir.

    Doner: tasinan .txt yolu, yoksa None.
    """
    uzanti = _aciklama_uzantisi(ayar)
    yan_kaynak = kaynak_medya.with_suffix(uzanti)
    if not yan_kaynak.exists():
        return None
    yan_hedef = bos_hedef_uret(hedef_medya.parent, hedef_medya.stem + uzanti)
    try:
        return guvenli_tasi(yan_kaynak, yan_hedef)
    except OSError as hata:
        _gunlukcu.warning(
            "Aciklama dosyasi tasinamadi, yerinde birakildi: %s (%s)", yan_kaynak, hata
        )
        return None


def _db_yolunu_guncelle(
    db_yolu: VeriTabaniYolu | None, gorev: Gorev, yeni_yol: Path
) -> None:
    """Dosya tasindiysa kuyruktaki dosya_yolu kaydini gunceller (istege bagli)."""
    if db_yolu is None or gorev.id is None:
        return
    try:
        dosya_yolunu_guncelle(db_yolu, gorev.id, yeni_yol)
    except Exception as hata:  # veritabani hatasi arsivlemeyi bozmasin
        _gunlukcu.warning(
            "Kuyruktaki dosya yolu guncellenemedi (gorev #%s): %s", gorev.id, hata
        )


# ---------------------------------------------------------------------------
# 1) Basarili yayin -> arsive tasi
# ---------------------------------------------------------------------------


def basarili_tasi(
    gorev: Gorev,
    ayar: Ayarlar | None = None,
    *,
    db_yolu: VeriTabaniYolu | None = None,
) -> Path:
    """Yayinlanan dosyayi yayinlandi/YYYY-MM/ altina tarih damgali isimle tasir.

    - Ad bicimi: <YYYYAAGG-SSDDSS>_<ozgun_ad>.<uzanti>
      Ornek: 20260925-183000_tanitim.mp4
    - Yanindaki aciklama dosyasi (.txt) da ayni isimle tasinir.
    - Isim cakismasinda "-1", "-2" eki verilir; mevcut dosyanin USTUNE YAZILMAZ.
    - db_yolu verilirse kuyruktaki dosya_yolu kaydi yeni konuma guncellenir.

    Doner: dosyanin yeni yolu. Dosya bulunamazsa (daha once tasinmis olabilir)
    eski yol dondurulur ve uyari loglanir.
    """
    ayar = ayar or ayarlari_yukle()
    kaynak = Path(gorev.dosya_yolu)
    an = gorev.yayin_zamani or simdi()

    if not kaynak.exists():
        _gunlukcu.warning(
            "Arsivlenecek dosya bulunamadi, atlandi (daha once tasinmis olabilir): %s",
            kaynak,
        )
        return kaynak

    klasor = arsiv_klasoru(ayar, an)
    hedef = bos_hedef_uret(klasor, f"{tarih_damgasi(an, ayar)}_{kaynak.name}")

    yeni_yol = guvenli_tasi(kaynak, hedef)
    yan_yol = _yan_dosyayi_tasi(kaynak, yeni_yol, ayar)
    _db_yolunu_guncelle(db_yolu, gorev, yeni_yol)

    _gunlukcu.info(
        "Arsivlendi: %s -> %s%s%s",
        kaynak.name,
        yeni_yol,
        (f" (aciklama: {yan_yol.name})" if yan_yol else ""),
        (f" | baglanti: {gorev.permalink}" if gorev.permalink else ""),
    )
    return yeni_yol


# ---------------------------------------------------------------------------
# 2) Hatali is -> hata klasorune tasi + .log yaz
# ---------------------------------------------------------------------------

#: Meta alt kodlarina gore Turkce cozum onerileri.
_ONERILER: Final[dict[int, str]] = {
    2207026: (
        "Video formati desteklenmiyor. MP4 veya MOV, H264/HEVC video + AAC ses "
        "olmali ve moov atom dosyanin BASINDA bulunmali. Duzeltme komutu: "
        "ffmpeg -i girdi.mp4 -c copy -movflags +faststart cikti.mp4"
    ),
    2207009: (
        "En-boy orani kabul edilmedi. Dikey 9:16 (ornek 1080x1920) onerilir."
    ),
    2207010: "Aciklama (caption) 2200 karakteri asiyor, kisaltin.",
    2207040: "Aciklamada 20'den fazla @ etiketi var, azaltin.",
    2207004: "Gorsel 8 MB'dan buyuk, kucultun.",
    2207005: "Gorsel formati desteklenmiyor. Sadece JPEG kabul edilir (RGB/sRGB).",
    2207023: "Bilinmeyen medya tipi gonderildi (REELS veya STORIES olmali).",
    2207042: (
        "Gunluk yayin kotasi doldu. Bu bir kalici hata degildir; kota penceresi "
        "acilinca dosyayi tekrar gelen klasorune koyabilirsiniz."
    ),
    2207050: (
        "Instagram hesabi kisitli. Instagram uygulamasindan hesap durumunu "
        "kontrol edin."
    ),
    2207051: (
        "Spam korumasi devreye girdi. Bir sure yayin yapmayin; tekrar denemek "
        "kisiti derinlestirebilir."
    ),
    2207057: "Kapak kare konumu (thumb_offset) video suresinin disinda.",
    2207027: (
        "Medya yayina hazir degildi. Kapsayici durumu FINISHED olmadan yayin "
        "denendi; bu genellikle kendiliginden duzelir."
    ),
    2207020: "Medyanin suresi doldu. Yeni kapsayici ile bastan denenmelidir.",
    2207003: "Medya indirme cok uzun surdu. Baglanti yavaslamis olabilir.",
}

_VARSAYILAN_ONERI: Final[str] = (
    "Ayrintili inceleme icin hata/otomasyon.log dosyasina bakin. Sorun devam "
    "ederse dosyayi ffmpeg ile yeniden kodlayip tekrar gelen klasorune koyun."
)

_TOKEN_ONERISI: Final[str] = (
    "Erisim anahtari gecersiz veya suresi dolmus. Facebook ile yeniden giris "
    "yapip config/.env icindeki FB_SAYFA_TOKEN degerini guncelleyin."
)

_IZIN_ONERISI: Final[str] = (
    "Yayin yetkisi problemi. Gerekli izinler: instagram_basic, "
    "instagram_content_publish, pages_read_engagement (Sayfa rolu Business "
    "Manager uzerinden verildiyse ads_management/ads_read de gerekir). Ayrica "
    "Sayfa Yayin Yetkilendirmesi (PPA) tamamlanmis olmalidir."
)


def _oneri_uret(hata: BaseException) -> str:
    """Hata kodundan Turkce cozum onerisi uretir."""
    if isinstance(hata, YayinHatasi):
        if hata.alt_kod and int(hata.alt_kod) in _ONERILER:
            return _ONERILER[int(hata.alt_kod)]
        if hata.kod in (190, 102, 467):
            return _TOKEN_ONERISI
        if hata.kod in (10, 200, 299, 9053):
            return _IZIN_ONERISI
        if "page publishing authorization" in str(hata.mesaj).lower():
            return _IZIN_ONERISI
    if isinstance(hata, FileNotFoundError):
        return "Dosya bulunamadi. Yayin sirasinda silinmis veya tasinmis olabilir."
    if isinstance(hata, (TimeoutError, ConnectionError)):
        return (
            "Ag baglantisi sorunu. Internet baglantisini kontrol edip dosyayi "
            "tekrar gelen klasorune koyun."
        )
    return _VARSAYILAN_ONERI


def _hata_raporu(
    gorev: Gorev,
    hata: BaseException,
    ayar: Ayarlar,
    *,
    medya_yolu: Path,
    tasindi: bool,
) -> str:
    """hata/ klasorune yazilacak .log dosyasinin Turkce icerigini uretir."""
    an = simdi()
    yerel = _yerel(an, ayar)
    kalici = bool(getattr(hata, "kalici", False))

    satirlar: list[str] = [
        "Instagram yayin hatasi",
        "=" * 60,
        f"Zaman (yerel)  : {yerel.strftime('%Y-%m-%d %H:%M:%S')} "
        f"({ayar.zamanlama.zaman_dilimi})",
        f"Zaman (UTC)    : {an.isoformat()}",
        f"Gorev          : {gorev.ozet()}",
        f"Medya tipi     : {gorev.medya_tipi}",
        f"Ozgun dosya    : {gorev.dosya_yolu}",
        f"Yeni konum     : {medya_yolu if tasindi else '(dosya tasinmadi)'}",
        f"Deneme sayisi  : {gorev.deneme_sayisi} / {ayar.yayin.azami_deneme}",
        f"Hata sinifi    : {type(hata).__name__}",
        f"Tekrar denenir : {'HAYIR (kalici hata)' if kalici else 'EVET (gecici hata)'}",
    ]

    if isinstance(hata, YayinHatasi):
        satirlar.append(f"Meta kodu      : {hata.kod if hata.kod is not None else '-'}")
        satirlar.append(f"Meta alt kodu  : {hata.alt_kod or '-'}")
        satirlar.append(f"Izleme no      : {hata.iz_no or '-'}  (fbtrace_id)")

    satirlar.extend(
        [
            "",
            "Hata mesaji:",
            f"  {hata}",
            "",
            "Oneri:",
            f"  {_oneri_uret(hata)}",
        ]
    )

    if gorev.container_id:
        satirlar.extend(
            [
                "",
                f"Kapsayici (container) ID : {gorev.container_id}",
                "  Not: Kapsayici olusturulduktan 24 saat sonra gecersiz olur.",
            ]
        )

    izler = "".join(
        traceback.format_exception(type(hata), hata, hata.__traceback__)
    ).strip()
    if izler and izler != "NoneType: None":
        satirlar.extend(["", "Teknik ayrinti (gelistirici icin):", izler])

    satirlar.append("")
    return sirlari_maskele("\n".join(satirlar)) + "\n"


def hatali_tasi(
    gorev: Gorev,
    hata: BaseException,
    ayar: Ayarlar | None = None,
    *,
    db_yolu: VeriTabaniYolu | None = None,
) -> Path:
    """Basarisiz isin dosyasini hata/ klasorune tasir ve yanina .log yazar.

    - .log dosyasi ayni isimlidir (ornek: tanitim.mp4 -> tanitim.log) ve icinde
      TURKCE hata aciklamasi, zaman damgasi, deneme sayisi ve cozum onerisi yer alir.
    - Isim cakismasinda "-1", "-2" eki verilir; USTUNE YAZILMAZ.
    - ayarlar.yaml icindeki yayin.hatali_dosyayi_tasi false ise dosya yerinde
      birakilir, yalnizca .log yazilir.
    - Dosya bulunamazsa yine de .log yazilir (hata kaydi kaybolmasin).

    Doner: dosyanin yeni yolu (tasinmadiysa eski yolu).
    """
    ayar = ayar or ayarlari_yukle()
    kaynak = Path(gorev.dosya_yolu)
    klasor = Path(ayar.klasorler.hata)
    klasor.mkdir(parents=True, exist_ok=True)

    tasinacak = bool(ayar.yayin.hatali_dosyayi_tasi) and kaynak.exists()
    hedef = bos_hedef_uret(klasor, kaynak.name)
    tasindi = False
    yeni_yol = kaynak

    if tasinacak:
        try:
            yeni_yol = guvenli_tasi(kaynak, hedef)
            tasindi = True
            _yan_dosyayi_tasi(kaynak, yeni_yol, ayar)
            _db_yolunu_guncelle(db_yolu, gorev, yeni_yol)
        except OSError as tasima_hatasi:
            _gunlukcu.error(
                "Hatali dosya tasinamadi, yerinde birakildi: %s (%s)",
                kaynak,
                tasima_hatasi,
            )
            yeni_yol = kaynak
    elif not kaynak.exists():
        _gunlukcu.warning(
            "Hatali gorevin dosyasi bulunamadi, sadece hata kaydi yazilacak: %s",
            kaynak,
        )

    # .log dosyasi: tasindiysa yeni adin yaninda, tasinmadiysa hata/ altinda
    temel = (yeni_yol if tasindi else hedef).stem
    log_yolu = bos_hedef_uret(klasor, temel + ".log")
    _metni_yaz(log_yolu, _hata_raporu(gorev, hata, ayar, medya_yolu=yeni_yol, tasindi=tasindi))

    _gunlukcu.error(
        "Yayin basarisiz: %s | hata: %s | kayit: %s",
        gorev.ozet(),
        hata,
        log_yolu.name,
    )
    return yeni_yol


# ---------------------------------------------------------------------------
# Kucuk yardimci: arsiv ozeti (CLI/rapor icin)
# ---------------------------------------------------------------------------


def arsiv_ozeti(ayar: Ayarlar | None = None) -> dict[str, Any]:
    """Arsiv ve hata klasorlerindeki MEDYA dosyasi sayilari (raporlama icin).

    Aciklama (.txt) ve hata kaydi (.log) dosyalari sayilmaz.
    """
    ayar = ayar or ayarlari_yukle()
    yayinlandi = Path(ayar.klasorler.yayinlandi)
    hata_klasoru = Path(ayar.klasorler.hata)
    sayilmayan = {".log", _aciklama_uzantisi(ayar)}

    def _medya_say(klasor: Path) -> int:
        # Nokta ile baslayan dosyalar (.gitkeep gibi) medya degildir.
        return sum(
            1
            for oge in klasor.iterdir()
            if oge.is_file()
            and not oge.name.startswith(".")
            and oge.suffix.lower() not in sayilmayan
        )

    aylar: dict[str, int] = {}
    if yayinlandi.exists():
        for alt in sorted(yayinlandi.iterdir()):
            if alt.is_dir():
                aylar[alt.name] = _medya_say(alt)

    hatali = _medya_say(hata_klasoru) if hata_klasoru.exists() else 0

    return {
        "arsiv_aylari": aylar,
        "arsiv_toplam": sum(aylar.values()),
        "hatali_dosya": hatali,
    }


__all__ = [
    "AsciiBicimlendirici",
    "DAMGA_BICIMI",
    "GUNLUKCU_ADI",
    "TASIMA_BEKLEME_SN",
    "TASIMA_DENEME",
    "arsiv_klasoru",
    "arsiv_ozeti",
    "ascii_yap",
    "basarili_tasi",
    "bos_hedef_uret",
    "gunlugu_yapilandir",
    "gunlukcu_al",
    "guvenli_tasi",
    "hatali_tasi",
    "sirlari_maskele",
    "tarih_damgasi",
]
