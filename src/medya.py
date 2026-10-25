# -*- coding: ascii -*-
"""Medya hazirlama ve dogrulama katmani.

Bu modulun tek isi, bir dosyanin Meta'ya gonderilmeden ONCE yerel olarak
elenmesi ve gerekiyorsa uygun bicime cevrilmesidir. Sinirlari asan dosya
yukleme sirasinda degil ISLEME sirasinda status_code=ERROR olarak geri doner;
o noktada hem zaman hem de 24 saatlik kapsayici kotasi bosa harcanmis olur.

Dort ana giris noktasi:
    dosya_tipi_belirle(yol)        -> video mu gorsel mi
    jpeg_e_cevir(yol)              -> PNG/WebP/HEIC/BMP -> JPEG (Pillow)
    video_dogrula(yol, medya_tipi) -> sure / oran / boyut / kodek kontrolu
    dosya_stabil_mi(yol)           -> dosya hala kopyalaniyor mu

Bagimlilik kurali:
  - Pillow ZORUNLU (gorsel cevrimi icin).
  - ffmpeg / ffprobe ZORUNLU DEGIL. Yoksa video icerik dogrulamasi yumusak
    gecilir (sadece uzanti + boyut + moov kontrolu yapilir) ve uyari loglanir.

DIKKAT (mimari): rupload/resumable akisi SADECE video kabul eder. JPEG bir
gorsel dogrudan yuklenemez; gorsel yayini public image_url ister. Bu projede
bulut depolama olmadigi icin gorseller ffmpeg ile kisa bir MP4'e cevrilip
STORIES videosu olarak yayinlanir (bkz. gorseli_videoya_cevir).

Turkce karakter kullanilmaz (ASCII), konsol encoding sorunu cikmasin diye.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from shutil import which
from typing import Any, Final

from src.modeller import (
    STORY_GORSEL_SINIRLAR,
    MedyaDogrulamaHatasi,
    MedyaTipi,
    sinirlari_getir,
)

kayit: Final[logging.Logger] = logging.getLogger("insta.medya")


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------


class DosyaTipi(StrEnum):
    """dosya_tipi_belirle() sonucu."""

    VIDEO = "video"
    GORSEL = "gorsel"
    ACIKLAMA = "aciklama"      # yanindaki .txt caption dosyasi (gorev degil)
    BILINMIYOR = "bilinmiyor"


#: Meta'nin kabul ettigi video uzantilari (MOV veya MP4 - MPEG-4 Part 14)
VIDEO_UZANTILARI: Final[tuple[str, ...]] = (".mp4", ".mov")

#: Video olarak taninan ama Meta'nin KABUL ETMEDIGI uzantilar.
#: Bunlar "bilinmiyor" degil "video" sayilir; boylece kullaniciya
#: "bu bir video ama once MP4'e cevirmelisin" denebilir.
CEVRILMESI_GEREKEN_VIDEO_UZANTILARI: Final[tuple[str, ...]] = (
    ".mkv", ".avi", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".3gp", ".flv", ".ts",
)

#: Meta'nin kabul ettigi tek gorsel bicimi (MPO / JPS desteklenmez)
GORSEL_UZANTILARI: Final[tuple[str, ...]] = tuple(STORY_GORSEL_SINIRLAR["uzantilar"])

#: Pillow ile JPEG'e cevrilebilen gorsel uzantilari
CEVRILEBILIR_GORSEL_UZANTILARI: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(
        tuple(STORY_GORSEL_SINIRLAR["cevrilebilir_uzantilar"])
        + (".heic", ".heif", ".tif", ".tiff", ".gif", ".jfif")
    )
)

#: Caption kaynagi olarak kullanilan metin dosyasi
ACIKLAMA_UZANTILARI: Final[tuple[str, ...]] = (".txt",)

#: JPEG cevriminde hedef ust sinir (Meta: 8 MB)
GORSEL_AZAMI_BAYT: Final[int] = int(STORY_GORSEL_SINIRLAR["maks_boyut_bayt"])

#: JPEG cevriminde uzun kenar ust siniri. Meta Stories icin kesin bir piksel
#: siniri yayinlamiyor; 1920 hem dosya boyutunu kucultur hem video sinirlariyla
#: tutarli kalir.
GORSEL_AZAMI_KENAR: Final[int] = 1920

#: Meta'nin kabul ettigi kodekler
KABUL_EDILEN_VIDEO_KODEKLERI: Final[frozenset[str]] = frozenset({"h264", "hevc"})
KABUL_EDILEN_SES_KODEKLERI: Final[frozenset[str]] = frozenset({"aac"})

#: FPS karsilastirmasinda yuvarlama paylari (29.97 / 59.94 gibi degerler icin)
FPS_TOLERANSI: Final[float] = 0.5
#: Sure karsilastirmasinda pay (ffprobe suresi milisaniye oynayabilir)
SURE_TOLERANSI_SN: Final[float] = 0.25

#: Gorselden uretilecek Story videosunun varsayilan suresi (min 3 sn siniri var)
GORSEL_VIDEO_SURESI_SN: Final[int] = 5
GORSEL_VIDEO_GENISLIK: Final[int] = 1080
GORSEL_VIDEO_YUKSEKLIK: Final[int] = 1920

#: Dis komutlar icin zaman asimlari (saniye)
FFPROBE_ZAMAN_ASIMI_SN: Final[float] = 30.0
FFMPEG_ZAMAN_ASIMI_SN: Final[float] = 600.0

#: Bu modulun urettigi gecici dosyalar. Yayin bitince temizlenmeli.
_GECICI_DOSYALAR: set[Path] = set()

_GECICI_ONEK: Final[str] = "insta_otomasyon_"


# ---------------------------------------------------------------------------
# Video bilgisi
# ---------------------------------------------------------------------------


@dataclass
class VideoBilgisi:
    """video_dogrula() sonucu. ffprobe yoksa alanlarin cogu None kalir."""

    yol: Path
    boyut_bayt: int
    sure_sn: float | None = None
    genislik: int | None = None          # donus (rotation) uygulanmis degerdir
    yukseklik: int | None = None
    fps: float | None = None
    video_kodegi: str | None = None
    ses_kodegi: str | None = None        # ses yoksa None
    moov_basta: bool | None = None       # None = tespit edilemedi
    ffprobe_kullanildi: bool = False
    uyarilar: list[str] = field(default_factory=list)

    @property
    def oran(self) -> float | None:
        """En-boy orani (genislik / yukseklik)."""
        if not self.genislik or not self.yukseklik:
            return None
        return self.genislik / self.yukseklik

    @property
    def boyut_mb(self) -> float:
        return self.boyut_bayt / (1024 * 1024)

    def ozet(self) -> str:
        """Log satirlari icin tek satirlik ozet."""
        parcalar = [f"{self.yol.name}", _bayt_metni(self.boyut_bayt)]
        if self.sure_sn is not None:
            parcalar.append(f"{self.sure_sn:.1f} sn")
        if self.genislik and self.yukseklik:
            parcalar.append(f"{self.genislik}x{self.yukseklik}")
        if self.fps:
            parcalar.append(f"{self.fps:.2f} fps")
        if self.video_kodegi:
            parcalar.append(self.video_kodegi)
        parcalar.append("ses=" + (self.ses_kodegi or "yok"))
        if self.moov_basta is False:
            parcalar.append("moov=sonda")
        return " | ".join(parcalar)


# ---------------------------------------------------------------------------
# 1) Dosya tipi
# ---------------------------------------------------------------------------


def dosya_tipi_belirle(yol: Path | str) -> DosyaTipi:
    """Dosyanin video mu gorsel mi oldugunu belirler.

    Once uzantiya bakar; uzanti yoksa veya taninmiyorsa dosyanin ilk
    baytlarindaki imzaya (magic bytes) bakar. Boylece "video" adiyla uzantisiz
    birakilmis dosyalar da dogru siniflanir.
    """
    dosya = Path(yol)
    uzanti = dosya.suffix.lower()

    if uzanti in VIDEO_UZANTILARI or uzanti in CEVRILMESI_GEREKEN_VIDEO_UZANTILARI:
        return DosyaTipi.VIDEO
    if uzanti in GORSEL_UZANTILARI or uzanti in CEVRILEBILIR_GORSEL_UZANTILARI:
        return DosyaTipi.GORSEL
    if uzanti in ACIKLAMA_UZANTILARI:
        return DosyaTipi.ACIKLAMA

    return _imzadan_tip_belirle(dosya)


def video_mu(yol: Path | str) -> bool:
    """Kisayol: dosya video mu."""
    return dosya_tipi_belirle(yol) is DosyaTipi.VIDEO


def gorsel_mi(yol: Path | str) -> bool:
    """Kisayol: dosya gorsel mi."""
    return dosya_tipi_belirle(yol) is DosyaTipi.GORSEL


def meta_kabul_ediyor_mu(yol: Path | str) -> bool:
    """Dosya, cevrim yapilmadan Meta'ya gonderilebilecek bicimde mi?"""
    uzanti = Path(yol).suffix.lower()
    return uzanti in VIDEO_UZANTILARI or uzanti in GORSEL_UZANTILARI


