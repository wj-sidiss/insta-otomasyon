# -*- coding: ascii -*-
"""Giris noktasi (src/main.py) testleri.

Kapsanan durumlar:
  - --kuru-calistirma: hicbir API cagrisi yapmadan kuyrugu/zamanlamayi gosterir
  - eksik .env: ANLASILIR Turkce hata ve adim adim yonerge
  - bozuk ayarlar.yaml: acilista anlasilir hata
  - komut satiri secenekleri
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from conftest import gorev_ekle, video_dosyasi_olustur
from src import db, main
from src.ayarlar import Ayarlar
from src.modeller import MedyaTipi, simdi


# ---------------------------------------------------------------------------
# Komut satiri
# ---------------------------------------------------------------------------


def test_varsayilan_secenekler() -> None:
    secenekler = main.argumanlari_ayristir([])
    assert secenekler.kuru_calistirma is False
    assert secenekler.yoklama is False
    assert secenekler.seviye is None


def test_kuru_calistirma_bayragi() -> None:
    assert main.argumanlari_ayristir(["--kuru-calistirma"]).kuru_calistirma is True
    assert main.argumanlari_ayristir(["--dry-run"]).kuru_calistirma is True


def test_diger_bayraklar() -> None:
    secenekler = main.argumanlari_ayristir(["--yoklama", "--seviye", "DEBUG"])
    assert secenekler.yoklama is True
    assert secenekler.seviye == "DEBUG"


# ---------------------------------------------------------------------------
# Yapilandirma dogrulamasi
# ---------------------------------------------------------------------------


def test_eksik_env_normal_calismayi_durdurur(
    proje_klasoru: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kod = main.main(["--proje-koku", str(proje_klasoru)])
    assert kod == main.CIKIS_AYAR_HATASI

    cikti = capsys.readouterr().err
    assert "YAPILANDIRMA EKSIK" in cikti
    assert "FB_SAYFA_TOKEN" in cikti
    assert "IG_HESAP_ID" in cikti
    assert "BASLA.md" in cikti


def test_eksik_ayar_metni_adim_adim_anlatir(ayar: Ayarlar) -> None:
    metin = main.eksik_ayar_metni(ayar, ["FB_SAYFA_TOKEN", "IG_HESAP_ID"])

    # Token nereden alinir
    assert "developers.facebook.com" in metin
    assert "instagram_content_publish" in metin
    assert "/me/accounts" in metin
    assert "fb_exchange_token" in metin
    # Hesap kimligi nereden alinir
    assert "instagram_business_account" in metin
    # Nereye yazilacak
    assert "config/.env" in metin
    assert ".env.ornek" in metin
    # ASCII kalmali (Windows konsolu)
    metin.encode("ascii")


def test_env_varsa_kopyalama_komutu_gosterilmez(
    proje_klasoru: Path, ayar: Ayarlar
) -> None:
    (proje_klasoru / "config" / ".env").write_text("FB_SAYFA_TOKEN=\n", encoding="utf-8")
    metin = main.eksik_ayar_metni(ayar, ["FB_SAYFA_TOKEN"])
    assert "Copy-Item" not in metin
    assert "Duzenlenecek dosya" in metin


def test_tokenli_ayar_dogrulamayi_gecer(ayar_tokenli: Ayarlar) -> None:
    assert main.yapilandirmayi_dogrula(ayar_tokenli) is True


def test_kuru_calistirmada_eksik_ayar_engel_degil(
    ayar: Ayarlar, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main.yapilandirmayi_dogrula(ayar, kuru_calistirma=True) is True
    cikti = capsys.readouterr().out
    assert "UYARI" in cikti
    assert "API cagrisi yapilmiyor" in cikti


def test_bozuk_ayarlar_yaml_anlasilir_hata_verir(
    proje_klasoru: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (proje_klasoru / "config" / "ayarlar.yaml").write_text(
        "zamanlama:\n  yayin_slotlari: ['25:99']\n", encoding="utf-8"
    )
    kod = main.main(["--kuru-calistirma", "--proje-koku", str(proje_klasoru)])

    assert kod == main.CIKIS_AYAR_HATASI
    cikti = capsys.readouterr().err
    assert "AYAR DOSYASI HATASI" in cikti
    assert "yayin_slotlari" in cikti


def test_bilinmeyen_ayar_anahtari_yakalanir(
    proje_klasoru: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (proje_klasoru / "config" / "ayarlar.yaml").write_text(
        "yayin:\n  gunluk_limitt: 5\n", encoding="utf-8"
    )
    kod = main.main(["--kuru-calistirma", "--proje-koku", str(proje_klasoru)])

    assert kod == main.CIKIS_AYAR_HATASI
    assert "AYAR DOSYASI HATASI" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Kuru calistirma
# ---------------------------------------------------------------------------


def test_kuru_calistirma_rapor_basar(
    proje_klasoru: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    video_dosyasi_olustur(proje_klasoru / "gelen" / "reels", "2026-09-25_1830_yeni.mp4")

    kod = main.main(["--kuru-calistirma", "--proje-koku", str(proje_klasoru)])
    assert kod == main.CIKIS_TAMAM

    cikti = capsys.readouterr().out
    assert "KURU CALISTIRMA" in cikti
    assert "hicbir yayin yapilmayacak" in cikti
    assert "AYARLAR" in cikti
    assert "KUYRUK OZETI" in cikti
    assert "PLANLANAN YAYIN SIRASI" in cikti
    assert "Meta'ya hicbir istek gonderilmedi" in cikti
    # Klasordeki yeni dosya raporda gorunmeli
    assert "2026-09-25_1830_yeni.mp4" in cikti
    assert "kuyruga ALINACAK" in cikti
    assert "25.09.2026 18:30" in cikti


def test_kuru_calistirma_kuyruga_dosya_eklemez(proje_klasoru: Path) -> None:
    video_dosyasi_olustur(proje_klasoru / "gelen" / "reels", "yeni.mp4")

    main.main(["--kuru-calistirma", "--proje-koku", str(proje_klasoru)])

    db_yolu = proje_klasoru / "kuyruk.db"
    assert db_yolu.exists(), "Sema olusturulmali (yerel islem)"
    assert db.kuyruk_ozeti(db_yolu)["bekliyor"] == 0, (
        "Kuru calistirma kuyruga yazmamali"
    )


def test_kuru_calistirma_kuyruktaki_gorevleri_gosterir(
    ayar: Ayarlar, capsys: pytest.CaptureFixture[str]
) -> None:
    db.veritabanini_hazirla(ayar.db_yolu)
    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "bekleyen.mp4")
    gorev_id = gorev_ekle(
        Path(ayar.db_yolu),
        video,
        MedyaTipi.REELS,
        planlanan_zaman=simdi() + timedelta(hours=3),
    )

    kod = main.kuru_calistirma(ayar)
    assert kod == main.CIKIS_TAMAM

    cikti = capsys.readouterr().out
    assert f"[#{gorev_id}]" in cikti
    assert "bekleyen.mp4" in cikti
    assert "zaten kuyrukta" in cikti


def test_kuru_calistirma_slot_simulasyonu_ayri_slotlar_verir(
    ayar: Ayarlar, capsys: pytest.CaptureFixture[str]
) -> None:
    db.veritabanini_hazirla(ayar.db_yolu)
    for sira in range(3):
        video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, f"video{sira}.mp4")
        gorev_ekle(Path(ayar.db_yolu), video)

    main.kuru_calistirma(ayar)

    cikti = capsys.readouterr().out
    bolum = cikti.split("PLANLANAN YAYIN SIRASI")[1].split("Sonraki bos slot")[0]
    for sira in range(3):
        assert f"video{sira}.mp4" in bolum
    # Her gorev ayri bir slota dusmeli: en az 2 farkli saat gorunmeli
    saatler = {satir.split("-> ")[1].strip() for satir in bolum.splitlines() if "-> " in satir}
    assert len(saatler) == 3


def test_kuru_calistirma_limit_dolu_uyarisi(
    ayar: Ayarlar, capsys: pytest.CaptureFixture[str]
) -> None:
    from conftest import ayari_degistir

    dar = ayari_degistir(ayar, yayin={"gunluk_limit": 1})
    db.veritabanini_hazirla(dar.db_yolu)
    video = video_dosyasi_olustur(dar.klasorler.yayinlandi, "onceki.mp4")
    gorev_id = gorev_ekle(Path(dar.db_yolu), video)
    db.durum_guncelle(dar.db_yolu, gorev_id, main.Durum.YAYINLANDI, yayin_zamani=simdi())

    main.kuru_calistirma(dar)

    cikti = capsys.readouterr().out
    assert "Son 24 saatte yayinlanan : 1 / 1" in cikti
    assert "Gunluk limit dolu" in cikti


def test_kuru_calistirma_ciktisi_ascii(
    proje_klasoru: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main.main(["--kuru-calistirma", "--proje-koku", str(proje_klasoru)])
    capsys.readouterr().out.encode("ascii")   # Turkce karakter olmamali


def test_kuru_calistirma_bozuk_tarihli_dosyayi_uyarir(
    proje_klasoru: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    video_dosyasi_olustur(proje_klasoru / "gelen" / "reels", "2026-13-45_bozuk.mp4")

    main.main(["--kuru-calistirma", "--proje-koku", str(proje_klasoru)])

    cikti = capsys.readouterr().out
    assert "2026-13-45_bozuk.mp4" in cikti
    assert "UYARI" in cikti
    assert "ilk uygun slot" in cikti


def test_arsiv_ozeti_gitkeep_saymaz(ayar: Ayarlar) -> None:
    from src.arsiv import arsiv_ozeti

    ayar.klasorleri_hazirla()
    (Path(ayar.klasorler.hata) / ".gitkeep").write_text("", encoding="ascii")
    (Path(ayar.klasorler.yayinlandi) / ".gitkeep").write_text("", encoding="ascii")

    ozet = arsiv_ozeti(ayar)
    assert ozet["hatali_dosya"] == 0
    assert ozet["arsiv_toplam"] == 0
