# -*- coding: ascii -*-
"""Meta hata kodlarinin KaliciHata / GeciciHata olarak siniflandirilmasi.

Bu ayrim sistemin en kritik karari: yanlis siniflandirma ya 24 saatlik
kapsayici kotasini kor tekrarlarla yakar, ya da duzeltilebilir bir dosyayi
gereksiz yere hata/ klasorune atar.
"""

from __future__ import annotations

import pytest

from src.modeller import (
    GECICI_ALT_KODLAR,
    KALICI_ALT_KODLAR,
    KOTA_ALT_KODU,
    KOTA_KODU,
    GeciciHata,
    KaliciHata,
    KotaDoluHatasi,
    MedyaDogrulamaHatasi,
    TokenGecersizHatasi,
    YayinHatasi,
    hata_sinifla,
)


# ---------------------------------------------------------------------------
# Kalici hatalar - tekrar denemek ANLAMSIZ
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alt_kod,aciklama",
    [
        (2207026, "video formati desteklenmiyor"),
        (2207005, "gorsel formati desteklenmiyor"),
        (2207009, "en-boy orani gecersiz"),
        (2207010, "caption cok uzun"),
        (2207040, "20'den fazla etiket"),
        (2207057, "thumb_offset sure disinda"),
        (2207004, "gorsel 8 MB'dan buyuk"),
        (2207023, "bilinmeyen media_type"),
        (2207050, "hesap kisitli"),
        (2207051, "spam korumasi"),
    ],
)
def test_kalici_alt_kodlar(alt_kod: int, aciklama: str) -> None:
    hata = hata_sinifla(None, alt_kod, aciklama)
    assert isinstance(hata, KaliciHata), f"{alt_kod} kalici olmali"
    assert hata.kalici is True
    assert hata.alt_kod == alt_kod


def test_kalici_alt_kod_listesi_ile_tutarli() -> None:
    for alt_kod in KALICI_ALT_KODLAR:
        assert hata_sinifla(None, alt_kod, "test").kalici is True


# ---------------------------------------------------------------------------
# Gecici hatalar - yeni kapsayici ile tekrar denenebilir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alt_kod,aciklama",
    [
        (2207003, "medya indirme cok uzun surdu"),
        (2207001, "Instagram sunucu hatasi"),
        (2207032, "create media fail"),
        (2207053, "bilinmeyen yukleme hatasi"),
        (2207008, "media builder suresi doldu"),
        (2207020, "medya suresi doldu"),
        (2207027, "medya yayina hazir degil"),
        (2207052, "medya URI'den cekilemedi"),
    ],
)
def test_gecici_alt_kodlar(alt_kod: int, aciklama: str) -> None:
    hata = hata_sinifla(None, alt_kod, aciklama)
    assert isinstance(hata, GeciciHata), f"{alt_kod} gecici olmali"
    assert hata.kalici is False


def test_gecici_alt_kod_listesi_ile_tutarli() -> None:
    for alt_kod in GECICI_ALT_KODLAR:
        assert hata_sinifla(None, alt_kod, "test").kalici is False


@pytest.mark.parametrize("kod", [1, 2])
def test_gecici_sunucu_kodlari(kod: int) -> None:
    assert hata_sinifla(kod, None, "gecici sunucu hatasi").kalici is False


@pytest.mark.parametrize("kod", [4, 17, 32, 341, 368, 613])
def test_cagri_limiti_gecicidir(kod: int) -> None:
    hata = hata_sinifla(kod, None, "rate limit")
    assert isinstance(hata, GeciciHata)
    assert "limit" in str(hata).lower()


# ---------------------------------------------------------------------------
# Token hatalari - kuyruk durdurulmali
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kod", [190, 102, 467])
def test_token_kodlari(kod: int) -> None:
    hata = hata_sinifla(kod, None, "Error validating access token")
    assert isinstance(hata, TokenGecersizHatasi)
    assert isinstance(hata, KaliciHata), "Token hatasi kalicidir"
    assert "config/.env" in str(hata)


