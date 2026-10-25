# -*- coding: ascii -*-
"""Testler icin ortak duzenek (fixture) tanimlari.

Butun testler gecici bir proje klasorunde calisir; gercek kuyruk.db ve gercek
gelen/ klasorleri ASLA kullanilmaz. Meta'ya hicbir istek gitmez (httpx sahte
istemci ile degistirilir).

Turkce karakter kullanilmaz (ASCII).
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

# Proje kokunu sys.path'e ekle ('src.xxx' ice aktarimlari icin)
PROJE_KOKU = Path(__file__).resolve().parent.parent
if str(PROJE_KOKU) not in sys.path:
    sys.path.insert(0, str(PROJE_KOKU))

from src.ayarlar import Ayarlar, ayarlari_yukle, onbellegi_temizle  # noqa: E402
from src.modeller import MedyaTipi  # noqa: E402

#: Ortam degiskenlerinden sizabilecek gercek sirlar: testlerde temizlenir.
_ORTAM_ANAHTARLARI = (
    "FB_SAYFA_TOKEN",
    "IG_HESAP_ID",
    "FB_UYGULAMA_ID",
    "FB_UYGULAMA_SIRRI",
    "API_SURUMU",
)

ORNEK_YAML = """
klasorler:
  gelen_reels: gelen/reels
  gelen_story: gelen/story
  yayinlandi: yayinlandi
  hata: hata
  veritabani: kuyruk.db

zamanlama:
  zaman_dilimi: "Europe/Istanbul"
  yayin_slotlari:
    - "12:00"
    - "19:00"
  tarama_araligi_sn: 10
  slot_tolerans_dk: 30

yayin:
  gunluk_limit: 5
  azami_deneme: 3
  durum_sorgu_araligi_sn: 5
  durum_azami_bekleme_sn: 60
  yukleme_zaman_asimi_sn: 60
  istek_zaman_asimi_sn: 10
  baglanti_zaman_asimi_sn: 5
  akisa_paylas: true
  varsayilan_aciklama: ""
  kota_sorgula: true
  hatali_dosyayi_tasi: true

dosya:
  kararlilik_bekleme_sn: 0.5
  kararlilik_tekrar: 1
  video_uzantilari: [".mp4", ".mov"]
  gorsel_uzantilari: [".jpg", ".jpeg", ".png", ".webp"]
  aciklama_uzantisi: ".txt"
  yoksayilan_onekler: [".", "~", "_"]

kayit:
  seviye: "DEBUG"
  dosya: hata/otomasyon.log
  azami_bayt: 65536
  yedek_sayisi: 1