def _imzadan_tip_belirle(dosya: Path) -> DosyaTipi:
    """Ilk baytlara bakarak tip tahmini (uzanti yoksa/taninmiyorsa)."""
    try:
        with dosya.open("rb") as akis:
            bas = akis.read(16)
    except OSError:
        return DosyaTipi.BILINMIYOR

    if len(bas) < 12:
        return DosyaTipi.BILINMIYOR
    if bas.startswith(b"\xff\xd8\xff"):                 # JPEG
        return DosyaTipi.GORSEL
    if bas.startswith(b"\x89PNG\r\n\x1a\n"):            # PNG
        return DosyaTipi.GORSEL
    if bas.startswith(b"BM"):                            # BMP
        return DosyaTipi.GORSEL
    if bas.startswith(b"GIF8"):                          # GIF
        return DosyaTipi.GORSEL
    if bas[:4] == b"RIFF" and bas[8:12] == b"WEBP":      # WebP
        return DosyaTipi.GORSEL
    if bas[4:8] == b"ftyp":                              # MP4 / MOV / HEIC
        marka = bas[8:12]
        if marka in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heif"):
            return DosyaTipi.GORSEL
        return DosyaTipi.VIDEO
    if bas[:4] == b"\x1a\x45\xdf\xa3":                   # Matroska / WebM
        return DosyaTipi.VIDEO
    return DosyaTipi.BILINMIYOR


# ---------------------------------------------------------------------------
# 2) Gorsel: dogrulama ve JPEG cevrimi
# ---------------------------------------------------------------------------


def _pillow_getir() -> Any:
    """Pillow'u ice aktarir; yoksa anlasilir Turkce hata verir."""
    try:
        from PIL import Image  # noqa: PLC0415 - istege bagli import
    except ImportError as hata:  # pragma: no cover - kurulum hatasi
        raise MedyaDogrulamaHatasi(
            "Pillow kurulu degil. Gorsel islemek icin kurun: "
            "python -m pip install Pillow"
        ) from hata
    return Image


def _heic_destegini_ac(uzanti: str) -> None:
    """HEIC/HEIF icin pillow-heif eklentisini kaydeder (varsa)."""
    if uzanti not in (".heic", ".heif"):
        return
    try:
        import pillow_heif  # noqa: PLC0415 - istege bagli eklenti

        pillow_heif.register_heif_opener()
    except ImportError:
        kayit.debug("pillow-heif yok; HEIC cevrimi ffmpeg'e devredilecek.")


def gorsel_dogrula(yol: Path | str) -> None:
    """Bir JPEG'in Meta sinirlarina uygunlugunu kontrol eder.

    Uymuyorsa MedyaDogrulamaHatasi firlatir. Not: bu dosya rupload ile
    DOGRUDAN yuklenemez; gorsel yayini icin gorseli_videoya_cevir() kullanilir.
    """
    dosya = _dosyayi_dogrula(yol)
    uzanti = dosya.suffix.lower()
    if uzanti not in GORSEL_UZANTILARI:
        raise MedyaDogrulamaHatasi(
            f"Gorsel bicimi desteklenmiyor: {dosya.name} "
            f"(Meta sadece JPEG kabul eder: {', '.join(GORSEL_UZANTILARI)}). "
            "jpeg_e_cevir() ile cevirin."
        )

    boyut = dosya.stat().st_size
    if boyut > GORSEL_AZAMI_BAYT:
        raise MedyaDogrulamaHatasi(
            f"Gorsel cok buyuk: {dosya.name} ({_bayt_metni(boyut)}). "
            f"Ust sinir {_bayt_metni(GORSEL_AZAMI_BAYT)}."
        )

    Image = _pillow_getir()
    try:
        with Image.open(dosya) as gorsel:
            gorsel.verify()
            bicim = (gorsel.format or "").upper()
    except MedyaDogrulamaHatasi:
        raise
    except Exception as hata:  # Pillow cesitli istisnalar firlatir
        raise MedyaDogrulamaHatasi(
            f"Gorsel dosyasi okunamadi veya bozuk: {dosya.name} ({hata})"
        ) from hata

    if bicim == "MPO":
        raise MedyaDogrulamaHatasi(
            f"Genisletilmis JPEG bicimi (MPO) desteklenmiyor: {dosya.name}. "
            "jpeg_e_cevir() ile duz JPEG'e cevirin."
        )
    if bicim not in ("JPEG", "JPG"):
        raise MedyaDogrulamaHatasi(
            f"Dosya uzantisi .jpg ama icerigi JPEG degil ({bicim or 'bilinmiyor'}): "
            f"{dosya.name}. Uzantiyi degistirmek yetmez, jpeg_e_cevir() kullanin."
        )


