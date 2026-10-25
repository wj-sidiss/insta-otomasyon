# -*- coding: ascii -*-
"""Klasor izleme: gelen/reels ve gelen/story icine atilan dosyalari kuyruga alir.

Akis:
    gelen/reels/ veya gelen/story/ icine dosya dusuyor
        -> watchdog olayi (watchdog yoksa dahili yoklama dongusu)
        -> dosya_stabil_mi(): kopyalama bitene kadar beklenir
        -> dosya adi ayristirilir (tarih/saat oneki varsa planlanan zaman olur)
        -> aciklama (caption) yanindaki .txt dosyasindan okunur
        -> db.kuyruga_ekle() ile SQLite kuyruguna yazilir

Tasarim kararlari:
  - Dosya adi ayristirma ve aciklama cozumlemesi SAF fonksiyonlardir
    (dosya_adini_ayristir / sablonu_uygula); disk ve veritabani gerektirmez,
    boylece testlerde dogrudan cagrilabilir.
  - Kararlilik beklemesi watchdog olay ipliginde DEGIL, ayri bir isci ipliginde
    yapilir; aksi halde uzun bekleme diger olaylari geciktirir.
  - '.txt' dosyalari kuyruga GOREV olarak eklenmez; sadece aciklama kaynagidir.
  - Program kapaliyken atilan dosyalar kaybolmasin diye acilista klasorler
    bastan taranir (baslangic_taramasi).
  - watchdog kurulu degilse modul yine de calisir: dahili yoklama (polling)
    dongusune duser. Boylece kurulum tamamlanmadan da test edilebilir.

Turkce karakter kullanilmaz (ASCII), konsol encoding sorunu cikmasin diye.
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Final, Iterable
from zoneinfo import ZoneInfo

from src.ayarlar import Ayarlar, ayarlari_yukle
from src.db import (
    VeriTabaniYolu,
    dosyaya_gore_getir,
    kuyruga_ekle,
    veritabanini_hazirla,
)
from src.modeller import CAPTION_MAKS_KARAKTER, MedyaTipi

#: Proje gunlukcu agaci "insta.*" altindadir (bkz. arsiv.gunlugu_yapilandir).
kayit = logging.getLogger("insta.izleyici")

# ---------------------------------------------------------------------------
# watchdog istege bagli: kurulu degilse dahili yoklama dongusune dusulur
# ---------------------------------------------------------------------------
try:  # pragma: no cover - kurulum ortamina bagli
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    from watchdog.observers.polling import PollingObserver

    WATCHDOG_VAR = True
except ImportError:  # pragma: no cover - watchdog kurulu degil
    FileSystemEventHandler = object  # type: ignore[assignment, misc]
    Observer = None  # type: ignore[assignment]
    PollingObserver = None  # type: ignore[assignment]
    WATCHDOG_VAR = False


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

#: Kopyalama/indirme sirasinda olusan gecici dosyalar - asla kuyruga alinmaz.
GECICI_UZANTILAR: Final[frozenset[str]] = frozenset(
    {
        ".tmp",
        ".temp",
        ".part",
        ".partial",
        ".crdownload",
        ".download",
        ".filepart",
        ".lock",
        ".swp",
        ".bak",
        ".!ut",
    }
)

#: Varsayilan zaman dilimi (ayarlar.yaml bunu ezer).
VARSAYILAN_ZAMAN_DILIMI: Final[str] = "Europe/Istanbul"

#: Aciklama (.txt) dosyasinin videodan sonra kopyalanmasi ihtimaline karsi
#: beklenecek azami ek sure. Kullanici once videoyu sonra metni atabilir.
ACIKLAMA_BEKLEME_SN: Final[float] = 10.0

#: Ayni dosya icin tekrar tekrar log basilmasin diye sogutma suresi.
_SOGUTMA_SN: Final[float] = 120.0

#: "2026-09-25_1830_tanitim.mp4" ve benzeri adlari ayristirir.
#: Desteklenen bicimler:
#:   2026-09-25_1830_ad   20260925_1830_ad   2026-09-25_18-30_ad
#:   2026-09-25_183000_ad (saniye yok sayilir)   2026-09-25_ad (sadece tarih)
_TARIH_DESENI: Final[re.Pattern[str]] = re.compile(
    r"""^
    (?P<yil>\d{4}) [-_.]? (?P<ay>\d{2}) [-_.]? (?P<gun>\d{2})
    (?:
        [ _T-]+ (?P<saat>\d{2}) [-_.:]? (?P<dakika>\d{2})
        (?: [-_.:]? (?P<saniye>\d{2}) )?
    )?
    (?: [ _-]+ (?P<kalan>.+) )?
    $""",
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# 1) Dosya adi ayristirma - SAF fonksiyon (test edilebilir)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdBilgisi:
    """Bir dosya adindan cikarilan bilgiler.

    ham_ad          : "2026-09-25_1830_tanitim.mp4"
    govde           : "tanitim"   (tarih/saat onekinden arindirilmis ad)
    uzanti          : ".mp4"      (kucuk harf)
    planlanan_zaman : tarih VE saat verilmisse timezone-aware datetime, yoksa None
    tarih           : sadece tarih verilmisse o gun (saat ayarlardaki slottan gelir)
    sadece_tarih    : True ise saat bilgisi yoktu
    uyari           : ayristirma sirasinda olusan Turkce uyari (gecersiz tarih vb.)
    """

    ham_ad: str
    govde: str
    uzanti: str
    planlanan_zaman: datetime | None = None
    tarih: date | None = None
    sadece_tarih: bool = False
    uyari: str | None = None

    @property
    def planlanmis_mi(self) -> bool:
        """Dosya adinda bir tarih bilgisi var mi?"""
        return self.planlanan_zaman is not None or self.tarih is not None


def _zaman_dilimi_coz(zaman_dilimi: ZoneInfo | str | None) -> ZoneInfo:
    """str / None / ZoneInfo -> ZoneInfo. None ise Europe/Istanbul."""
    if isinstance(zaman_dilimi, ZoneInfo):
        return zaman_dilimi
    return ZoneInfo(str(zaman_dilimi or VARSAYILAN_ZAMAN_DILIMI))


def dosya_adini_ayristir(
    dosya_adi: str | Path,
    *,
    zaman_dilimi: ZoneInfo | str | None = None,
) -> AdBilgisi:
    """Dosya adini ayristirir. Disk erisimi YOKTUR, saf fonksiyondur.

    Ornekler:
        "tanitim.mp4"
            -> planlanan_zaman=None (siradaki uygun slot kullanilir)
        "2026-09-25_1830_tanitim.mp4"
            -> planlanan_zaman = 2026-09-25 18:30 (yerel zaman dilimi)
        "2026-09-25_tanitim.mp4"
            -> tarih = 2026-09-25, sadece_tarih=True (saat ayarlardaki ilk slot)

    Tarih deseni tutup da gecersiz bir tarih verirse (ornek 2026-13-45) dosya
    plansiz kabul edilir ve 'uyari' alani doldurulur; dosya ASLA atlanmaz.
    """
    ad = Path(str(dosya_adi)).name
    uzanti = Path(ad).suffix.lower()
    govde_ham = ad[: len(ad) - len(uzanti)] if uzanti else ad
    dilim = _zaman_dilimi_coz(zaman_dilimi)

    eslesme = _TARIH_DESENI.match(govde_ham.strip())
    if eslesme is None:
        return AdBilgisi(ham_ad=ad, govde=govde_ham.strip(" _-"), uzanti=uzanti)

    kalan = (eslesme.group("kalan") or "").strip(" _-")
    govde = kalan or govde_ham.strip(" _-")

    try:
        yil = int(eslesme.group("yil"))
        ay = int(eslesme.group("ay"))
        gun = int(eslesme.group("gun"))
        gun_nesnesi = date(yil, ay, gun)
    except ValueError:
        return AdBilgisi(
            ham_ad=ad,
            govde=govde,
            uzanti=uzanti,
            uyari=(
                "Dosya adindaki tarih gecersiz, dosya plansiz kuyruga alinacak: " + ad
            ),
        )

    saat_metni = eslesme.group("saat")
    if saat_metni is None:
        return AdBilgisi(
            ham_ad=ad,
            govde=govde,
            uzanti=uzanti,
            tarih=gun_nesnesi,
            sadece_tarih=True,
        )

    try:
        an = datetime(
            gun_nesnesi.year,
            gun_nesnesi.month,
            gun_nesnesi.day,
            int(saat_metni),
            int(eslesme.group("dakika")),
            tzinfo=dilim,
        )
    except ValueError:
        return AdBilgisi(
            ham_ad=ad,
            govde=govde,
            uzanti=uzanti,
            tarih=gun_nesnesi,
            sadece_tarih=True,
            uyari=(
                "Dosya adindaki saat gecersiz, sadece tarih dikkate alindi: " + ad
            ),
        )

    return AdBilgisi(
        ham_ad=ad,
        govde=govde,
        uzanti=uzanti,
        planlanan_zaman=an,
        tarih=gun_nesnesi,
    )


def planlanan_zamani_hesapla(
    ad_bilgisi: AdBilgisi,
    ayar: Ayarlar,
    *,
    an: datetime | None = None,
) -> datetime | None:
    """Ad bilgisinden kuyruga yazilacak planlanan zamani uretir.

    - Tam tarih + saat varsa o an dondurulur.
    - Sadece tarih varsa o gunun ilk UYGUN yayin slotu secilir (hepsi gecmisse
      gunun ilk slotu kullanilir; gorev ilk firsatta yayinlanir).
    - Tarih yoksa None doner; zamanlayici siradaki uygun slotu kendisi secer.
    """
    if ad_bilgisi.planlanan_zaman is not None:
        return ad_bilgisi.planlanan_zaman
    if ad_bilgisi.tarih is None:
        return None

    dilim = ayar.zaman_dilimi
    su_an = (an or datetime.now(dilim)).astimezone(dilim)
    slotlar = ayar.slot_saatleri() or [(12, 0)]
    adaylar = [
        datetime(
            ad_bilgisi.tarih.year,
            ad_bilgisi.tarih.month,
            ad_bilgisi.tarih.day,
            saat,
            dakika,
            tzinfo=dilim,
        )
        for saat, dakika in slotlar
    ]
    adaylar.sort()
    for aday in adaylar:
        if aday > su_an:
            return aday
    return adaylar[0]


# ---------------------------------------------------------------------------
# 2) Aciklama (caption) cozumleme
# ---------------------------------------------------------------------------


class _GuvenliSozluk(dict):
    """Sablonda tanimsiz alan varsa onu oldugu gibi birakir (hata firlatmaz)."""

    def __missing__(self, anahtar: str) -> str:
        return "{" + anahtar + "}"


def sablonu_uygula(
    sablon: str,
    ad_bilgisi: AdBilgisi,
    medya_tipi: MedyaTipi,
    *,
    zaman_dilimi: ZoneInfo | str | None = None,
) -> str:
    """Varsayilan aciklama sablonundaki alanlari doldurur. Saf fonksiyondur.

    Kullanilabilir alanlar:
        {ad}        -> tarih onekinden arindirilmis dosya adi
        {dosya_adi} -> uzantisiyla birlikte tam dosya adi
        {tip}       -> REELS / STORIES
        {tarih}     -> GG.AA.YYYY  (planlanan zaman yoksa bugun)
        {saat}      -> SS:DD
    """
    dilim = _zaman_dilimi_coz(zaman_dilimi)
    an = ad_bilgisi.planlanan_zaman or datetime.now(dilim)
    an = an.astimezone(dilim)
    alanlar = _GuvenliSozluk(
        ad=ad_bilgisi.govde,
        dosya_adi=ad_bilgisi.ham_ad,
        tip=str(medya_tipi),
        tarih=an.strftime("%d.%m.%Y"),
        saat=an.strftime("%H:%M"),
    )
    try:
        return sablon.format_map(alanlar)
    except (IndexError, ValueError):
        # Sablonda bozuk sus parantezi varsa metni oldugu gibi kullan.
        kayit.warning(
            "Varsayilan aciklama sablonu bozuk, metin oldugu gibi kullanilacak."
        )
        return sablon


def _metni_oku(yol: Path) -> str | None:
    """Metin dosyasini birkac kodlama deneyerek okur."""
    for kodlama in ("utf-8-sig", "utf-8", "cp1254", "latin-1"):
        try:
            return yol.read_text(encoding=kodlama)
        except UnicodeDecodeError:
            continue
        except OSError as hata:
            kayit.warning("Aciklama dosyasi okunamadi (%s): %s", yol.name, hata)
            return None
    kayit.warning("Aciklama dosyasinin kodlamasi cozulemedi: %s", yol.name)
    return None


def aciklama_dosyasi_adaylari(
    dosya_yolu: Path, ad_bilgisi: AdBilgisi, uzanti: str = ".txt"
) -> list[Path]:
    """Aciklama dosyasinin bakilacagi yollar (sirayla denenir).

    1) Birebir ayni ad: "2026-09-25_1830_tanitim.txt"
    2) Tarih oneksiz ad: "tanitim.txt"
    """
    uzanti = uzanti if uzanti.startswith(".") else "." + uzanti
    klasor = dosya_yolu.parent
    adaylar = [klasor / (dosya_yolu.stem + uzanti)]
    if ad_bilgisi.govde and ad_bilgisi.govde != dosya_yolu.stem:
        adaylar.append(klasor / (ad_bilgisi.govde + uzanti))
    benzersiz: list[Path] = []
    for aday in adaylar:
        if aday not in benzersiz:
            benzersiz.append(aday)
    return benzersiz


def aciklamayi_coz(
    dosya_yolu: Path,
    ad_bilgisi: AdBilgisi,
    ayar: Ayarlar,
    medya_tipi: MedyaTipi,
) -> tuple[str | None, Path | None]:
    """Aciklamayi bulur. Doner: (metin, kaynak_dosya_yolu veya None).

    Once yanindaki .txt dosyasina bakilir, yoksa ayarlar.yaml'daki
    'yayin.varsayilan_aciklama' sablonu kullanilir. Ikisi de yoksa None doner.
    """
    for aday in aciklama_dosyasi_adaylari(
        dosya_yolu, ad_bilgisi, ayar.dosya.aciklama_uzantisi
    ):
        if not aday.is_file():
            continue
        metin = _metni_oku(aday)
        if metin is None:
            continue
        metin = metin.strip()
        if metin:
            return _aciklamayi_kirp(metin, dosya_yolu.name), aday
        kayit.info(
            "Aciklama dosyasi bos, varsayilan sablon kullanilacak: %s", aday.name
        )

    sablon = (ayar.yayin.varsayilan_aciklama or "").strip()
    if not sablon:
        return None, None
    metin = sablonu_uygula(
        sablon, ad_bilgisi, medya_tipi, zaman_dilimi=ayar.zaman_dilimi
    ).strip()
    return (_aciklamayi_kirp(metin, dosya_yolu.name) if metin else None), None


def _aciklamayi_kirp(metin: str, dosya_adi: str) -> str:
    """Caption 2200 karakteri asarsa kisaltir (asilirsa Meta kalici hata verir)."""
    if len(metin) <= CAPTION_MAKS_KARAKTER:
        return metin
    kesilmis = metin[:CAPTION_MAKS_KARAKTER]
    bosluk = kesilmis.rfind(" ")
    if bosluk > CAPTION_MAKS_KARAKTER - 200:
        kesilmis = kesilmis[:bosluk]
    kayit.warning(
        "Aciklama %d karakter siniri asti (%d), kisaltildi: %s",
        CAPTION_MAKS_KARAKTER,
        len(metin),
        dosya_adi,
    )
    return kesilmis.rstrip()


# ---------------------------------------------------------------------------
# 3) Dosya kabul kurallari ve kararlilik kontrolu
# ---------------------------------------------------------------------------


def _uzantiyi_duzelt(uzanti: str) -> str:
    uzanti = str(uzanti).strip().lower()
    return uzanti if uzanti.startswith(".") else "." + uzanti


def dosya_kabul_edilir_mi(
    dosya_yolu: Path, ayar: Ayarlar, medya_tipi: MedyaTipi
) -> tuple[bool, str]:
    """Dosya kuyruga alinmaya uygun mu? Doner: (uygun_mu, Turkce sebep)."""
    ad = dosya_yolu.name
    uzanti = dosya_yolu.suffix.lower()

    for onek in ayar.dosya.yoksayilan_onekler:
        if onek and ad.startswith(onek):
            return False, f"'{onek}' onekiyle basliyor, yoksayildi"
    if ad.endswith("~"):
        return False, "gecici yedek dosya (~ ile bitiyor)"
    if uzanti in GECICI_UZANTILAR:
        return False, "gecici/yarim dosya uzantisi"
    if uzanti == _uzantiyi_duzelt(ayar.dosya.aciklama_uzantisi):
        return False, "aciklama dosyasi, gorev olarak eklenmez"

    videolar = {_uzantiyi_duzelt(u) for u in ayar.dosya.video_uzantilari}
    gorseller = {_uzantiyi_duzelt(u) for u in ayar.dosya.gorsel_uzantilari}

    if uzanti in videolar:
        return True, "video"
    if uzanti in gorseller:
        if medya_tipi is MedyaTipi.STORIES:
            # Gorsel Story: rupload gorsel kabul etmez; medya katmani gorseli
            # once kisa bir videoya cevirir. Kuyruga alinmasi dogrudur.
            return True, "gorsel (video'ya cevrilecek)"
        return False, (
            "Reels icin gorsel yayinlanamaz, sadece video kabul edilir "
            "(gorseli gelen/story klasorune koyun)"
        )
    return False, f"desteklenmeyen uzanti: {uzanti or '(yok)'}"


def _dosya_acilabiliyor_mu(dosya_yolu: Path) -> bool:
    """Dosya baska bir surec tarafindan kilitli mi? (Windows kopyalama kontrolu)"""
    try:
        with open(dosya_yolu, "rb+"):
            return True
    except PermissionError:
        # Salt okunur dosya da PermissionError verir; bu bir kilit degildir.
        if not os.access(dosya_yolu, os.W_OK):
            try:
                with open(dosya_yolu, "rb") as akis:
                    akis.read(1)
                return True
            except OSError:
                return False
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return False


def dosya_stabil_mi(
    dosya_yolu: Path | str,
    *,
    bekleme_sn: float = 3.0,
    tekrar: int = 3,
    azami_bekleme_sn: float | None = None,
    uyku: Callable[[float], None] = time.sleep,
    iptal: threading.Event | None = None,
) -> bool:
    """Dosyanin kopyalanmasi bitti mi? Bitene kadar bekler.

    Boyut ve degistirme zamani ust uste 'tekrar' kez ayni kalirsa ve dosya
    baska bir surec tarafindan kilitli degilse dosya hazir sayilir.

    iptal verilirse (threading.Event) program kapanirken bekleme aninda kesilir.

    Doner: True (hazir) / False (silindi, bos kaldi, iptal edildi, sure asildi)
    """
    yol = Path(dosya_yolu)
    bekleme_sn = max(0.1, float(bekleme_sn))
    tekrar = max(1, int(tekrar))
    if azami_bekleme_sn is None:
        azami_bekleme_sn = max(60.0, bekleme_sn * tekrar * 10.0)
    bitis = time.monotonic() + float(azami_bekleme_sn)

    son_olcum: tuple[int, int] | None = None
    esit_sayaci = 0

    while True:
        if iptal is not None and iptal.is_set():
            kayit.debug("Kapanis istegi geldi, bekleme kesildi: %s", yol.name)
            return False
        try:
            durum = yol.stat()
        except FileNotFoundError:
            kayit.debug("Dosya kayboldu, beklemekten vazgecildi: %s", yol.name)
            return False
        except OSError as hata:
            kayit.warning("Dosya durumu okunamadi (%s): %s", yol.name, hata)
            return False

        olcum = (int(durum.st_size), int(durum.st_mtime_ns))
        if durum.st_size <= 0:
            esit_sayaci = 0
        elif son_olcum is not None and olcum == son_olcum:
            esit_sayaci += 1
        else:
            esit_sayaci = 1
        son_olcum = olcum

        if esit_sayaci >= tekrar and _dosya_acilabiliyor_mu(yol):
            return True

        if time.monotonic() >= bitis:
            kayit.warning(
                "Dosya hala kopyalaniyor gorunuyor, bu turda atlandi "
                "(bir sonraki taramada tekrar denenecek): %s",
                yol.name,
            )
            return False
        if iptal is not None:
            if iptal.wait(bekleme_sn):
                return False
        else:
            uyku(bekleme_sn)


def _aciklama_dosyasini_bekle(
    dosya_yolu: Path,
    ad_bilgisi: AdBilgisi,
    ayar: Ayarlar,
    *,
    azami_sn: float = ACIKLAMA_BEKLEME_SN,
    uyku: Callable[[float], None] = time.sleep,
    iptal: threading.Event | None = None,
) -> None:
    """Video once, .txt sonra kopyalanmis olabilir; kisa bir sure bekler.

    Aciklama dosyasi zaten varsa ya da dosya eski ise (baslangic taramasindaki
    gibi) hic beklenmez; beklemek sadece yeni gelen dosyalar icin anlamlidir.
    """
    adaylar = aciklama_dosyasi_adaylari(
        dosya_yolu, ad_bilgisi, ayar.dosya.aciklama_uzantisi
    )
    if any(aday.is_file() for aday in adaylar):
        return
    if azami_sn <= 0:
        return
    try:
        yas_sn = time.time() - dosya_yolu.stat().st_mtime
    except OSError:
        return
    if yas_sn > 60.0:
        # Program kapaliyken atilmis eski dosya: metin gelecekse zaten gelmistir.
        return
    adim = min(1.0, max(0.2, azami_sn / 10.0))
    bitis = time.monotonic() + azami_sn
    while time.monotonic() < bitis:
        if iptal is not None:
            if iptal.wait(adim):
                return
        else:
            uyku(adim)
        if any(aday.is_file() for aday in adaylar):
            kayit.debug(
                "Aciklama dosyasi sonradan geldi: %s", dosya_yolu.name
            )
            return


# ---------------------------------------------------------------------------
# 4) Kuyruga alma
# ---------------------------------------------------------------------------


#: Ayni veritabani icin sema hazirligini surec basina bir kez calistirmak yeter.
_HAZIRLANAN_VERITABANLARI: set[str] = set()
_HAZIRLIK_KILIDI: Final[threading.Lock] = threading.Lock()


def _veritabanini_bir_kez_hazirla(db_yolu: VeriTabaniYolu) -> None:
    """Sema yoksa olusturur; ayni yol icin tekrar tekrar calismaz."""
    anahtar = str(Path(db_yolu).expanduser())
    with _HAZIRLIK_KILIDI:
        if anahtar in _HAZIRLANAN_VERITABANLARI:
            return
        veritabanini_hazirla(db_yolu)
        _HAZIRLANAN_VERITABANLARI.add(anahtar)


def gorevi_kuyruga_al(
    dosya_yolu: Path | str,
    medya_tipi: MedyaTipi | str,
    ayar: Ayarlar,
    *,
    db_yolu: VeriTabaniYolu | None = None,
) -> int | None:
    """Tek bir dosyayi kuyruga ekler. Doner: gorev id'si veya None.

    None donme sebepleri: dosya uygun degil, dosya yok ya da ZATEN kuyrukta.
    Dosyanin kopyalanmasinin bitmis olmasi beklenir (bkz. dosya_stabil_mi).
    """
    yol = Path(dosya_yolu)
    tip = MedyaTipi(str(medya_tipi))
    veritabani = db_yolu if db_yolu is not None else ayar.db_yolu

    uygun, sebep = dosya_kabul_edilir_mi(yol, ayar, tip)
    if not uygun:
        kayit.debug("Atlandi (%s): %s", sebep, yol.name)
        return None
    if not yol.is_file():
        kayit.debug("Dosya bulunamadi, atlandi: %s", yol)
        return None

    _veritabanini_bir_kez_hazirla(veritabani)
    mevcut = dosyaya_gore_getir(veritabani, yol)
    if mevcut is not None:
        kayit.debug(
            "Dosya zaten kuyrukta (durum=%s), tekrar eklenmedi: %s",
            mevcut.durum,
            yol.name,
        )
        return None

    ad_bilgisi = dosya_adini_ayristir(yol.name, zaman_dilimi=ayar.zaman_dilimi)
    if ad_bilgisi.uyari:
        kayit.warning("%s", ad_bilgisi.uyari)

    planlanan = planlanan_zamani_hesapla(ad_bilgisi, ayar)
    aciklama, kaynak = aciklamayi_coz(yol, ad_bilgisi, ayar, tip)

    if tip is MedyaTipi.STORIES and aciklama:
        # Meta dokumaninda STORIES icin caption desteklenmiyor; gonderilse bile
        # yok sayilir. Kuyruga yazmiyoruz ki yayin katmani yanlislikla gondermesin.
        if kaynak is not None:
            kayit.info(
                "Story icin aciklama desteklenmiyor, %s dosyasindaki metin "
                "kullanilmayacak.",
                kaynak.name,
            )
        aciklama = None

    gorev_id = kuyruga_ekle(
        veritabani,
        yol,
        tip,
        aciklama=aciklama,
        planlanan_zaman=planlanan,
    )
    if gorev_id is None:
        kayit.debug("Dosya zaten kuyrukta (benzersiz kisit): %s", yol.name)
        return None

    if planlanan is None:
        plan_metni = "siradaki uygun slot"
    else:
        plan_metni = planlanan.astimezone(ayar.zaman_dilimi).strftime(
            "%d.%m.%Y %H:%M"
        )
        if planlanan < datetime.now(ayar.zaman_dilimi):
            plan_metni += " (gecmis tarih, ilk firsatta yayinlanacak)"

    kayit.info(
        "Kuyruga eklendi [#%s] %s | tip=%s | plan=%s | aciklama=%s",
        gorev_id,
        yol.name,
        tip,
        plan_metni,
        (kaynak.name if kaynak else ("varsayilan sablon" if aciklama else "yok")),
    )
    return gorev_id


def klasordeki_adaylar(ayar: Ayarlar) -> list[tuple[Path, MedyaTipi]]:
    """Izlenen klasorlerdeki uygun dosyalari (eskiden yeniye) listeler."""
    bulunanlar: list[tuple[float, Path, MedyaTipi]] = []
    for anahtar, klasor in ayar.izlenen_klasorler.items():
        tip = MedyaTipi(str(anahtar))
        if not klasor.is_dir():
            kayit.warning("Izlenecek klasor bulunamadi: %s", klasor)
            continue
        for yol in klasor.iterdir():
            if not yol.is_file():
                continue
            uygun, sebep = dosya_kabul_edilir_mi(yol, ayar, tip)
            if not uygun:
                if yol.suffix.lower() != _uzantiyi_duzelt(
                    ayar.dosya.aciklama_uzantisi
                ):
                    kayit.debug("Atlandi (%s): %s", sebep, yol.name)
                continue
            try:
                zaman = yol.stat().st_mtime
            except OSError:
                zaman = 0.0
            bulunanlar.append((zaman, yol, tip))
    bulunanlar.sort(key=lambda kayit_satiri: kayit_satiri[0])
    return [(yol, tip) for _, yol, tip in bulunanlar]


def tek_seferlik_tarama(
    ayar: Ayarlar | None = None,
    *,
    db_yolu: VeriTabaniYolu | None = None,
    kararlilik_bekle: bool = True,
) -> int:
    """Klasorleri bir kez tarar ve uygun dosyalari kuyruga alir.

    watchdog olmadan da calisir. Zamanlayici bunu guvenlik agi olarak
    periyodik cagirabilir (kacirilan olaylar icin).

    Doner: kuyruga yeni eklenen gorev sayisi.
    """
    ayar = ayar or ayarlari_yukle()
    veritabani = db_yolu if db_yolu is not None else ayar.db_yolu
    _veritabanini_bir_kez_hazirla(veritabani)

    eklenen = 0
    for yol, tip in klasordeki_adaylar(ayar):
        if kararlilik_bekle and not dosya_stabil_mi(
            yol,
            bekleme_sn=ayar.dosya.kararlilik_bekleme_sn,
            tekrar=ayar.dosya.kararlilik_tekrar,
        ):
            continue
        if gorevi_kuyruga_al(yol, tip, ayar, db_yolu=veritabani) is not None:
            eklenen += 1
    return eklenen


# ---------------------------------------------------------------------------
# 5) watchdog olay isleyicisi
# ---------------------------------------------------------------------------


class _OlayIsleyici(FileSystemEventHandler):  # type: ignore[misc, valid-type]
    """Tek bir klasor icin watchdog olaylarini izleyiciye aktarir."""

    def __init__(self, izleyici: "KlasorIzleyici", medya_tipi: MedyaTipi) -> None:
        super().__init__()
        self._izleyici = izleyici
        self._medya_tipi = medya_tipi

    # -- watchdog geri cagrilari --------------------------------------------

    def on_created(self, olay: Any) -> None:
        self._aktar(olay, getattr(olay, "src_path", None))

    def on_modified(self, olay: Any) -> None:
        self._aktar(olay, getattr(olay, "src_path", None))

    def on_moved(self, olay: Any) -> None:
        self._aktar(olay, getattr(olay, "dest_path", None))

    def on_closed(self, olay: Any) -> None:  # bazi platformlarda uretilir
        self._aktar(olay, getattr(olay, "src_path", None))

    # -- ic yardimci ---------------------------------------------------------

    def _aktar(self, olay: Any, ham_yol: Any) -> None:
        if getattr(olay, "is_directory", False) or not ham_yol:
            return
        try:
            yol = Path(os.fsdecode(ham_yol))
        except (TypeError, ValueError):
            return
        self._izleyici.aday_ekle(yol, self._medya_tipi)


# ---------------------------------------------------------------------------
# 6) Izleyici servisi
# ---------------------------------------------------------------------------


class KlasorIzleyici:
    """gelen/reels ve gelen/story klasorlerini izler, kuyruga gorev ekler.

    Kullanim:
        ayar = ayarlari_yukle()
        with KlasorIzleyici(ayar) as izleyici:
            izleyici.bekle()          # Ctrl+C ile cikilir

    veya tek satirda:
        KlasorIzleyici(ayarlari_yukle()).calis()
    """

    def __init__(
        self,
        ayar: Ayarlar | None = None,
        *,
        db_yolu: VeriTabaniYolu | None = None,
        yoklama_kullan: bool = False,
        geri_cagri: Callable[[int, Path, MedyaTipi], None] | None = None,
    ) -> None:
        self.ayar = ayar or ayarlari_yukle()
        self.db_yolu: VeriTabaniYolu = (
            db_yolu if db_yolu is not None else self.ayar.db_yolu
        )
        self.yoklama_kullan = bool(yoklama_kullan)
        self.geri_cagri = geri_cagri

        self._kuyruk: "queue.Queue[tuple[Path, MedyaTipi] | None]" = queue.Queue()
        self._kilit = threading.Lock()
        self._kuyruktakiler: set[str] = set()
        self._son_islenen: dict[str, float] = {}
        self._dur = threading.Event()
        self._isci: threading.Thread | None = None
        self._yoklama_ipligi: threading.Thread | None = None
        self._gozlemci: Any = None
        self._calisiyor = False
        self.eklenen_sayisi = 0

    # -- yasam dongusu -------------------------------------------------------

    def baslat(self) -> None:
        """Klasorleri hazirlar, izlemeyi baslatir ve ilk taramayi yapar."""
        if self._calisiyor:
            kayit.debug("Izleyici zaten calisiyor.")
            return

        self.ayar.klasorleri_hazirla()
        _veritabanini_bir_kez_hazirla(self.db_yolu)
        self._dur.clear()

        self._isci = threading.Thread(
            target=self._isci_dongusu, name="izleyici-isci", daemon=True
        )
        self._isci.start()

        if WATCHDOG_VAR:
            self._gozlemciyi_baslat()
        else:
            kayit.warning(
                "watchdog paketi kurulu degil, dahili yoklama moduna gecildi "
                "(her %d saniyede bir klasorler taranir). Kurulum: "
                "pip install watchdog",
                self.ayar.zamanlama.tarama_araligi_sn,
            )
            self._yoklamayi_baslat()

        self._calisiyor = True
        for anahtar, klasor in self.ayar.izlenen_klasorler.items():
            kayit.info("Izleniyor (%s): %s", anahtar, klasor)

        eklenen = self.baslangic_taramasi()
        if eklenen:
            kayit.info(
                "Baslangic taramasi: %d dosya kuyruga alindi.", eklenen
            )
        else:
            kayit.info("Baslangic taramasi: yeni dosya yok.")

    def durdur(self, zaman_asimi: float = 15.0) -> None:
        """Izlemeyi durdurur ve isci ipligin bitmesini bekler."""
        if not self._calisiyor and self._isci is None:
            return
        kayit.info("Izleyici durduruluyor...")
        self._dur.set()

        if self._gozlemci is not None:
            try:
                self._gozlemci.stop()
                self._gozlemci.join(timeout=zaman_asimi)
            except Exception as hata:  # pragma: no cover - platforma bagli
                kayit.debug("Gozlemci durdurulurken hata: %s", hata)
            self._gozlemci = None

        self._kuyruk.put(None)  # isci dongusunu uyandir
        if self._isci is not None:
            self._isci.join(timeout=zaman_asimi)
            self._isci = None
        if self._yoklama_ipligi is not None:
            self._yoklama_ipligi.join(timeout=zaman_asimi)
            self._yoklama_ipligi = None

        self._calisiyor = False
        kayit.info("Izleyici durduruldu.")

    def bekle(self, aralik_sn: float = 1.0) -> None:
        """Durdurulana kadar bloklar (Ctrl+C ile cikilir)."""
        while not self._dur.is_set():
            self._dur.wait(aralik_sn)

    def calis(self) -> None:
        """baslat + bekle + durdur; tek basina calistirma icin."""
        self.baslat()
        try:
            self.bekle()
        except KeyboardInterrupt:
            print("")  # Ctrl+C sonrasi satir basi
            kayit.info("Klavyeden durdurma istegi alindi.")
        finally:
            self.durdur()

    def __enter__(self) -> "KlasorIzleyici":
        self.baslat()
        return self

    def __exit__(self, *_hata_bilgisi: Any) -> None:
        self.durdur()

    # -- tarama ve kuyruk ----------------------------------------------------

    def baslangic_taramasi(self) -> int:
        """Program kapaliyken atilan dosyalari kuyruga alir.

        Dosyalar isci ipligine devredilir; kararlilik beklemesi orada yapilir.
        Doner: kuyruga verilen aday sayisi.
        """
        verilen = 0
        for yol, tip in klasordeki_adaylar(self.ayar):
            if self.aday_ekle(yol, tip):
                verilen += 1
        return verilen

    def aday_ekle(self, dosya_yolu: Path | str, medya_tipi: MedyaTipi) -> bool:
        """Bir dosyayi islenmek uzere kuyruga koyar. Doner: eklendi mi."""
        yol = Path(dosya_yolu)
        uzanti = yol.suffix.lower()

        # .txt dosyasi: gorev degildir, ama yanindaki medyanin kuyruga girmesini
        # tetikleyebilir (once video sonra metin atilmis olabilir).
        if uzanti == _uzantiyi_duzelt(self.ayar.dosya.aciklama_uzantisi):
            self._aciklama_dosyasi_geldi(yol, medya_tipi)
            return False

        uygun, sebep = dosya_kabul_edilir_mi(yol, self.ayar, medya_tipi)
        if not uygun:
            kayit.debug("Atlandi (%s): %s", sebep, yol.name)
            return False

        anahtar = self._anahtar(yol)
        simdi_mono = time.monotonic()
        with self._kilit:
            if anahtar in self._kuyruktakiler:
                return False
            son = self._son_islenen.get(anahtar)
            if son is not None and simdi_mono - son < _SOGUTMA_SN:
                return False
            self._kuyruktakiler.add(anahtar)

        self._kuyruk.put((yol, medya_tipi))
        kayit.debug("Aday kuyruga verildi: %s", yol.name)
        return True

    def _aciklama_dosyasi_geldi(self, txt_yolu: Path, medya_tipi: MedyaTipi) -> None:
        """Yeni gelen .txt icin ayni adli medya dosyalarini tekrar degerlendirir."""
        klasor = txt_yolu.parent
        govde = txt_yolu.stem
        uzantilar = [
            _uzantiyi_duzelt(u)
            for u in (
                list(self.ayar.dosya.video_uzantilari)
                + list(self.ayar.dosya.gorsel_uzantilari)
            )
        ]
        for uzanti in uzantilar:
            aday = klasor / (govde + uzanti)
            if aday.is_file():
                self.aday_ekle(aday, medya_tipi)

    # -- ic iplikler ---------------------------------------------------------

    def _isci_dongusu(self) -> None:
        """Kuyruktan aday alir, kararlilik bekler ve veritabanina yazar."""
        while not self._dur.is_set():
            try:
                is_kaydi = self._kuyruk.get(timeout=0.5)
            except queue.Empty:
                continue
            if is_kaydi is None:
                self._kuyruk.task_done()
                break
            yol, tip = is_kaydi
            sogut = True
            try:
                sogut = self._adayi_isle(yol, tip)
            except Exception as hata:  # tek bir dosya butun izleyiciyi durdurmasin
                kayit.exception(
                    "Dosya islenirken beklenmeyen hata (%s): %s", yol.name, hata
                )
            finally:
                anahtar = self._anahtar(yol)
                with self._kilit:
                    self._kuyruktakiler.discard(anahtar)
                    if sogut:
                        self._son_islenen[anahtar] = time.monotonic()
                    else:
                        # Yarim kalan dosya: sogutma uygulanmaz ki bir sonraki
                        # olay/tarama ayni dosyayi tekrar denesin.
                        self._son_islenen.pop(anahtar, None)
                    self._eski_kayitlari_temizle()
                self._kuyruk.task_done()

    def _adayi_isle(self, yol: Path, tip: MedyaTipi) -> bool:
        """Tek bir aday dosyayi kuyruga alir (kararlilik + aciklama beklemesi).

        Doner: sogutma uygulansin mi (False ise dosya tekrar denenmelidir).
        """
        if not yol.is_file():
            return False
        hazir = dosya_stabil_mi(
            yol,
            bekleme_sn=self.ayar.dosya.kararlilik_bekleme_sn,
            tekrar=self.ayar.dosya.kararlilik_tekrar,
            iptal=self._dur,
        )
        if not hazir:
            return False

        ad_bilgisi = dosya_adini_ayristir(
            yol.name, zaman_dilimi=self.ayar.zaman_dilimi
        )
        _aciklama_dosyasini_bekle(yol, ad_bilgisi, self.ayar, iptal=self._dur)

        gorev_id = gorevi_kuyruga_al(yol, tip, self.ayar, db_yolu=self.db_yolu)
        if gorev_id is None:
            return True
        self.eklenen_sayisi += 1
        if self.geri_cagri is not None:
            try:
                self.geri_cagri(gorev_id, yol, tip)
            except Exception as hata:  # geri cagri hatasi izleyiciyi durdurmasin
                kayit.exception("Geri cagri hatasi: %s", hata)
        return True

    def _yoklama_dongusu(self) -> None:
        """watchdog yoksa: klasorleri periyodik tarar."""
        aralik = float(self.ayar.zamanlama.tarama_araligi_sn)
        while not self._dur.is_set():
            if self._dur.wait(aralik):
                break
            try:
                for yol, tip in klasordeki_adaylar(self.ayar):
                    self.aday_ekle(yol, tip)
            except Exception as hata:  # pragma: no cover - savunmaci
                kayit.exception("Yoklama taramasinda hata: %s", hata)

    # -- kurulum yardimcilari ------------------------------------------------

    def _gozlemciyi_baslat(self) -> None:
        """watchdog gozlemcisini kurar ve baslatir."""
        sinif = PollingObserver if self.yoklama_kullan else Observer
        self._gozlemci = sinif()
        for anahtar, klasor in self.ayar.izlenen_klasorler.items():
            tip = MedyaTipi(str(anahtar))
            klasor.mkdir(parents=True, exist_ok=True)
            self._gozlemci.schedule(
                _OlayIsleyici(self, tip), str(klasor), recursive=False
            )
        self._gozlemci.start()

    def _yoklamayi_baslat(self) -> None:
        self._yoklama_ipligi = threading.Thread(
            target=self._yoklama_dongusu, name="izleyici-yoklama", daemon=True
        )
        self._yoklama_ipligi.start()

    @staticmethod
    def _anahtar(yol: Path) -> str:
        """Ayni dosyanin farkli yazimlari tek anahtara dussun."""
        try:
            return str(yol.resolve())
        except OSError:  # pragma: no cover - cok nadir
            return str(yol)

    def _eski_kayitlari_temizle(self) -> None:
        """Sogutma sozlugu sonsuz buyumesin (kilit ALTINDA cagrilir)."""
        if len(self._son_islenen) < 512:
            return
        simdi_mono = time.monotonic()
        for anahtar, an in list(self._son_islenen.items()):
            if simdi_mono - an > _SOGUTMA_SN:
                self._son_islenen.pop(anahtar, None)


# ---------------------------------------------------------------------------
# 7) Tek basina calistirma
# ---------------------------------------------------------------------------


def _kaydi_ayarla(ayar: Ayarlar) -> None:
    """Tek basina calistirildiginda basit log bicimi kurar."""
    logging.basicConfig(
        level=getattr(logging, ayar.kayit.seviye, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argumanlar: Iterable[str] | None = None) -> int:
    """python -m src.izleyici  ->  klasorleri izler (Ctrl+C ile cikilir)."""
    ayristirici = argparse.ArgumentParser(
        description="Klasor izleyici: gelen/ klasorlerini izler ve kuyruga ekler."
    )
    ayristirici.add_argument(
        "--tara",
        action="store_true",
        help="Sadece bir kez tara ve cik (surekli izleme yapma).",
    )
    ayristirici.add_argument(
        "--yoklama",
        action="store_true",
        help="Olay yerine yoklama (polling) kullan (ag surucusu/paylasim icin).",
    )
    secenekler = ayristirici.parse_args(list(argumanlar) if argumanlar else None)

    ayar = ayarlari_yukle()
    _kaydi_ayarla(ayar)
    ayar.klasorleri_hazirla()

    if secenekler.tara:
        eklenen = tek_seferlik_tarama(ayar)
        print(f"Tarama bitti. Kuyruga eklenen dosya sayisi: {eklenen}")
        return 0

    izleyici = KlasorIzleyici(ayar, yoklama_kullan=secenekler.yoklama)
    izleyici.calis()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ACIKLAMA_BEKLEME_SN",
    "AdBilgisi",
    "GECICI_UZANTILAR",
    "KlasorIzleyici",
    "WATCHDOG_VAR",
    "aciklama_dosyasi_adaylari",
    "aciklamayi_coz",
    "dosya_adini_ayristir",
    "dosya_kabul_edilir_mi",
    "dosya_stabil_mi",
    "gorevi_kuyruga_al",
    "klasordeki_adaylar",
    "main",
    "planlanan_zamani_hesapla",
    "sablonu_uygula",
    "tek_seferlik_tarama",
]