"""


@pytest.fixture(autouse=True)
def gunlukcuyu_temizle() -> Iterator[None]:
    """Her testten sonra 'insta' gunlukcusunun yoneticilerini kaldirir.

    Aksi halde bir testte acilan dosya yoneticisi (gecici klasordeki
    hata/otomasyon.log) sonraki testlerde silinmis bir yola yazmaya calisir.
    """
    yield
    gunlukcu = logging.getLogger("insta")
    for yonetici in list(gunlukcu.handlers):
        gunlukcu.removeHandler(yonetici)
        try:
            yonetici.close()
        except OSError:  # pragma: no cover - savunmaci
            pass


@pytest.fixture(autouse=True)
def anahtar_dosyasini_izole_et(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Testler kullanicinin gercek config/anahtarlar.json dosyasini ve ortamdaki API anahtarlarini
    ASLA okumaz/yazmaz: her test bos, gecici bir anahtar dosyasiyla baslar."""
    monkeypatch.setenv("IC_ANAHTAR_DOSYASI", str(tmp_path_factory.mktemp("anahtar") / "anahtarlar.json"))
    for ad in ("PEXELS_API_KEY", "PIXABAY_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(ad, raising=False)


@pytest.fixture(autouse=True)
def sogutucuyu_sifirla(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kaynak sogutucusu (429 sonrasi bekleme + en kisa aralik) surec geneli durum tutar: testler birbirini etkilemesin
    ve gercekten uyumasin."""
    from src.icerik import acik_kaynaklar

    acik_kaynaklar.SOGUTUCU._son.clear()
    acik_kaynaklar.SOGUTUCU._kapali.clear()
    monkeypatch.setattr(acik_kaynaklar.SOGUTUCU, "_aralik", {})


@pytest.fixture
def proje_klasoru(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Gecici, eksiksiz bir proje klasoru olusturur."""
    for anahtar in _ORTAM_ANAHTARLARI:
        monkeypatch.delenv(anahtar, raising=False)

    (tmp_path / "config").mkdir()
    (tmp_path / "gelen" / "reels").mkdir(parents=True)
    (tmp_path / "gelen" / "story").mkdir(parents=True)
    (tmp_path / "yayinlandi").mkdir()
    (tmp_path / "hata").mkdir()
    (tmp_path / "config" / "ayarlar.yaml").write_text(ORNEK_YAML, encoding="utf-8")

    onbellegi_temizle()
    yield tmp_path
    onbellegi_temizle()


@pytest.fixture
def ayar(proje_klasoru: Path) -> Ayarlar:
    """Token'i BOS olan ayar nesnesi (yayin yapamaz, kuyruk calisir)."""
    return ayarlari_yukle(proje_klasoru, onbellek=False)


@pytest.fixture
def ayar_tokenli(proje_klasoru: Path, monkeypatch: pytest.MonkeyPatch) -> Ayarlar:
    """Sahte (gercek olmayan) token ve hesap kimligi iceren ayar nesnesi."""
    monkeypatch.setenv("FB_SAYFA_TOKEN", "TEST_SAHTE_ANAHTAR_0123456789")
    monkeypatch.setenv("IG_HESAP_ID", "17841400000000001")
    monkeypatch.setenv("API_SURUMU", "v23.0")
    onbellegi_temizle()
    return ayarlari_yukle(proje_klasoru, onbellek=False)


@pytest.fixture
def db_yolu(ayar: Ayarlar) -> Path:
    """Hazirlanmis (semasi olusturulmus) gecici veritabani yolu."""
    from src.db import veritabanini_hazirla

    veritabanini_hazirla(ayar.db_yolu)
    return Path(ayar.db_yolu)


# ---------------------------------------------------------------------------
# Yardimcilar
# ---------------------------------------------------------------------------


def ayari_degistir(ayar: Ayarlar, **bolumler: Any) -> Ayarlar:
    """Ayar nesnesinin bir bolumunu degistirilmis kopyasini uretir.

    Ornek:
        ayari_degistir(ayar, yayin={"gunluk_limit": 1})
    """
    yenileme: dict[str, Any] = {}
    for bolum_adi, degisiklik in bolumler.items():
        bolum = getattr(ayar, bolum_adi)
        yenileme[bolum_adi] = bolum.model_copy(update=dict(degisiklik))
    return dataclasses.replace(ayar, **yenileme)


def video_dosyasi_olustur(
    klasor: Path, ad: str, bayt: int = 4096
) -> Path:
    """Test icin sahte bir video dosyasi uretir (icerik onemsizdir)."""
    klasor.mkdir(parents=True, exist_ok=True)
    yol = klasor / ad
    # Basit bir MP4 kutu yapisi: moov once gelsin (moov_basta_mi True dondursun)
    govde = bytearray()
    govde += (8 + 16).to_bytes(4, "big") + b"moov" + b"\x00" * 16
    kalan = max(0, bayt - len(govde) - 8)
    govde += (8 + kalan).to_bytes(4, "big") + b"mdat" + b"\x00" * kalan
    yol.write_bytes(bytes(govde))
    return yol


def gorev_ekle(
    db_yolu: Path,
    dosya_yolu: Path,
    medya_tipi: MedyaTipi = MedyaTipi.REELS,
    **ekstra: Any,
) -> int:
    """Kuyruga gorev ekler ve id dondurur (testlerde kisayol)."""
    from src.db import kuyruga_ekle

    gorev_id = kuyruga_ekle(db_yolu, dosya_yolu, medya_tipi, **ekstra)
    assert gorev_id is not None, "Gorev kuyruga eklenemedi"
    return gorev_id