def jpeg_e_cevir(
    yol: Path | str,
    *,
    hedef: Path | str | None = None,
    kalite: int = 90,
    azami_bayt: int = GORSEL_AZAMI_BAYT,
    azami_kenar: int = GORSEL_AZAMI_KENAR,
    zorla: bool = False,
) -> Path:
    """PNG / WebP / HEIC / BMP / TIFF girdisini JPEG'e cevirir.

    Meta sadece JPEG kabul eder (MPO ve JPS gibi genisletilmis bicimler dahil
    degil). Cevrim sirasinda:
      - EXIF donus bilgisi piksellere uygulanir (telefon fotograflari yan durmasin)
      - saydamlik beyaz zemine yakilir (RGBA / paletli / LA girdiler)
      - CMYK ve diger modlar RGB'ye cevrilir (JPEG'te sRGB beklenir)
      - uzun kenar 'azami_kenar' pikseli gecerse orantili kucultulur
      - dosya 8 MB'i asarsa kalite, sonra boyut kademeli dusurulur

    Doner:
        Yeni JPEG dosyasinin yolu. 'hedef' verilmezse gecici bir dosya uretilir
        ve modul icinde kayda alinir; is bitince gecici_dosyayi_sil() veya
        gecici_dosyalari_temizle() cagrilmalidir.
        Girdi zaten gecerli bir JPEG ise (ve zorla=False) AYNI yol dondurulur;
        bu durumda silinecek gecici dosya yoktur (gecici_dosya_mi() ile bakin).
    """
    dosya = _dosyayi_dogrula(yol)
    uzanti = dosya.suffix.lower()

    if dosya_tipi_belirle(dosya) is not DosyaTipi.GORSEL:
        raise MedyaDogrulamaHatasi(
            f"Bu dosya gorsel degil, JPEG'e cevrilemez: {dosya.name}"
        )

    # Zaten uygun bir JPEG ise dokunma (gereksiz yeniden sikistirma kalite kaybidir)
    if not zorla and uzanti in GORSEL_UZANTILARI:
        try:
            gorsel_dogrula(dosya)
        except MedyaDogrulamaHatasi as hata:
            kayit.info("JPEG yeniden uretilecek (%s): %s", dosya.name, hata)
        else:
            return dosya

    if hedef is not None:
        cikti = Path(hedef)
        cikti.parent.mkdir(parents=True, exist_ok=True)
        gecici_mi = False
    else:
        cikti = _gecici_yol(".jpg")
        gecici_mi = True

    try:
        _pillow_ile_jpeg_yaz(dosya, cikti, kalite, azami_bayt, azami_kenar)
    except MedyaDogrulamaHatasi:
        # HEIC gibi Pillow'un acamadigi bicimlerde ffmpeg'e dusulur
        if uzanti in (".heic", ".heif") and ffmpeg_var_mi():
            kayit.info(
                "HEIC dosyasi Pillow ile acilamadi, ffmpeg ile cevriliyor: %s",
                dosya.name,
            )
            _ffmpeg_ile_jpeg_yaz(dosya, cikti)
        else:
            if gecici_mi:
                gecici_dosyayi_sil(cikti)
            raise

    kayit.info(
        "Gorsel JPEG'e cevrildi: %s -> %s (%s)",
        dosya.name,
        cikti.name,
        _bayt_metni(cikti.stat().st_size),
    )
    return cikti


def _pillow_ile_jpeg_yaz(
    kaynak: Path,
    cikti: Path,
    kalite: int,
    azami_bayt: int,
    azami_kenar: int,
) -> None:
    """Pillow ile JPEG uretir; boyut sinirini tutturana kadar kademeli kuculur."""
    Image = _pillow_getir()
    _heic_destegini_ac(kaynak.suffix.lower())

    try:
        from PIL import ImageOps  # noqa: PLC0415

        with Image.open(kaynak) as ham:
            ham.load()
            gorsel = ImageOps.exif_transpose(ham) or ham
            gorsel = _rgb_ye_cevir(gorsel, Image)

            if azami_kenar > 0 and max(gorsel.size) > azami_kenar:
                gorsel.thumbnail((azami_kenar, azami_kenar), Image.LANCZOS)

            suanki_kalite = max(40, min(int(kalite), 95))
            for deneme in range(14):
                gorsel.save(
                    cikti,
                    format="JPEG",
                    quality=suanki_kalite,
                    optimize=True,
                    subsampling="4:2:0",
                )
                if cikti.stat().st_size <= azami_bayt:
                    return
                # Once kaliteyi dusur, yetmezse cozunurlugu kademeli kucult
                if suanki_kalite > 45:
                    suanki_kalite -= 10
                else:
                    yeni_en = max(120, int(gorsel.width * 0.7))
                    yeni_boy = max(120, int(gorsel.height * 0.7))
                    if (yeni_en, yeni_boy) == gorsel.size:
                        break
                    gorsel = gorsel.resize((yeni_en, yeni_boy), Image.LANCZOS)
                kayit.debug(
                    "JPEG hala buyuk, kucultuluyor (deneme %d): %s", deneme + 1, cikti.name
                )
    except MedyaDogrulamaHatasi:
        raise
    except Exception as hata:
        raise MedyaDogrulamaHatasi(
            f"Gorsel JPEG'e cevrilemedi: {kaynak.name} ({hata})"
        ) from hata

    raise MedyaDogrulamaHatasi(
        f"Gorsel {_bayt_metni(azami_bayt)} altina indirilemedi: {kaynak.name}. "
        "Gorseli kucultup tekrar deneyin."
    )


