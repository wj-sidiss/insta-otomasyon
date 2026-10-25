# -*- coding: ascii -*-
"""Dosya adi ayristirma ve aciklama (caption) eslestirme testleri.

Kapsanan durumlar:
  - tarihli adlar (birkac farkli yazim)
  - tarihsiz adlar
  - bozuk / gecersiz tarihli adlar
  - aciklama dosyasinin eslestirilmesi (birebir ad ve tarih oneksiz ad)
  - varsayilan aciklama sablonu
  - 2200 karakter siniri
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import ayari_degistir
from src.ayarlar import Ayarlar
from src.izleyici import (
    aciklama_dosyasi_adaylari,
    aciklamayi_coz,
    dosya_adini_ayristir,
    dosya_kabul_edilir_mi,
    planlanan_zamani_hesapla,
    sablonu_uygula,
)
from src.modeller import CAPTION_MAKS_KARAKTER, MedyaTipi

ISTANBUL = ZoneInfo("Europe/Istanbul")


# ---------------------------------------------------------------------------
# Tarihsiz adlar
# ---------------------------------------------------------------------------


def test_tarihsiz_ad_plansiz_kalir() -> None:
    bilgi = dosya_adini_ayristir("tanitim.mp4", zaman_dilimi=ISTANBUL)
    assert bilgi.planlanan_zaman is None
    assert bilgi.tarih is None
    assert bilgi.planlanmis_mi is False
    assert bilgi.govde == "tanitim"
    assert bilgi.uzanti == ".mp4"
    assert bilgi.uyari is None


def test_tarihsiz_ad_bosluklu() -> None:
    bilgi = dosya_adini_ayristir("yaz kampanyasi son.mov", zaman_dilimi=ISTANBUL)
    assert bilgi.planlanan_zaman is None
    assert bilgi.govde == "yaz kampanyasi son"
    assert bilgi.uzanti == ".mov"


def test_uzantisiz_ad() -> None:
    bilgi = dosya_adini_ayristir("video", zaman_dilimi=ISTANBUL)
    assert bilgi.uzanti == ""
    assert bilgi.govde == "video"
    assert bilgi.planlanan_zaman is None


def test_tam_yol_verilse_de_sadece_ad_kullanilir() -> None:
    bilgi = dosya_adini_ayristir(
        Path("C:/gelen/reels/2026-09-25_1830_tanitim.mp4"), zaman_dilimi=ISTANBUL
    )
    assert bilgi.ham_ad == "2026-09-25_1830_tanitim.mp4"
    assert bilgi.govde == "tanitim"


# ---------------------------------------------------------------------------
# Tarihli adlar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ad",
    [
        "2026-09-25_1830_tanitim.mp4",
        "20260925_1830_tanitim.mp4",
        "2026-09-25_18-30_tanitim.mp4",
        "2026-09-25_18:30_tanitim.mp4",
        "2026.09.25_1830_tanitim.mp4",
        "2026-09-25T1830_tanitim.mp4",
    ],
)
def test_tarih_ve_saat_ayristirilir(ad: str) -> None:
    bilgi = dosya_adini_ayristir(ad, zaman_dilimi=ISTANBUL)
    assert bilgi.planlanan_zaman == datetime(2026, 9, 25, 18, 30, tzinfo=ISTANBUL)
    assert bilgi.tarih == date(2026, 9, 25)
    assert bilgi.sadece_tarih is False
    assert bilgi.govde == "tanitim"
    assert bilgi.planlanmis_mi is True


def test_saniyeli_ad_saniyeyi_yok_sayar() -> None:
    bilgi = dosya_adini_ayristir("2026-09-25_183045_tanitim.mp4", zaman_dilimi=ISTANBUL)
    assert bilgi.planlanan_zaman == datetime(2026, 9, 25, 18, 30, tzinfo=ISTANBUL)


def test_sadece_tarihli_ad() -> None:
    bilgi = dosya_adini_ayristir("2026-09-25_tanitim.mp4", zaman_dilimi=ISTANBUL)
    assert bilgi.planlanan_zaman is None
    assert bilgi.tarih == date(2026, 9, 25)
    assert bilgi.sadece_tarih is True
    assert bilgi.govde == "tanitim"


def test_sadece_tarih_govdesiz() -> None:
    bilgi = dosya_adini_ayristir("2026-09-25.mp4", zaman_dilimi=ISTANBUL)
    assert bilgi.tarih == date(2026, 9, 25)
    assert bilgi.sadece_tarih is True


def test_zaman_dilimi_metin_olarak_verilebilir() -> None:
    bilgi = dosya_adini_ayristir(
        "2026-09-25_0900_sabah.mp4", zaman_dilimi="Europe/Istanbul"
    )
    assert bilgi.planlanan_zaman is not None
    assert bilgi.planlanan_zaman.utcoffset() is not None


# ---------------------------------------------------------------------------
# Bozuk adlar - dosya ASLA atlanmaz, sadece plansiz kalir
# ---------------------------------------------------------------------------


def test_gecersiz_tarih_uyari_verir_ama_dosya_atlanmaz() -> None:
    bilgi = dosya_adini_ayristir("2026-13-45_bozuk.mp4", zaman_dilimi=ISTANBUL)
    assert bilgi.planlanan_zaman is None
    assert bilgi.tarih is None
    assert bilgi.uyari is not None
    assert "gecersiz" in bilgi.uyari.lower()
    assert bilgi.govde == "bozuk"


def test_gecersiz_saat_sadece_tarihe_duser() -> None:
    bilgi = dosya_adini_ayristir("2026-09-25_2599_bozuk.mp4", zaman_dilimi=ISTANBUL)
    assert bilgi.planlanan_zaman is None
    assert bilgi.tarih == date(2026, 9, 25)
    assert bilgi.sadece_tarih is True
    assert bilgi.uyari is not None


def test_yariyarim_tarih_desene_uymaz() -> None:
    bilgi = dosya_adini_ayristir("2026-09_tanitim.mp4", zaman_dilimi=ISTANBUL)
    assert bilgi.planlanan_zaman is None
    assert bilgi.tarih is None
    assert bilgi.uyari is None  # desene hic uymadi, bu bir hata degil


# ---------------------------------------------------------------------------
# Planlanan zamanin hesaplanmasi
# ---------------------------------------------------------------------------


def test_sadece_tarihli_dosya_gunun_ilk_uygun_slotuna_gider(ayar: Ayarlar) -> None:
    bilgi = dosya_adini_ayristir("2026-09-25_tanitim.mp4", zaman_dilimi=ISTANBUL)
    an = datetime(2026, 9, 25, 8, 0, tzinfo=ISTANBUL)
    plan = planlanan_zamani_hesapla(bilgi, ayar, an=an)
    assert plan == datetime(2026, 9, 25, 12, 0, tzinfo=ISTANBUL)


def test_gunun_ilk_slotu_gectiyse_ikinci_slot_secilir(ayar: Ayarlar) -> None:
    bilgi = dosya_adini_ayristir("2026-09-25_tanitim.mp4", zaman_dilimi=ISTANBUL)
    an = datetime(2026, 9, 25, 13, 0, tzinfo=ISTANBUL)
    plan = planlanan_zamani_hesapla(bilgi, ayar, an=an)
    assert plan == datetime(2026, 9, 25, 19, 0, tzinfo=ISTANBUL)


def test_tum_slotlar_gectiyse_gunun_ilk_slotu_kullanilir(ayar: Ayarlar) -> None:
    bilgi = dosya_adini_ayristir("2026-09-25_tanitim.mp4", zaman_dilimi=ISTANBUL)
    an = datetime(2026, 9, 25, 23, 30, tzinfo=ISTANBUL)
    plan = planlanan_zamani_hesapla(bilgi, ayar, an=an)
    # Gecmis tarih: gorev ilk firsatta yayinlanir
    assert plan == datetime(2026, 9, 25, 12, 0, tzinfo=ISTANBUL)


def test_tarihsiz_dosya_icin_plan_yoktur(ayar: Ayarlar) -> None:
    bilgi = dosya_adini_ayristir("tanitim.mp4", zaman_dilimi=ISTANBUL)
    assert planlanan_zamani_hesapla(bilgi, ayar) is None


# ---------------------------------------------------------------------------
# Aciklama (caption) eslestirme
# ---------------------------------------------------------------------------


def test_aciklama_adaylari_iki_secenek_uretir() -> None:
    bilgi = dosya_adini_ayristir("2026-09-25_1830_tanitim.mp4", zaman_dilimi=ISTANBUL)
    adaylar = aciklama_dosyasi_adaylari(
        Path("/gelen/reels/2026-09-25_1830_tanitim.mp4"), bilgi
    )
    adlar = [aday.name for aday in adaylar]
    assert adlar == ["2026-09-25_1830_tanitim.txt", "tanitim.txt"]


def test_birebir_adli_aciklama_okunur(ayar: Ayarlar) -> None:
    klasor = ayar.klasorler.gelen_reels
    video = klasor / "2026-09-25_1830_tanitim.mp4"
    video.write_bytes(b"x" * 16)
    (klasor / "2026-09-25_1830_tanitim.txt").write_text(
        "Merhaba dunya #tanitim", encoding="utf-8"
    )
    bilgi = dosya_adini_ayristir(video.name, zaman_dilimi=ISTANBUL)

    metin, kaynak = aciklamayi_coz(video, bilgi, ayar, MedyaTipi.REELS)
    assert metin == "Merhaba dunya #tanitim"
    assert kaynak is not None and kaynak.name == "2026-09-25_1830_tanitim.txt"


def test_tarih_oneksiz_adli_aciklama_da_bulunur(ayar: Ayarlar) -> None:
    klasor = ayar.klasorler.gelen_reels
    video = klasor / "2026-09-25_1830_tanitim.mp4"
    video.write_bytes(b"x" * 16)
    (klasor / "tanitim.txt").write_text("Ortak aciklama", encoding="utf-8")
    bilgi = dosya_adini_ayristir(video.name, zaman_dilimi=ISTANBUL)

    metin, kaynak = aciklamayi_coz(video, bilgi, ayar, MedyaTipi.REELS)
    assert metin == "Ortak aciklama"
    assert kaynak is not None and kaynak.name == "tanitim.txt"


def test_aciklama_yoksa_none_doner(ayar: Ayarlar) -> None:
    klasor = ayar.klasorler.gelen_reels
    video = klasor / "tanitim.mp4"
    video.write_bytes(b"x" * 16)
    bilgi = dosya_adini_ayristir(video.name, zaman_dilimi=ISTANBUL)

    metin, kaynak = aciklamayi_coz(video, bilgi, ayar, MedyaTipi.REELS)
    assert metin is None
    assert kaynak is None


def test_bos_aciklama_dosyasi_varsayilan_sablona_duser(ayar: Ayarlar) -> None:
    yeni_ayar = ayari_degistir(
        ayar, yayin={"varsayilan_aciklama": "{ad} | {tip} | {tarih}"}
    )
    klasor = yeni_ayar.klasorler.gelen_reels
    video = klasor / "2026-09-25_1830_tanitim.mp4"
    video.write_bytes(b"x" * 16)
    (klasor / "2026-09-25_1830_tanitim.txt").write_text("   \n", encoding="utf-8")
    bilgi = dosya_adini_ayristir(video.name, zaman_dilimi=ISTANBUL)

    metin, kaynak = aciklamayi_coz(video, bilgi, yeni_ayar, MedyaTipi.REELS)
    assert metin == "tanitim | REELS | 25.09.2026"
    assert kaynak is None  # sablondan uretildi, dosyadan degil


def test_sablon_bilinmeyen_alani_bozmaz() -> None:
    bilgi = dosya_adini_ayristir("2026-09-25_1830_tanitim.mp4", zaman_dilimi=ISTANBUL)
    metin = sablonu_uygula(
        "{ad} - {bilinmeyen} - {saat}", bilgi, MedyaTipi.REELS, zaman_dilimi=ISTANBUL
    )
    assert metin == "tanitim - {bilinmeyen} - 18:30"


def test_cok_uzun_aciklama_kirpilir(ayar: Ayarlar) -> None:
    klasor = ayar.klasorler.gelen_reels
    video = klasor / "uzun.mp4"
    video.write_bytes(b"x" * 16)
    uzun_metin = ("kelime " * 600).strip()   # ~4200 karakter
    assert len(uzun_metin) > CAPTION_MAKS_KARAKTER
    (klasor / "uzun.txt").write_text(uzun_metin, encoding="utf-8")
    bilgi = dosya_adini_ayristir(video.name, zaman_dilimi=ISTANBUL)

    metin, _ = aciklamayi_coz(video, bilgi, ayar, MedyaTipi.REELS)
    assert metin is not None
    assert len(metin) <= CAPTION_MAKS_KARAKTER


def test_aciklama_dosyasi_utf8_bom_ile_okunur(ayar: Ayarlar) -> None:
    klasor = ayar.klasorler.gelen_reels
    video = klasor / "bom.mp4"
    video.write_bytes(b"x" * 16)
    (klasor / "bom.txt").write_bytes(b"\xef\xbb\xbfBom ile yazilmis metin")
    bilgi = dosya_adini_ayristir(video.name, zaman_dilimi=ISTANBUL)

    metin, _ = aciklamayi_coz(video, bilgi, ayar, MedyaTipi.REELS)
    assert metin == "Bom ile yazilmis metin"


# ---------------------------------------------------------------------------
# Dosya kabul kurallari
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ad,tip,beklenen",
    [
        ("tanitim.mp4", MedyaTipi.REELS, True),
        ("tanitim.mov", MedyaTipi.REELS, True),
        ("tanitim.MP4", MedyaTipi.REELS, True),
        ("tanitim.txt", MedyaTipi.REELS, False),
        ("tanitim.mkv", MedyaTipi.REELS, False),
        ("tanitim.tmp", MedyaTipi.REELS, False),
        ("tanitim.mp4.part", MedyaTipi.REELS, False),
        (".gizli.mp4", MedyaTipi.REELS, False),
        ("_taslak.mp4", MedyaTipi.REELS, False),
        ("~yedek.mp4", MedyaTipi.REELS, False),
        ("kapak.jpg", MedyaTipi.REELS, False),      # Reels gorsel kabul etmez
        ("kapak.jpg", MedyaTipi.STORIES, True),     # Story gorsel kabul eder
        ("kapak.png", MedyaTipi.STORIES, True),
    ],
)
def test_dosya_kabul_kurallari(
    ayar: Ayarlar, ad: str, tip: MedyaTipi, beklenen: bool
) -> None:
    yol = ayar.klasorler.gelen_reels / ad
    uygun, sebep = dosya_kabul_edilir_mi(yol, ayar, tip)
    assert uygun is beklenen, f"{ad} -> {sebep}"
    assert sebep  # sebep her zaman Turkce bir metin olmali
