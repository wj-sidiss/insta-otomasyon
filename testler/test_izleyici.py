# -*- coding: ascii -*-
"""Klasor izleyici -> kuyruk baglantisinin testleri.

Burada watchdog olaylari kullanilmaz (platforma bagli ve yavastir); bunun
yerine tek_seferlik_tarama() ve gorevi_kuyruga_al() dogrudan cagrilir.
Bu iki fonksiyon izleyicinin isci ipliginin de kullandigi yoldur.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from conftest import ayari_degistir, video_dosyasi_olustur
from src import db
from src.ayarlar import Ayarlar
from src.izleyici import gorevi_kuyruga_al, klasordeki_adaylar, tek_seferlik_tarama
from src.modeller import Durum, MedyaTipi

ISTANBUL = ZoneInfo("Europe/Istanbul")


def test_tarama_uygun_dosyalari_kuyruga_alir(ayar: Ayarlar) -> None:
    video_dosyasi_olustur(ayar.klasorler.gelen_reels, "tanitim.mp4")
    video_dosyasi_olustur(ayar.klasorler.gelen_story, "hikaye.mp4")

    eklenen = tek_seferlik_tarama(ayar, kararlilik_bekle=False)

    assert eklenen == 2
    ozet = db.kuyruk_ozeti(ayar.db_yolu)
    assert ozet[str(Durum.BEKLIYOR)] == 2

    tipler = {g.dosya_adi: g.medya_tipi for g in db.bekleyenleri_getir(ayar.db_yolu)}
    assert tipler["tanitim.mp4"] is MedyaTipi.REELS
    assert tipler["hikaye.mp4"] is MedyaTipi.STORIES


def test_tarama_ayni_dosyayi_ikinci_kez_eklemez(ayar: Ayarlar) -> None:
    video_dosyasi_olustur(ayar.klasorler.gelen_reels, "tanitim.mp4")

    assert tek_seferlik_tarama(ayar, kararlilik_bekle=False) == 1
    assert tek_seferlik_tarama(ayar, kararlilik_bekle=False) == 0


def test_aciklama_dosyasi_gorev_olarak_eklenmez(ayar: Ayarlar) -> None:
    klasor = ayar.klasorler.gelen_reels
    video_dosyasi_olustur(klasor, "tanitim.mp4")
    (klasor / "tanitim.txt").write_text("Aciklama metni", encoding="utf-8")

    assert tek_seferlik_tarama(ayar, kararlilik_bekle=False) == 1

    gorevler = db.bekleyenleri_getir(ayar.db_yolu)
    assert len(gorevler) == 1
    assert gorevler[0].aciklama == "Aciklama metni"


def test_gecici_ve_gizli_dosyalar_atlanir(ayar: Ayarlar) -> None:
    klasor = ayar.klasorler.gelen_reels
    for ad in ("tanitim.mp4.part", ".gizli.mp4", "_taslak.mp4", "belge.pdf"):
        (klasor / ad).write_bytes(b"x" * 64)

    assert tek_seferlik_tarama(ayar, kararlilik_bekle=False) == 0
    assert klasordeki_adaylar(ayar) == []


def test_story_icin_aciklama_kuyruga_yazilmaz(ayar: Ayarlar) -> None:
    """Instagram Story'de altyazi alani yok; yanlislikla gonderilmemeli."""
    klasor = ayar.klasorler.gelen_story
    video = video_dosyasi_olustur(klasor, "hikaye.mp4")
    (klasor / "hikaye.txt").write_text("Bu metin kullanilmamali", encoding="utf-8")

    gorev_id = gorevi_kuyruga_al(video, MedyaTipi.STORIES, ayar)

    assert gorev_id is not None
    assert db.gorev_getir(ayar.db_yolu, gorev_id).aciklama is None


def test_tarihli_dosya_adindan_plan_yazilir(ayar: Ayarlar) -> None:
    video = video_dosyasi_olustur(
        ayar.klasorler.gelen_reels, "2026-09-25_1830_tanitim.mp4"
    )

    gorev_id = gorevi_kuyruga_al(video, MedyaTipi.REELS, ayar)

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.planlanan_zaman is not None
    assert gorev.planlanan_zaman.astimezone(ISTANBUL) == datetime(
        2026, 9, 25, 18, 30, tzinfo=ISTANBUL
    )


def test_tarihsiz_dosya_plansiz_kuyruga_girer(ayar: Ayarlar) -> None:
    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "tanitim.mp4")

    gorev_id = gorevi_kuyruga_al(video, MedyaTipi.REELS, ayar)

    assert db.gorev_getir(ayar.db_yolu, gorev_id).planlanan_zaman is None


def test_reels_klasorundeki_gorsel_kuyruga_alinmaz(ayar: Ayarlar) -> None:
    gorsel = ayar.klasorler.gelen_reels / "kapak.jpg"
    gorsel.write_bytes(b"x" * 64)

    assert gorevi_kuyruga_al(gorsel, MedyaTipi.REELS, ayar) is None


def test_story_klasorundeki_gorsel_kuyruga_alinir(ayar: Ayarlar) -> None:
    gorsel = ayar.klasorler.gelen_story / "kapak.jpg"
    gorsel.write_bytes(b"x" * 64)

    assert gorevi_kuyruga_al(gorsel, MedyaTipi.STORIES, ayar) is not None


def test_varsayilan_aciklama_sablonu_kullanilir(ayar: Ayarlar) -> None:
    yeni = ayari_degistir(ayar, yayin={"varsayilan_aciklama": "Yeni video: {ad}"})
    video = video_dosyasi_olustur(yeni.klasorler.gelen_reels, "tanitim.mp4")

    gorev_id = gorevi_kuyruga_al(video, MedyaTipi.REELS, yeni)

    assert db.gorev_getir(yeni.db_yolu, gorev_id).aciklama == "Yeni video: tanitim"


def test_adaylar_eskiden_yeniye_siralanir(ayar: Ayarlar) -> None:
    import os
    import time

    klasor = ayar.klasorler.gelen_reels
    eski = video_dosyasi_olustur(klasor, "eski.mp4")
    video_dosyasi_olustur(klasor, "yeni.mp4")
    gecmis = time.time() - 3600
    os.utime(eski, (gecmis, gecmis))

    adaylar = [yol.name for yol, _ in klasordeki_adaylar(ayar)]
    assert adaylar == ["eski.mp4", "yeni.mp4"]


def test_gecmis_tarihli_dosya_yine_de_kuyruga_girer(ayar: Ayarlar) -> None:
    """Gecmis tarihli ad hata degildir; gorev ilk firsatta yayinlanir."""
    video = video_dosyasi_olustur(
        ayar.klasorler.gelen_reels, "2020-01-01_0900_eski.mp4"
    )

    gorev_id = gorevi_kuyruga_al(video, MedyaTipi.REELS, ayar)

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev is not None
    assert gorev.planlanan_zaman is not None
    assert gorev.zamani_geldi_mi() is True


def test_olmayan_dosya_kuyruga_alinmaz(ayar: Ayarlar) -> None:
    yok = ayar.klasorler.gelen_reels / "yok.mp4"
    assert gorevi_kuyruga_al(yok, MedyaTipi.REELS, ayar) is None
