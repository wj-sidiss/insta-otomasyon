# -*- coding: ascii -*-
"""SQLite kuyrugu testleri: durum gecisleri ve UNIQUE korumasi.

Kapsanan durumlar:
  - ayni dosya iki kez kuyruga giremez (UNIQUE kisiti)
  - goreli / mutlak yol ayni satira duser
  - BEKLIYOR -> YUKLENIYOR -> HAZIRLANIYOR -> YAYINLANDI akisi
  - YAYINLANDI durumunda yayin_zamani otomatik yazilir
  - siradaki_gorevi_al ile atomik sahiplenme
  - planlanan zamani gelmemis gorev siraya girmez
  - azami deneme siniri
  - 24 saatlik kayan pencere sayaci
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from conftest import gorev_ekle
from src.db import (
    bekleyenleri_getir,
    dosya_yolunu_guncelle,
    dosyaya_gore_getir,
    durum_guncelle,
    gorev_getir,
    gorev_sil,
    gorevleri_listele,
    kuyruga_ekle,
    kuyruk_ozeti,
    siradaki_gorevi_al,
    son_24_saatte_yayinlanan_sayisi,
    veritabanini_hazirla,
)
from src.modeller import Durum, MedyaTipi, simdi


# ---------------------------------------------------------------------------
# Sema
# ---------------------------------------------------------------------------


def test_sema_iki_kez_hazirlanabilir(tmp_path: Path) -> None:
    yol = tmp_path / "kuyruk.db"
    veritabanini_hazirla(yol)
    veritabanini_hazirla(yol)   # tekrar cagirmak hata vermemeli
    assert yol.exists()
    assert kuyruk_ozeti(yol) == {str(d): 0 for d in Durum}


def test_veritabani_klasoru_otomatik_olusur(tmp_path: Path) -> None:
    yol = tmp_path / "olmayan" / "alt" / "kuyruk.db"
    veritabanini_hazirla(yol)
    assert yol.exists()


# ---------------------------------------------------------------------------
# UNIQUE korumasi
# ---------------------------------------------------------------------------


def test_ayni_dosya_iki_kez_eklenemez(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")

    birinci = kuyruga_ekle(db_yolu, video, MedyaTipi.REELS)
    ikinci = kuyruga_ekle(db_yolu, video, MedyaTipi.REELS)

    assert isinstance(birinci, int) and birinci > 0
    assert ikinci is None, "UNIQUE kisiti ayni dosyayi ikinci kez kabul etmemeli"
    assert kuyruk_ozeti(db_yolu)[str(Durum.BEKLIYOR)] == 1


def test_goreli_ve_mutlak_yol_ayni_satira_duser(
    db_yolu: Path, tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    monkeypatch.chdir(tmp_path)

    birinci = kuyruga_ekle(db_yolu, video, MedyaTipi.REELS)
    ikinci = kuyruga_ekle(db_yolu, Path("tanitim.mp4"), MedyaTipi.REELS)

    assert birinci is not None
    assert ikinci is None


def test_dosyaya_gore_getir(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)

    gorev = dosyaya_gore_getir(db_yolu, video)
    assert gorev is not None
    assert gorev.id == gorev_id
    assert gorev.medya_tipi is MedyaTipi.REELS
    assert dosyaya_gore_getir(db_yolu, tmp_path / "yok.mp4") is None


def test_farkli_dosyalar_ayri_satir(db_yolu: Path, tmp_path: Path) -> None:
    for ad in ("bir.mp4", "iki.mp4", "uc.mp4"):
        (tmp_path / ad).write_bytes(b"x")
        assert kuyruga_ekle(db_yolu, tmp_path / ad, MedyaTipi.REELS) is not None
    assert kuyruk_ozeti(db_yolu)[str(Durum.BEKLIYOR)] == 3


# ---------------------------------------------------------------------------
# Durum gecisleri
# ---------------------------------------------------------------------------


def test_tam_durum_akisi(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)

    assert gorev_getir(db_yolu, gorev_id).durum is Durum.BEKLIYOR

    an = simdi()
    assert durum_guncelle(
        db_yolu, gorev_id, Durum.YUKLENIYOR, container_id="k-1", container_zamani=an
    )
    gorev = gorev_getir(db_yolu, gorev_id)
    assert gorev.durum is Durum.YUKLENIYOR
    assert gorev.container_id == "k-1"
    assert gorev.container_zamani == an

    assert durum_guncelle(db_yolu, gorev_id, Durum.HAZIRLANIYOR)
    assert gorev_getir(db_yolu, gorev_id).durum is Durum.HAZIRLANIYOR

    assert durum_guncelle(
        db_yolu, gorev_id, Durum.YAYINLANDI, medya_id="m-1", permalink="https://x/y"
    )
    gorev = gorev_getir(db_yolu, gorev_id)
    assert gorev.durum is Durum.YAYINLANDI
    assert gorev.medya_id == "m-1"
    assert gorev.permalink == "https://x/y"
    # YAYINLANDI durumunda yayin_zamani OTOMATIK yazilmali (kota sayaci icin)
    assert gorev.yayin_zamani is not None


def test_dokunma_nobetcisi_alanlari_korur(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video, aciklama="ilk metin")

    durum_guncelle(db_yolu, gorev_id, Durum.YUKLENIYOR, container_id="k-1")
    durum_guncelle(db_yolu, gorev_id, Durum.HAZIRLANIYOR)   # container_id verilmedi

    gorev = gorev_getir(db_yolu, gorev_id)
    assert gorev.container_id == "k-1", "Verilmeyen alana dokunulmamali"
    assert gorev.aciklama == "ilk metin"


def test_alani_acikca_bosaltmak(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)

    durum_guncelle(db_yolu, gorev_id, Durum.YUKLENIYOR, container_id="k-1")
    durum_guncelle(db_yolu, gorev_id, Durum.BEKLIYOR, container_id=None)

    assert gorev_getir(db_yolu, gorev_id).container_id is None


def test_deneme_sayaci(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)

    durum_guncelle(db_yolu, gorev_id, deneme_artir=True)
    durum_guncelle(db_yolu, gorev_id, deneme_artir=True)
    assert gorev_getir(db_yolu, gorev_id).deneme_sayisi == 2

    durum_guncelle(db_yolu, gorev_id, deneme_sifirla=True)
    assert gorev_getir(db_yolu, gorev_id).deneme_sayisi == 0


def test_olmayan_gorev_guncellenemez(db_yolu: Path) -> None:
    assert durum_guncelle(db_yolu, 9999, Durum.YAYINLANDI) is False
    assert gorev_sil(db_yolu, 9999) is False


# ---------------------------------------------------------------------------
# Sira ve sahiplenme
# ---------------------------------------------------------------------------


def test_sahiplenme_durumu_yukleniyor_yapar(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_ekle(db_yolu, video)

    gorev = siradaki_gorevi_al(db_yolu, sahiplen=True)
    assert gorev is not None
    assert gorev.durum is Durum.YUKLENIYOR
    assert gorev_getir(db_yolu, gorev.id).durum is Durum.YUKLENIYOR


def test_sahiplenme_yarim_kalan_gorevin_durumunu_bozmaz(
    db_yolu: Path, tmp_path: Path
) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)
    durum_guncelle(db_yolu, gorev_id, Durum.HAZIRLANIYOR)

    gorev = siradaki_gorevi_al(db_yolu, sahiplen=True)
    assert gorev is not None
    assert gorev.durum is Durum.HAZIRLANIYOR, "Yarim is kaldigi adimdan surmeli"


def test_gelecek_planli_gorev_siraya_girmez(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_ekle(db_yolu, video, planlanan_zaman=simdi() + timedelta(hours=2))

    assert siradaki_gorevi_al(db_yolu) is None
    assert siradaki_gorevi_al(db_yolu, an=simdi() + timedelta(hours=3)) is not None


def test_gecmis_planli_gorev_once_gelir(db_yolu: Path, tmp_path: Path) -> None:
    eski = tmp_path / "eski.mp4"
    yeni = tmp_path / "yeni.mp4"
    for yol in (eski, yeni):
        yol.write_bytes(b"x")
    gorev_ekle(db_yolu, yeni)   # plansiz
    gorev_ekle(db_yolu, eski, planlanan_zaman=simdi() - timedelta(hours=1))

    gorev = siradaki_gorevi_al(db_yolu)
    assert gorev is not None
    assert gorev.dosya_adi == "eski.mp4", "Planli gorevler plansizlardan once gelir"


def test_azami_deneme_siniri(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)
    for _ in range(3):
        durum_guncelle(db_yolu, gorev_id, deneme_artir=True)

    assert siradaki_gorevi_al(db_yolu, azami_deneme=3) is None
    assert siradaki_gorevi_al(db_yolu, azami_deneme=4) is not None


def test_sonlanmis_gorevler_siraya_girmez(db_yolu: Path, tmp_path: Path) -> None:
    for ad, durum in (("y.mp4", Durum.YAYINLANDI), ("h.mp4", Durum.HATA)):
        yol = tmp_path / ad
        yol.write_bytes(b"x")
        gorev_id = gorev_ekle(db_yolu, yol)
        durum_guncelle(db_yolu, gorev_id, durum)

    assert siradaki_gorevi_al(db_yolu) is None
    assert bekleyenleri_getir(db_yolu) == []


# ---------------------------------------------------------------------------
# Kota sayaci ve raporlama
# ---------------------------------------------------------------------------


def test_24_saatlik_kayan_pencere_sayaci(db_yolu: Path, tmp_path: Path) -> None:
    su_an = simdi()
    yeni = tmp_path / "yeni.mp4"
    eski = tmp_path / "eski.mp4"
    for yol in (yeni, eski):
        yol.write_bytes(b"x")

    yeni_id = gorev_ekle(db_yolu, yeni)
    eski_id = gorev_ekle(db_yolu, eski)
    durum_guncelle(
        db_yolu, yeni_id, Durum.YAYINLANDI, yayin_zamani=su_an - timedelta(hours=2)
    )
    durum_guncelle(
        db_yolu, eski_id, Durum.YAYINLANDI, yayin_zamani=su_an - timedelta(hours=30)
    )

    assert son_24_saatte_yayinlanan_sayisi(db_yolu, su_an) == 1


def test_kuyruk_ozeti_butun_durumlari_icerir(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)
    durum_guncelle(db_yolu, gorev_id, Durum.HATA)

    ozet = kuyruk_ozeti(db_yolu)
    assert set(ozet) == {str(d) for d in Durum}
    assert ozet[str(Durum.HATA)] == 1
    assert ozet[str(Durum.BEKLIYOR)] == 0


def test_dosya_yolu_guncelleme(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)

    yeni_yol = tmp_path / "yayinlandi" / "tanitim.mp4"
    yeni_yol.parent.mkdir(parents=True, exist_ok=True)
    assert dosya_yolunu_guncelle(db_yolu, gorev_id, yeni_yol)

    gorev = gorev_getir(db_yolu, gorev_id)
    assert gorev.dosya_yolu == yeni_yol.resolve()
    # Eski yol artik bos: ayni dosya tekrar kuyruga alinabilir
    assert kuyruga_ekle(db_yolu, video, MedyaTipi.REELS) is not None


def test_gorevleri_listele_en_yeni_basta(db_yolu: Path, tmp_path: Path) -> None:
    for ad in ("bir.mp4", "iki.mp4"):
        (tmp_path / ad).write_bytes(b"x")
        gorev_ekle(db_yolu, tmp_path / ad)

    liste = gorevleri_listele(db_yolu)
    assert [g.dosya_adi for g in liste] == ["iki.mp4", "bir.mp4"]
    assert gorevleri_listele(db_yolu, durum=Durum.YAYINLANDI) == []


def test_gorev_sil(db_yolu: Path, tmp_path: Path) -> None:
    video = tmp_path / "tanitim.mp4"
    video.write_bytes(b"x")
    gorev_id = gorev_ekle(db_yolu, video)

    assert gorev_sil(db_yolu, gorev_id) is True
    assert gorev_getir(db_yolu, gorev_id) is None