@pytest.mark.parametrize("alt_kod", [458, 459, 460, 463, 464, 467, 492])
def test_token_alt_kodlari(alt_kod: int) -> None:
    assert isinstance(hata_sinifla(None, alt_kod, "token"), TokenGecersizHatasi)


def test_token_hatasi_alt_kodlardan_once_degerlendirilir() -> None:
    """Hem token alt kodu hem kalici alt kod varsa token onceliklidir."""
    hata = hata_sinifla(190, 2207026, "token gecersiz")
    assert isinstance(hata, TokenGecersizHatasi)


# ---------------------------------------------------------------------------
# Izin hatalari
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kod", [10, 200, 299, 9053])
def test_izin_kodlari_kalicidir(kod: int) -> None:
    hata = hata_sinifla(kod, None, "izin yok")
    assert isinstance(hata, KaliciHata)
    assert "instagram_content_publish" in str(hata)
    assert "pages_read_engagement" in str(hata)


def test_sayfa_yayin_yetkilendirmesi_mesajdan_tespit_edilir() -> None:
    hata = hata_sinifla(
        None, None, "The page publishing authorization is required."
    )
    assert isinstance(hata, KaliciHata)
    assert "PPA" in str(hata)


# ---------------------------------------------------------------------------
# Kota - hata DEGIL, ERTELEME
# ---------------------------------------------------------------------------


def test_kota_dolu_gecici_kabul_edilir() -> None:
    hata = hata_sinifla(KOTA_KODU, KOTA_ALT_KODU, "quota exceeded")
    assert isinstance(hata, KotaDoluHatasi)
    assert isinstance(hata, GeciciHata), "Kota dolu kalici hata DEGILDIR"
    assert hata.kalici is False


def test_kota_kodu_tek_basina_yetmez() -> None:
    """Sadece kod 9 (alt kod olmadan) kota hatasi sayilmaz."""
    assert not isinstance(hata_sinifla(KOTA_KODU, None, "baska bir sey"), KotaDoluHatasi)


# ---------------------------------------------------------------------------
# Bilinmeyen hatalar
# ---------------------------------------------------------------------------


def test_bilinmeyen_hata_gecici_kabul_edilir() -> None:
    """Bilinmeyen hata gecicidir; deneme sayisi siniri sonsuz donguyu engeller."""
    hata = hata_sinifla(999999, 888888, "hic gorulmemis bir hata")
    assert isinstance(hata, GeciciHata)
    assert hata.kalici is False


def test_kodsuz_hata_gecici_kabul_edilir() -> None:
    assert hata_sinifla(None, None, "ayrinti yok").kalici is False


def test_metin_kodlar_sayiya_cevrilir() -> None:
    hata = hata_sinifla("190", "463", "metin olarak gelen kodlar")
    assert isinstance(hata, TokenGecersizHatasi)
    assert hata.kod == 190
    assert hata.alt_kod == 463


# ---------------------------------------------------------------------------
# Istisna siniflarinin kendisi
# ---------------------------------------------------------------------------


def test_medya_dogrulama_hatasi_kalicidir() -> None:
    hata = MedyaDogrulamaHatasi("dosya cok buyuk")
    assert hata.kalici is True
    assert isinstance(hata, KaliciHata)
    assert isinstance(hata, YayinHatasi)


def test_izleme_numarasi_ozette_gorunur() -> None:
    hata = hata_sinifla(None, 2207026, "format hatasi", iz_no="AbCdEf123")
    metin = str(hata)
    assert "alt_kod=2207026" in metin
    assert "iz=AbCdEf123" in metin


def test_kota_hatasi_yeniden_deneme_zamani_tasiyabilir() -> None:
    from datetime import timedelta

    from src.modeller import simdi

    an = simdi() + timedelta(hours=1)
    hata = KotaDoluHatasi("kota dolu", yeniden_deneme_zamani=an)
    assert hata.yeniden_deneme_zamani == an