def _rgb_ye_cevir(gorsel: Any, Image: Any) -> Any:
    """Saydamligi beyaz zemine yakar ve modu RGB'ye getirir (JPEG sarti)."""
    mod = gorsel.mode
    if mod == "RGB":
        return gorsel
    if mod in ("RGBA", "LA") or (mod == "P" and "transparency" in gorsel.info):
        saydam = gorsel.convert("RGBA")
        zemin = Image.new("RGB", saydam.size, (255, 255, 255))
        zemin.paste(saydam, mask=saydam.split()[-1])
        return zemin
    return gorsel.convert("RGB")


def _ffmpeg_ile_jpeg_yaz(kaynak: Path, cikti: Path) -> None:
    """Pillow acamayinca (ornek HEIC) ffmpeg ile JPEG uretir."""
    komut = [
        _ffmpeg_yolu(),
        "-y",
        "-v", "error",
        "-i", str(kaynak),
        "-frames:v", "1",
        "-q:v", "3",
        str(cikti),
    ]
    kod, _, hata_metni = _komut_calistir(komut, FFMPEG_ZAMAN_ASIMI_SN)
    if kod != 0 or not cikti.exists() or cikti.stat().st_size == 0:
        raise MedyaDogrulamaHatasi(
            f"Gorsel JPEG'e cevrilemedi (ffmpeg): {kaynak.name} "
            f"{hata_metni.strip()[:200]}"
        )


def gorseli_videoya_cevir(
    yol: Path | str,
    *,
    sure_sn: int = GORSEL_VIDEO_SURESI_SN,
    genislik: int = GORSEL_VIDEO_GENISLIK,
    yukseklik: int = GORSEL_VIDEO_YUKSEKLIK,
    hedef: Path | str | None = None,
) -> Path:
    """Gorseli kisa bir MP4'e cevirir (Story olarak yayinlamak icin).

    NEDEN: rupload/upload_type=resumable akisi SADECE video kabul eder. Gorsel
    yayini public bir image_url ister; bu projede bulut depolama olmadigi icin
    gorsel 3-60 saniyelik sessiz bir videoya cevrilip STORIES olarak gonderilir.

    ffmpeg gerektirir. ffmpeg yoksa anlasilir bir hata firlatir; cagiran taraf
    dosyayi hata/ klasorune tasimalidir.
    """
    dosya = _dosyayi_dogrula(yol)
    if not ffmpeg_var_mi():
        raise MedyaDogrulamaHatasi(
            f"Gorseli Story videosuna cevirmek icin ffmpeg gerekiyor: {dosya.name}. "
            "ffmpeg kurun (winget install Gyan.FFmpeg) veya gelen/story klasorune "
            "dogrudan .mp4 birakin."
        )

    sure = max(3, min(int(sure_sn), 60))   # STORIES: min 3 sn, maks 60 sn
    cikti = Path(hedef) if hedef is not None else _gecici_yol(".mp4")
    if hedef is not None:
        cikti.parent.mkdir(parents=True, exist_ok=True)

    # Pillow ile acilabilen her bicimi once JPEG'e cevirip ffmpeg'e oyle veririz;
    # boylece HEIC / WebP gibi bicimlerde ffmpeg derleme farklari sorun cikarmaz.
    ara_jpeg: Path | None = None
    girdi = dosya
    if dosya.suffix.lower() not in GORSEL_UZANTILARI:
        ara_jpeg = jpeg_e_cevir(dosya)
        girdi = ara_jpeg

    olcek = (
        f"scale={genislik}:{yukseklik}:force_original_aspect_ratio=decrease,"
        f"pad={genislik}:{yukseklik}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )
    komut = [
        _ffmpeg_yolu(),
        "-y",
        "-v", "error",
        "-loop", "1",
        "-i", str(girdi),
        # Meta AAC ses bekler; sessiz bir ses kanali eklemek en guvenlisi
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t", str(sure),
        "-r", "30",
        "-vf", olcek,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",          # 4:2:0 - dokumante edilmis sart
        "-profile:v", "high",
        "-g", "60",                     # closed GOP
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2",
        "-shortest",
        "-movflags", "+faststart",      # moov atom dosyanin BASINDA olmali
        str(cikti),
    ]
    kod, _, hata_metni = _komut_calistir(komut, FFMPEG_ZAMAN_ASIMI_SN)
    if ara_jpeg is not None:
        gecici_dosyayi_sil(ara_jpeg)
    if kod != 0 or not cikti.exists() or cikti.stat().st_size == 0:
        gecici_dosyayi_sil(cikti)
        raise MedyaDogrulamaHatasi(
            f"Gorsel videoya cevrilemedi: {dosya.name} "
            f"(ffmpeg hatasi: {hata_metni.strip()[:200]})"
        )

    kayit.info(
        "Gorsel Story videosuna cevrildi: %s -> %s (%d sn)",
        dosya.name,
        cikti.name,
        sure,
    )
    return cikti


# ---------------------------------------------------------------------------
# 3) Video dogrulama
# ---------------------------------------------------------------------------


def video_dogrula(
    yol: Path | str,
    medya_tipi: MedyaTipi | str,
    *,
    ffprobe_zorunlu: bool = False,
) -> VideoBilgisi:
    """Videonun Meta sinirlarina uygunlugunu YEREL olarak dogrular.

    Kontroller (kaynak: Meta Instagram Platform dokumani):
      - uzanti MP4 / MOV
      - dosya boyutu: REELS 300 MB, STORIES 100 MB
      - sure: REELS 3 sn - 15 dk, STORIES 3 sn - 60 sn
      - en-boy orani: REELS 0.01:1 - 10:1, STORIES 0.1:1 - 10:1 (onerilen 9:16)
      - genislik: en fazla 1920 piksel
      - kare hizi: 23 - 60 FPS
      - video kodegi H264 veya HEVC, ses kodegi AAC
      - moov atom dosyanin basinda mi (uyari; ffmpeg gerektirmeden kontrol edilir)

    ffprobe yoksa sure/oran/kodek kontrolleri YAPILAMAZ: uyari loglanir ve
    dogrulama yumusak gecilir (ffmpeg bu projede zorunlu bagimlilik degildir).
    Sert davranis isteniyorsa ffprobe_zorunlu=True verin.

    Doner:
        VideoBilgisi (uyarilar listesi ile).
    Firlatir:
        MedyaDogrulamaHatasi - sinir asildiginda, Turkce ve cozum onerili mesajla.
    """
    dosya = _dosyayi_dogrula(yol)
    tip = MedyaTipi(str(medya_tipi))
    sinir = sinirlari_getir(tip)
    etiket = "Reels" if tip is MedyaTipi.REELS else "Story"

    uzanti = dosya.suffix.lower()
    if uzanti not in tuple(sinir["uzantilar"]):
        if uzanti in CEVRILMESI_GEREKEN_VIDEO_UZANTILARI:
            raise MedyaDogrulamaHatasi(
                f"{etiket} icin video bicimi desteklenmiyor: {dosya.name} "
                f"({uzanti}). Meta sadece MP4 ve MOV kabul eder. Cevirmek icin: "
                f'ffmpeg -i "{dosya.name}" -c:v libx264 -c:a aac '
                f"-movflags +faststart cikti.mp4"
            )
        raise MedyaDogrulamaHatasi(
            f"{etiket} icin gecersiz dosya uzantisi: {dosya.name} "
            f"(beklenen: {', '.join(sinir['uzantilar'])})"
        )

    boyut = dosya.stat().st_size
    bilgi = VideoBilgisi(yol=dosya, boyut_bayt=boyut)

    if boyut == 0:
        raise MedyaDogrulamaHatasi(f"Dosya bos (0 bayt): {dosya.name}")

    azami_boyut = int(sinir["maks_boyut_bayt"])
    if boyut > azami_boyut:
        raise MedyaDogrulamaHatasi(
            f"{etiket} dosya boyutu siniri asildi: {dosya.name} "
            f"({_bayt_metni(boyut)}). Ust sinir {_bayt_metni(azami_boyut)}. "
            "Videoyu sikistirin veya kisaltin."
        )

    # moov atom kontrolu - ffmpeg GEREKTIRMEZ, dosya yapisindan okunur
    bilgi.moov_basta = moov_basta_mi(dosya)
    if bilgi.moov_basta is False:
        bilgi.uyarilar.append(
            "moov atom dosyanin sonunda. Meta isleme asamasinda "
            "'video format is not supported' (2207026) dondurebilir. Duzeltme: "
            f'ffmpeg -i "{dosya.name}" -c copy -movflags +faststart cikti.mp4'
        )

    veri = _ffprobe_oku(dosya)
    if veri is None:
        mesaj = (
            f"ffprobe bulunamadi veya calistirilamadi; {dosya.name} icin sure, "
            "en-boy orani ve kodek dogrulamasi ATLANDI. Uyumsuz dosya, yukleme "
            "sirasinda degil isleme sirasinda hata verecektir."
        )
        if ffprobe_zorunlu:
            raise MedyaDogrulamaHatasi(mesaj)
        kayit.warning(mesaj)
        bilgi.uyarilar.append(mesaj)
        _uyarilari_logla(bilgi)
        return bilgi

    bilgi.ffprobe_kullanildi = True
    _ffprobe_verisini_isle(veri, bilgi)
    _sinirlari_uygula(bilgi, sinir, etiket)
    _uyarilari_logla(bilgi)
    kayit.info("Video dogrulandi (%s): %s", etiket, bilgi.ozet())
    return bilgi


def _ffprobe_verisini_isle(veri: dict[str, Any], bilgi: VideoBilgisi) -> None:
    """ffprobe JSON ciktisini VideoBilgisi alanlarina doldurur."""
    akislar: list[dict[str, Any]] = list(veri.get("streams") or [])
    video_akisi = next(
        (a for a in akislar if str(a.get("codec_type")) == "video"), None
    )
    ses_akisi = next((a for a in akislar if str(a.get("codec_type")) == "audio"), None)

    if video_akisi is None:
        raise MedyaDogrulamaHatasi(
            f"Dosyada video akisi bulunamadi: {bilgi.yol.name}. "
            "Dosya bozuk olabilir veya sadece ses iceriyor."
        )

    bilgi.video_kodegi = _metin(video_akisi.get("codec_name"))
    bilgi.ses_kodegi = _metin(ses_akisi.get("codec_name")) if ses_akisi else None

    genislik = _tam_sayi(video_akisi.get("width"))
    yukseklik = _tam_sayi(video_akisi.get("height"))
    donus = _donus_acisi(video_akisi)
    if donus in (90, 270) and genislik and yukseklik:
        genislik, yukseklik = yukseklik, genislik   # dik cekilmis telefon videosu
    bilgi.genislik = genislik
    bilgi.yukseklik = yukseklik

    bilgi.fps = _kare_hizi(video_akisi)

    sure = _ondalik(video_akisi.get("duration"))
    if sure is None:
        sure = _ondalik((veri.get("format") or {}).get("duration"))
    bilgi.sure_sn = sure


def _sinirlari_uygula(
    bilgi: VideoBilgisi, sinir: dict[str, Any], etiket: str
) -> None:
    """Olculen degerleri Meta sinirlariyla karsilastirir."""
    ad = bilgi.yol.name

    # -- sure --
    if bilgi.sure_sn is None:
        bilgi.uyarilar.append("Video suresi okunamadi, sure kontrolu atlandi.")
    else:
        en_az = float(sinir["min_sure_sn"])
        en_cok = float(sinir["maks_sure_sn"])
        if bilgi.sure_sn + SURE_TOLERANSI_SN < en_az:
            raise MedyaDogrulamaHatasi(
                f"{etiket} videosu cok kisa: {ad} ({bilgi.sure_sn:.1f} sn). "
                f"En az {en_az:.0f} saniye olmali."
            )
        if bilgi.sure_sn - SURE_TOLERANSI_SN > en_cok:
            raise MedyaDogrulamaHatasi(
                f"{etiket} videosu cok uzun: {ad} ({bilgi.sure_sn:.1f} sn). "
                f"Ust sinir {en_cok:.0f} saniye."
            )

    # -- en-boy orani --
    oran = bilgi.oran
    if oran is None:
        bilgi.uyarilar.append("Cozunurluk okunamadi, en-boy orani kontrolu atlandi.")
    else:
        if not (float(sinir["min_oran"]) <= oran <= float(sinir["maks_oran"])):
            raise MedyaDogrulamaHatasi(
                f"{etiket} en-boy orani gecersiz: {ad} "
                f"({bilgi.genislik}x{bilgi.yukseklik}, oran {oran:.3f}). "
                f"Izin verilen aralik {sinir['min_oran']}:1 - {sinir['maks_oran']}:1, "
                "onerilen 9:16 (dikey)."
            )
        if bilgi.genislik and bilgi.genislik > int(sinir["maks_genislik_px"]):
            raise MedyaDogrulamaHatasi(
                f"{etiket} videosu cok genis: {ad} ({bilgi.genislik} piksel). "
                f"Ust sinir {sinir['maks_genislik_px']} piksel. Ornek cozum: "
                f'ffmpeg -i "{ad}" -vf scale=1080:-2 -c:a copy cikti.mp4'
            )
        if oran > 1.0:
            bilgi.uyarilar.append(
                "Video yatay. Reels ve Story dikey (9:16) icin tasarlandi; "
                "yayin gorunumu kirpilmis gorunebilir."
            )

    # -- kare hizi --
    if bilgi.fps is None or bilgi.fps <= 0:
        bilgi.uyarilar.append("Kare hizi (FPS) okunamadi, kontrol atlandi.")
    else:
        en_az_fps = float(sinir["min_fps"]) - FPS_TOLERANSI
        en_cok_fps = float(sinir["maks_fps"]) + FPS_TOLERANSI
        if not (en_az_fps <= bilgi.fps <= en_cok_fps):
            raise MedyaDogrulamaHatasi(
                f"Kare hizi desteklenmiyor: {ad} ({bilgi.fps:.2f} FPS). "
                f"Izin verilen aralik {sinir['min_fps']}-{sinir['maks_fps']} FPS. "
                f'Duzeltme: ffmpeg -i "{ad}" -r 30 -c:v libx264 -c:a aac '
                "-movflags +faststart cikti.mp4"
            )

    # -- kodekler --
    if bilgi.video_kodegi and bilgi.video_kodegi not in KABUL_EDILEN_VIDEO_KODEKLERI:
        raise MedyaDogrulamaHatasi(
            f"Video kodegi desteklenmiyor: {ad} ({bilgi.video_kodegi}). "
            "Meta sadece H264 ve HEVC kabul eder. Duzeltme: "
            f'ffmpeg -i "{ad}" -c:v libx264 -c:a aac -movflags +faststart cikti.mp4'
        )
    if bilgi.ses_kodegi is None:
        bilgi.uyarilar.append(
            "Videoda ses akisi yok. Yayin genelde calisir ancak bazi hesaplarda "
            "isleme hatasi gorulebilir."
        )
    elif bilgi.ses_kodegi not in KABUL_EDILEN_SES_KODEKLERI:
        raise MedyaDogrulamaHatasi(
            f"Ses kodegi desteklenmiyor: {ad} ({bilgi.ses_kodegi}). "
            "Meta AAC bekler (en fazla 48 kHz, 1-2 kanal). Duzeltme: "
            f'ffmpeg -i "{ad}" -c:v copy -c:a aac -b:a 128k '
            "-movflags +faststart cikti.mp4"
        )


def _uyarilari_logla(bilgi: VideoBilgisi) -> None:
    for uyari in bilgi.uyarilar:
        kayit.warning("%s: %s", bilgi.yol.name, uyari)


def moov_basta_mi(yol: Path | str) -> bool | None:
    """MP4 / MOV dosyasinda moov atomu mdat'tan once mi geliyor?

    Meta dokumani 'moov atom at front' sart kosar (ffmpeg'te -movflags
    +faststart). Bu kontrol dis program gerektirmez, dosyanin kutu (box)
    yapisi okunarak yapilir.

    Doner:
        True  -> moov once geliyor (yayina uygun)
        False -> mdat once geliyor (isleme asamasinda hata riski)
        None  -> tespit edilemedi (dosya MP4/MOV yapisinda degil veya okunamadi)
    """
    dosya = Path(yol)
    try:
        toplam = dosya.stat().st_size
        with dosya.open("rb") as akis:
            konum = 0
            for _ in range(64):          # sonsuz donguye karsi ust sinir
                akis.seek(konum)
                basliklar = akis.read(8)
                if len(basliklar) < 8:
                    return None
                kutu_boyu = int.from_bytes(basliklar[:4], "big")
                kutu_tipi = basliklar[4:8]

                if kutu_tipi == b"moov":
                    return True
                if kutu_tipi == b"mdat":
                    return False

                if kutu_boyu == 1:       # 64 bit genisletilmis boyut
                    genis = akis.read(8)
                    if len(genis) < 8:
                        return None
                    kutu_boyu = int.from_bytes(genis, "big")
                elif kutu_boyu == 0:     # kutu dosya sonuna kadar uzaniyor
                    return None

                if kutu_boyu < 8:
                    return None
                konum += kutu_boyu
                if konum >= toplam:
                    return None
    except OSError:
        return None
    return None


def hizli_baslat_uygula(yol: Path | str, *, hedef: Path | str | None = None) -> Path:
    """moov atomunu dosyanin basina tasir (ffmpeg -c copy -movflags +faststart).

    Yeniden kodlama YAPMAZ, sadece kutulari yeniden siralar; hizlidir ve kalite
    kaybi yoktur. ffmpeg yoksa MedyaDogrulamaHatasi firlatir.
    """
    dosya = _dosyayi_dogrula(yol)
    if not ffmpeg_var_mi():
        raise MedyaDogrulamaHatasi(
            f"moov atomunu basa tasimak icin ffmpeg gerekiyor: {dosya.name}"
        )
    cikti = Path(hedef) if hedef is not None else _gecici_yol(dosya.suffix.lower())
    if hedef is not None:
        cikti.parent.mkdir(parents=True, exist_ok=True)

    komut = [
        _ffmpeg_yolu(),
        "-y",
        "-v", "error",
        "-i", str(dosya),
        "-c", "copy",
        "-movflags", "+faststart",
        str(cikti),
    ]
    kod, _, hata_metni = _komut_calistir(komut, FFMPEG_ZAMAN_ASIMI_SN)
    if kod != 0 or not cikti.exists() or cikti.stat().st_size == 0:
        gecici_dosyayi_sil(cikti)
        raise MedyaDogrulamaHatasi(
            f"moov atomu basa tasinamadi: {dosya.name} "
            f"(ffmpeg hatasi: {hata_metni.strip()[:200]})"
        )
    kayit.info("moov atomu basa tasindi: %s -> %s", dosya.name, cikti.name)
    return cikti


def yayina_hazirla(yol: Path | str, medya_tipi: MedyaTipi | str) -> tuple[Path, VideoBilgisi]:
    """Bir dosyayi yayina hazir hale getirir ve yuklenecek yolu dondurur.

    Akis:
      1. Gorsel ise: kisa bir Story videosuna cevrilir (rupload gorsel kabul etmez).
      2. Video dogrulanir; sinir asimi varsa MedyaDogrulamaHatasi firlatir.
      3. moov atom sonda ise ve ffmpeg varsa dosya sessizce yeniden siralanir.

    Doner:
        (yuklenecek_yol, video_bilgisi)
        yuklenecek_yol gecici bir dosya olabilir; gecici_dosya_mi() ile bakip
        yayin bitince gecici_dosyayi_sil() cagirin.
    """
    dosya = _dosyayi_dogrula(yol)
    tip = MedyaTipi(str(medya_tipi))
    uretilen: Path | None = None

    if dosya_tipi_belirle(dosya) is DosyaTipi.GORSEL:
        if tip is not MedyaTipi.STORIES:
            raise MedyaDogrulamaHatasi(
                f"Gorsel dosya Reels olarak yayinlanamaz: {dosya.name}. "
                "Gorselleri gelen/story klasorune birakin."
            )
        dosya = gorseli_videoya_cevir(dosya)
        uretilen = dosya

    try:
        bilgi = video_dogrula(dosya, tip)
        if bilgi.moov_basta is False and ffmpeg_var_mi():
            duzeltilmis = hizli_baslat_uygula(dosya)
            if uretilen is not None:
                # gorselden uretilen ara video artik gereksiz
                gecici_dosyayi_sil(uretilen)
            bilgi.yol = duzeltilmis
            bilgi.moov_basta = True
            bilgi.boyut_bayt = duzeltilmis.stat().st_size
            return duzeltilmis, bilgi
    except Exception:
        if uretilen is not None:
            gecici_dosyayi_sil(uretilen)
        raise

    return dosya, bilgi


# ---------------------------------------------------------------------------
# 4) Dosya kararliligi (watchdog tuzagi)
# ---------------------------------------------------------------------------


def dosya_stabil_mi(
    yol: Path | str,
    *,
    bekleme_sn: float = 3.0,
    tekrar: int = 3,
    kilit_kontrolu: bool = True,
) -> bool:
    """Dosyanin kopyalanmasi bitti mi?

    watchdog, dosya diske YAZILIRKEN olay uretir. Yarim kopyalanmis bir dosya
    kuyruga alinirsa yukleme bozuk gider veya isleme asamasinda hata doner.
    Bu fonksiyon dosya boyutu ve degisiklik zamani ardisik olcumlerde AYNI
    kalana kadar bekler.

    Parametreler:
        bekleme_sn      - iki olcum arasindaki bekleme (ayarlar: dosya.kararlilik_bekleme_sn)
        tekrar          - kac ardisik ayni olcum isteniyor (ayarlar: dosya.kararlilik_tekrar)
        kilit_kontrolu  - Windows'ta dosya baska bir surec tarafindan yazilmaya
                          acik mi diye bakilsin mi

    Doner:
        True  -> dosya kararli, kuyruga alinabilir
        False -> hala degisiyor, kilitli veya dosya kayboldu
    """
    dosya = Path(yol)
    gereken = max(1, int(tekrar))
    onceki: tuple[int, float] | None = None
    ayni_sayisi = 0

    for _ in range(gereken * 4):     # ust sinir: sonsuz beklemeyi engeller
        olcum = _olcum_al(dosya)
        if olcum is None:
            kayit.debug("Dosya okunamadi veya kayboldu: %s", dosya)
            return False
        if olcum[0] == 0:
            onceki, ayni_sayisi = olcum, 0
            time.sleep(max(0.1, float(bekleme_sn)))
            continue

        if onceki is not None and olcum == onceki:
            ayni_sayisi += 1
            if ayni_sayisi >= gereken - 1:
                if kilit_kontrolu and _dosya_kilitli_mi(dosya):
                    kayit.debug("Dosya hala baska bir surec tarafindan kilitli: %s", dosya)
                    return False
                return True
        else:
            ayni_sayisi = 0

        onceki = olcum
        time.sleep(max(0.1, float(bekleme_sn)))

    kayit.warning(
        "Dosya boyutu kararli hale gelmedi, hala kopyalaniyor olabilir: %s", dosya.name
    )
    return False


def _olcum_al(dosya: Path) -> tuple[int, float] | None:
    """(boyut, degisiklik_zamani) olcumu; dosya yoksa None."""
    try:
        durum = dosya.stat()
    except OSError:
        return None
    if not dosya.is_file():
        return None
    return int(durum.st_size), float(durum.st_mtime)


def _dosya_kilitli_mi(dosya: Path) -> bool:
    """Dosya baska bir surec tarafindan yazim icin aciksa True (Windows).

    Windows'ta kopyalama suren dosya paylasimli acilamaz; bu yuzden 'rb+' ile
    acmayi denemek guvenilir bir gostergedir. Dosya icerigine dokunulmaz.
    """
    try:
        with dosya.open("rb+"):
            return False
    except PermissionError:
        return True
    except OSError:
        # Salt okunur dosya veya izin farki: kararlilik kararini bozmayalim
        return False


# ---------------------------------------------------------------------------
# Gecici dosya yonetimi
# ---------------------------------------------------------------------------


def _gecici_yol(uzanti: str) -> Path:
    """Gecici dosya yolu uretir ve kayda alir (silinmesi cagirana aittir)."""
    tanitici, ham_yol = tempfile.mkstemp(prefix=_GECICI_ONEK, suffix=uzanti)
    os.close(tanitici)   # dosyayi Pillow / ffmpeg yazacak, tanitici burada kapanir
    yol = Path(ham_yol)
    _GECICI_DOSYALAR.add(yol)
    return yol


def gecici_dosya_mi(yol: Path | str | None) -> bool:
    """Verilen yol bu modulun urettigi gecici bir dosya mi?"""
    if yol is None:
        return False
    return Path(yol) in _GECICI_DOSYALAR


def gecici_dosyayi_sil(yol: Path | str | None) -> None:
    """Gecici dosyayi siler (yoksa sessizce gecer)."""
    if yol is None:
        return
    dosya = Path(yol)
    try:
        dosya.unlink(missing_ok=True)
    except OSError as hata:
        kayit.debug("Gecici dosya silinemedi: %s (%s)", dosya, hata)
    _GECICI_DOSYALAR.discard(dosya)


def gecici_dosyalari_temizle() -> int:
    """Uretilmis butun gecici dosyalari siler. Doner: silinen dosya sayisi."""
    sayac = 0
    for dosya in list(_GECICI_DOSYALAR):
        try:
            dosya.unlink(missing_ok=True)
            sayac += 1
        except OSError as hata:
            kayit.debug("Gecici dosya silinemedi: %s (%s)", dosya, hata)
        _GECICI_DOSYALAR.discard(dosya)
    return sayac


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe yardimcilari (ZORUNLU DEGIL)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _ffprobe_yolu() -> str | None:
    return which("ffprobe")


@lru_cache(maxsize=1)
def _ffmpeg_yolu_ic() -> str | None:
    return which("ffmpeg")


def ffprobe_var_mi() -> bool:
    """Sistemde ffprobe kurulu mu?"""
    return _ffprobe_yolu() is not None


def ffmpeg_var_mi() -> bool:
    """Sistemde ffmpeg kurulu mu?"""
    return _ffmpeg_yolu_ic() is not None


def _ffmpeg_yolu() -> str:
    yol = _ffmpeg_yolu_ic()
    if yol is None:  # cagiran taraf ffmpeg_var_mi() ile kontrol etmeli
        raise MedyaDogrulamaHatasi(
            "ffmpeg bulunamadi. Kurulum: winget install Gyan.FFmpeg"
        )
    return yol


def arac_durumu() -> dict[str, bool]:
    """Aciliste loglanabilir arac durumu ozeti."""
    return {"ffmpeg": ffmpeg_var_mi(), "ffprobe": ffprobe_var_mi()}


def _komut_calistir(komut: list[str], zaman_asimi: float) -> tuple[int, str, str]:
    """Dis komutu calistirir. Doner: (cikis_kodu, standart_cikti, hata_ciktisi)."""
    ek: dict[str, Any] = {}
    if sys.platform == "win32":
        # Konsol penceresi acilmasin (arka planda hizmet gibi calisiyor olabilir)
        ek["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        sonuc = subprocess.run(  # noqa: S603 - komut listesi sabit, kabuk yok
            komut,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=zaman_asimi,
            check=False,
            **ek,
        )
    except subprocess.TimeoutExpired:
        return 1, "", f"Komut zaman asimina ugradi ({zaman_asimi:.0f} sn)."
    except OSError as hata:
        return 1, "", f"Komut calistirilamadi: {hata}"
    return sonuc.returncode, sonuc.stdout or "", sonuc.stderr or ""


def _ffprobe_oku(dosya: Path) -> dict[str, Any] | None:
    """ffprobe ciktisini sozluk olarak dondurur; ffprobe yoksa None."""
    yol = _ffprobe_yolu()
    if yol is None:
        return None
    komut = [
        yol,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(dosya),
    ]
    kod, cikti, hata_metni = _komut_calistir(komut, FFPROBE_ZAMAN_ASIMI_SN)
    if kod != 0 or not cikti.strip():
        kayit.warning(
            "ffprobe dosyayi okuyamadi: %s (%s)", dosya.name, hata_metni.strip()[:200]
        )
        return None
    try:
        veri = json.loads(cikti)
    except json.JSONDecodeError as hata:
        kayit.warning("ffprobe ciktisi ayristirilamadi: %s (%s)", dosya.name, hata)
        return None
    return veri if isinstance(veri, dict) else None


def _donus_acisi(akis: dict[str, Any]) -> int:
    """Video akisindaki donus (rotation) acisini derece cinsinden dondurur."""
    etiketler = akis.get("tags") or {}
    ham = etiketler.get("rotate")
    if ham is None:
        for yan in akis.get("side_data_list") or []:
            if isinstance(yan, dict) and "rotation" in yan:
                ham = yan.get("rotation")
                break
    if ham is None:
        return 0
    try:
        aci = int(round(float(ham)))
    except (TypeError, ValueError):
        return 0
    return abs(aci) % 360


def _kare_hizi(akis: dict[str, Any]) -> float | None:
    """'30000/1001' gibi ffprobe kesirini ondalik FPS'e cevirir."""
    for alan in ("avg_frame_rate", "r_frame_rate"):
        ham = akis.get(alan)
        if not ham or not isinstance(ham, str):
            continue
        try:
            if "/" in ham:
                pay, payda = ham.split("/", 1)
                payda_sayi = float(payda)
                if payda_sayi == 0:
                    continue
                deger = float(pay) / payda_sayi
            else:
                deger = float(ham)
        except ValueError:
            continue
        if deger > 0:
            return deger
    return None


def _tam_sayi(deger: Any) -> int | None:
    try:
        return int(deger)
    except (TypeError, ValueError):
        return None


def _ondalik(deger: Any) -> float | None:
    try:
        sayi = float(deger)
    except (TypeError, ValueError):
        return None
    return sayi if sayi > 0 else None


def _metin(deger: Any) -> str | None:
    if deger is None:
        return None
    metin = str(deger).strip().lower()
    return metin or None


def _bayt_metni(bayt: int | float) -> str:
    """Bayt sayisini insan okunur metne cevirir (log ve hata mesajlari icin)."""
    sayi = float(bayt)
    if sayi >= 1024 * 1024:
        return f"{sayi / (1024 * 1024):.1f} MB"
    if sayi >= 1024:
        return f"{sayi / 1024:.1f} KB"
    return f"{int(sayi)} bayt"


def _dosyayi_dogrula(yol: Path | str) -> Path:
    """Dosya var mi ve gercekten dosya mi? Degilse Turkce hata firlatir."""
    dosya = Path(yol)
    if not dosya.exists():
        raise MedyaDogrulamaHatasi(f"Dosya bulunamadi: {dosya}")
    if not dosya.is_file():
        raise MedyaDogrulamaHatasi(f"Bu bir dosya degil: {dosya}")
    return dosya


__all__ = [
    "ACIKLAMA_UZANTILARI",
    "CEVRILEBILIR_GORSEL_UZANTILARI",
    "CEVRILMESI_GEREKEN_VIDEO_UZANTILARI",
    "DosyaTipi",
    "GORSEL_AZAMI_BAYT",
    "GORSEL_UZANTILARI",
    "KABUL_EDILEN_SES_KODEKLERI",
    "KABUL_EDILEN_VIDEO_KODEKLERI",
    "VIDEO_UZANTILARI",
    "VideoBilgisi",
    "arac_durumu",
    "dosya_stabil_mi",
    "dosya_tipi_belirle",
    "ffmpeg_var_mi",
    "ffprobe_var_mi",
    "gecici_dosya_mi",
    "gecici_dosyalari_temizle",
    "gecici_dosyayi_sil",
    "gorsel_dogrula",
    "gorsel_mi",
    "gorseli_videoya_cevir",
    "hizli_baslat_uygula",
    "jpeg_e_cevir",
    "meta_kabul_ediyor_mu",
    "moov_basta_mi",
    "video_dogrula",
    "video_mu",
    "yayina_hazirla",
]
