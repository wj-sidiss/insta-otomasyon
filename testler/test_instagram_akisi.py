# -*- coding: ascii -*-
"""instagram.py yayin akisinin SAHTE (mock) httpx ile ucbastan uca testi.

Test edilen akis:
    konteyner_olustur -> dosya_yukle -> durum_bekle(IN_PROGRESS, FINISHED)
    -> yayinla -> gonderi bilgisi

Ag'a HICBIR istek gitmez; httpx istemcisi disaridan enjekte edilen sahte bir
nesne ile degistirilir. Boylece istek basliklari, govdesi ve cagri SIRASI
dogrulanabilir.

Ozellikle dogrulanan kritik kurallar:
  - rupload tarafinda Authorization semasi "OAuth" (Bearer DEGIL)
  - 'offset' ve 'file_size' basliklari kucuk harf; file_size TOPLAM boyut
  - govde ham binary (multipart degil)
  - upload_type=resumable ve video_url GONDERILMEZ
  - HTTP 200 yayin demek degildir; status_code FINISHED beklenir
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from conftest import video_dosyasi_olustur
from src.instagram import (
    DURUM_HAZIR,
    InstagramIstemcisi,
    KonteynerSuresiDoldu,
)
from src.modeller import (
    GeciciHata,
    KaliciHata,
    KotaDoluHatasi,
    MedyaDogrulamaHatasi,
    MedyaTipi,
    TokenGecersizHatasi,
)

TOKEN = "TEST_SAHTE_ANAHTAR_0123456789"
IG_ID = "17841400000000001"
SURUM = "v23.0"
GRAFIK = f"https://graph.facebook.com/{SURUM}"
KONTEYNER_ID = "17999888877766655"
MEDYA_ID = "17888777766655544"
YUKLEME_ADRESI = f"https://rupload.facebook.com/ig-api-upload/{SURUM}/{KONTEYNER_ID}"


# ---------------------------------------------------------------------------
# Sahte httpx istemcisi
# ---------------------------------------------------------------------------


class SahteCagri:
    """Kaydedilen tek bir HTTP cagrisi."""

    def __init__(
        self,
        yontem: str,
        adres: str,
        *,
        parametreler: dict[str, Any] | None = None,
        veri: dict[str, Any] | None = None,
        basliklar: dict[str, str] | None = None,
        icerik: bytes | None = None,
    ) -> None:
        self.yontem = yontem.upper()
        self.adres = adres
        self.parametreler = parametreler or {}
        self.veri = veri or {}
        self.basliklar = basliklar or {}
        self.icerik = icerik

    def __repr__(self) -> str:  # hata mesajlarinda okunakli olsun
        return f"<{self.yontem} {self.adres}>"


class SahteHttpIstemci:
    """httpx.Client yerine gecen sahte istemci.

    yanitlar: (YONTEM, adres) -> sirayla dondurulecek httpx.Response listesi.
    Sira tukenirse SON yanit tekrar tekrar dondurulur.
    """

    def __init__(self, yanitlar: dict[tuple[str, str], list[httpx.Response]]) -> None:
        self.yanitlar = yanitlar
        self.cagrilar: list[SahteCagri] = []
        self.kapandi = False

    # -- ic yardimci ---------------------------------------------------------

    def _yanit_al(self, yontem: str, adres: str) -> httpx.Response:
        sira = self.yanitlar.get((yontem.upper(), adres))
        if sira is None:
            raise AssertionError(
                f"Beklenmeyen istek: {yontem.upper()} {adres}\n"
                f"Tanimli adresler: {sorted(a for _, a in self.yanitlar)}"
            )
        if len(sira) > 1:
            return sira.pop(0)
        return sira[0]

    # -- httpx.Client arayuzu ------------------------------------------------

    def request(
        self,
        yontem: str,
        adres: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: Any = None,
        **_ek: Any,
    ) -> httpx.Response:
        self.cagrilar.append(
            SahteCagri(yontem, adres, parametreler=params, veri=data)
        )
        return self._yanit_al(yontem, adres)

    def post(
        self,
        adres: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        timeout: Any = None,
        **_ek: Any,
    ) -> httpx.Response:
        self.cagrilar.append(
            SahteCagri("POST", adres, basliklar=headers, icerik=content)
        )
        return self._yanit_al("POST", adres)

    def close(self) -> None:
        self.kapandi = True

    # -- test yardimcilari ---------------------------------------------------

    def cagri_sirasi(self) -> list[str]:
        return [f"{c.yontem} {c.adres}" for c in self.cagrilar]

    def ilk(self, yontem: str, adres: str) -> SahteCagri:
        for cagri in self.cagrilar:
            if cagri.yontem == yontem.upper() and cagri.adres == adres:
                return cagri
        raise AssertionError(f"Boyle bir cagri yapilmadi: {yontem} {adres}")


def _yanit(govde: dict[str, Any], kod: int = 200) -> httpx.Response:
    return httpx.Response(kod, json=govde)


def _hata_yaniti(
    kod: int, mesaj: str, *, hata_kodu: int | None = None, alt_kod: int | None = None
) -> httpx.Response:
    govde: dict[str, Any] = {
        "error": {
            "message": mesaj,
            "type": "OAuthException",
            "fbtrace_id": "AbCdEf123",
        }
    }
    if hata_kodu is not None:
        govde["error"]["code"] = hata_kodu
    if alt_kod is not None:
        govde["error"]["error_subcode"] = alt_kod
    return httpx.Response(kod, json=govde)


@pytest.fixture
def uykusuz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Geri cekilme ve durum sorgu beklemelerini sifirlar (test hizli kalsin)."""
    import time as _zaman

    monkeypatch.setattr(_zaman, "sleep", lambda _sure: None)
    yield


def _tam_akis_yanitlari(
    durum_sirasi: list[dict[str, Any]] | None = None,
) -> dict[tuple[str, str], list[httpx.Response]]:
    """Basarili bir uctan uca akisin butun yanitlarini kurar."""
    durumlar = durum_sirasi or [
        {"status_code": "IN_PROGRESS", "status": "Yukleniyor"},
        {"status_code": "FINISHED", "status": "Finished"},
    ]
    return {
        ("GET", f"{GRAFIK}/{IG_ID}/content_publishing_limit"): [
            _yanit({"data": [{"config": {"quota_total": 50}, "quota_usage": 3}]})
        ],
        ("POST", f"{GRAFIK}/{IG_ID}/media"): [
            _yanit({"id": KONTEYNER_ID, "uri": YUKLEME_ADRESI})
        ],
        ("POST", YUKLEME_ADRESI): [_yanit({"success": True})],
        ("GET", f"{GRAFIK}/{KONTEYNER_ID}"): [_yanit(d) for d in durumlar],
        ("POST", f"{GRAFIK}/{IG_ID}/media_publish"): [_yanit({"id": MEDYA_ID})],
        ("GET", f"{GRAFIK}/{MEDYA_ID}"): [
            _yanit(
                {
                    "id": MEDYA_ID,
                    "permalink": "https://www.instagram.com/reel/AbCdEf/",
                    "media_product_type": "REELS",
                    "media_type": "VIDEO",
                    "timestamp": "2026-09-25T18:30:00+0000",
                }
            )
        ],
    }


def _istemci_uret(
    sahte: SahteHttpIstemci, **ek: Any
) -> InstagramIstemcisi:
    varsayilan: dict[str, Any] = {
        "durum_sorgu_araligi_sn": 0.01,
        "durum_azami_bekleme_sn": 5.0,
        "azami_deneme": 3,
        "istemci": sahte,
    }
    varsayilan.update(ek)
    return InstagramIstemcisi(TOKEN, IG_ID, SURUM, **varsayilan)


# ---------------------------------------------------------------------------
# 1) Uctan uca basarili akis
# ---------------------------------------------------------------------------


def test_ucbastan_uca_yayin_akisi(tmp_path: Path, uykusuz: None) -> None:
    video = video_dosyasi_olustur(tmp_path, "tanitim.mp4", bayt=2048)
    toplam_bayt = video.stat().st_size
    sahte = SahteHttpIstemci(_tam_akis_yanitlari())
    istemci = _istemci_uret(sahte)

    sonuc = istemci.videoyu_yayinla(video, MedyaTipi.REELS, "Merhaba #tanitim")

    # --- sonuc ---
    assert sonuc.medya_id == MEDYA_ID
    assert sonuc.permalink == "https://www.instagram.com/reel/AbCdEf/"
    assert sonuc.urun_tipi == "REELS"

    # --- cagri sirasi: kota -> konteyner -> yukleme -> durum x2 -> yayin -> bilgi ---
    assert sahte.cagri_sirasi() == [
        f"GET {GRAFIK}/{IG_ID}/content_publishing_limit",
        f"POST {GRAFIK}/{IG_ID}/media",
        f"POST {YUKLEME_ADRESI}",
        f"GET {GRAFIK}/{KONTEYNER_ID}",
        f"GET {GRAFIK}/{KONTEYNER_ID}",
        f"POST {GRAFIK}/{IG_ID}/media_publish",
        f"GET {GRAFIK}/{MEDYA_ID}",
    ]

    # --- 1) konteyner istegi ---
    konteyner = sahte.ilk("POST", f"{GRAFIK}/{IG_ID}/media")
    assert konteyner.veri["media_type"] == "REELS"
    assert konteyner.veri["upload_type"] == "resumable"
    assert konteyner.veri["caption"] == "Merhaba #tanitim"
    assert konteyner.veri["share_to_feed"] == "true"
    assert konteyner.veri["access_token"] == TOKEN
    # Yerel dosya yukleniyor: video_url ASLA gonderilmemeli
    assert "video_url" not in konteyner.veri
    assert "image_url" not in konteyner.veri

    # --- 2) yukleme istegi ---
    yukleme = sahte.ilk("POST", YUKLEME_ADRESI)
    assert yukleme.basliklar["Authorization"] == f"OAuth {TOKEN}"
    assert not yukleme.basliklar["Authorization"].startswith("Bearer")
    assert yukleme.basliklar["offset"] == "0"
    assert yukleme.basliklar["file_size"] == str(toplam_bayt)
    assert yukleme.basliklar["Content-Type"] == "application/octet-stream"
    assert yukleme.icerik == video.read_bytes()   # ham binary, multipart degil

    # --- 3) durum sorgusu ---
    durum = sahte.ilk("GET", f"{GRAFIK}/{KONTEYNER_ID}")
    assert durum.parametreler["fields"] == "status_code,status"

    # --- 4) yayin istegi ---
    yayin = sahte.ilk("POST", f"{GRAFIK}/{IG_ID}/media_publish")
    assert yayin.veri == {"creation_id": KONTEYNER_ID, "access_token": TOKEN}


def test_kapsayici_yanitindaki_uri_kullanilir(tmp_path: Path, uykusuz: None) -> None:
    """Yanitta 'uri' varsa adres elle kurulmaz, o kullanilir."""
    video = video_dosyasi_olustur(tmp_path, "tanitim.mp4")
    ozel_adres = "https://rupload.facebook.com/ig-api-upload/v23.0/OZEL-ADRES"
    yanitlar = _tam_akis_yanitlari()
    yanitlar[("POST", f"{GRAFIK}/{IG_ID}/media")] = [
        _yanit({"id": KONTEYNER_ID, "uri": ozel_adres})
    ]
    yanitlar[("POST", ozel_adres)] = [_yanit({"success": True})]
    sahte = SahteHttpIstemci(yanitlar)
    istemci = _istemci_uret(sahte)

    konteyner = istemci.konteyner_olustur(MedyaTipi.REELS, "metin", video)
    assert konteyner.yukleme_adresi == ozel_adres
    istemci.dosya_yukle(konteyner.id, video)
    assert f"POST {ozel_adres}" in sahte.cagri_sirasi()


def test_story_konteynerinde_caption_gonderilmez(tmp_path: Path, uykusuz: None) -> None:
    video = video_dosyasi_olustur(tmp_path, "hikaye.mp4")
    sahte = SahteHttpIstemci(_tam_akis_yanitlari())
    istemci = _istemci_uret(sahte)

    istemci.konteyner_olustur(MedyaTipi.STORIES, "bu metin gonderilmemeli", video)

    veri = sahte.ilk("POST", f"{GRAFIK}/{IG_ID}/media").veri
    assert veri["media_type"] == "STORIES"
    assert veri["upload_type"] == "resumable"
    assert "caption" not in veri
    assert "share_to_feed" not in veri


# ---------------------------------------------------------------------------
# 2) Durum bekleme
# ---------------------------------------------------------------------------


def test_durum_bekle_isleniyorken_bekler(tmp_path: Path, uykusuz: None) -> None:
    sahte = SahteHttpIstemci(
        _tam_akis_yanitlari(
            [
                {"status_code": "IN_PROGRESS"},
                {"status_code": "IN_PROGRESS"},
                {"status_code": "FINISHED"},
            ]
        )
    )
    istemci = _istemci_uret(sahte)

    assert istemci.durum_bekle(KONTEYNER_ID) == DURUM_HAZIR
    assert sahte.cagri_sirasi().count(f"GET {GRAFIK}/{KONTEYNER_ID}") == 3


def test_durum_hata_kodu_kalici_hataya_cevrilir(uykusuz: None) -> None:
    sahte = SahteHttpIstemci(
        _tam_akis_yanitlari(
            [{"status_code": "ERROR", "status": "Error: 2207026 format not supported"}]
        )
    )
    istemci = _istemci_uret(sahte)

    with pytest.raises(KaliciHata) as bilgi:
        istemci.durum_bekle(KONTEYNER_ID)
    assert bilgi.value.alt_kod == 2207026
    assert "moov" in str(bilgi.value)


def test_durum_hata_kodu_gecici_hataya_cevrilir(uykusuz: None) -> None:
    sahte = SahteHttpIstemci(
        _tam_akis_yanitlari(
            [{"status_code": "ERROR", "status": "Error: 2207001 server error"}]
        )
    )
    istemci = _istemci_uret(sahte)

    with pytest.raises(GeciciHata) as bilgi:
        istemci.durum_bekle(KONTEYNER_ID)
    assert bilgi.value.kalici is False


def test_suresi_dolan_kapsayici(uykusuz: None) -> None:
    sahte = SahteHttpIstemci(_tam_akis_yanitlari([{"status_code": "EXPIRED"}]))
    istemci = _istemci_uret(sahte)

    with pytest.raises(KonteynerSuresiDoldu):
        istemci.durum_bekle(KONTEYNER_ID)


def test_isleme_zaman_asimi_gecici_hatadir(uykusuz: None) -> None:
    sahte = SahteHttpIstemci(_tam_akis_yanitlari([{"status_code": "IN_PROGRESS"}]))
    istemci = _istemci_uret(sahte, durum_azami_bekleme_sn=0.0)

    with pytest.raises(GeciciHata) as bilgi:
        istemci.durum_bekle(KONTEYNER_ID)
    assert "zaman asimi" in str(bilgi.value).lower()


def test_hazir_olmadan_yayin_denenmez(uykusuz: None) -> None:
    sahte = SahteHttpIstemci(_tam_akis_yanitlari([{"status_code": "IN_PROGRESS"}]))
    istemci = _istemci_uret(sahte)

    with pytest.raises(GeciciHata):
        istemci.yayinla_ayrintili(KONTEYNER_ID, durumu_kontrol_et=True)
    # media_publish cagrisi HIC yapilmamis olmali
    assert f"POST {GRAFIK}/{IG_ID}/media_publish" not in sahte.cagri_sirasi()


# ---------------------------------------------------------------------------
# 3) Yukleme: kaldigi yerden devam
# ---------------------------------------------------------------------------


def test_kesilen_yukleme_ofsetten_devam_eder(tmp_path: Path, uykusuz: None) -> None:
    video = video_dosyasi_olustur(tmp_path, "buyuk.mp4", bayt=4096)
    toplam = video.stat().st_size
    aktarilan = 1024

    yanitlar = _tam_akis_yanitlari()
    yanitlar[("POST", YUKLEME_ADRESI)] = [
        httpx.Response(500, text="gecici sunucu hatasi"),
        _yanit({"success": True}),
    ]
    yanitlar[("GET", f"{GRAFIK}/{KONTEYNER_ID}")] = [
        _yanit(
            {
                "id": KONTEYNER_ID,
                "status_code": "IN_PROGRESS",
                "video_status": {
                    "uploading_phase": {
                        "status": "in_progress",
                        "bytes_transferred": aktarilan,
                    }
                },
            }
        )
    ]
    sahte = SahteHttpIstemci(yanitlar)
    istemci = _istemci_uret(sahte)

    istemci.dosya_yukle(KONTEYNER_ID, video, yukleme_adresi=YUKLEME_ADRESI)

    yuklemeler = [c for c in sahte.cagrilar if c.adres == YUKLEME_ADRESI]
    assert len(yuklemeler) == 2
    # Ilk deneme: bastan
    assert yuklemeler[0].basliklar["offset"] == "0"
    assert yuklemeler[0].basliklar["file_size"] == str(toplam)
    # Ikinci deneme: kaldigi yerden; file_size HALA toplam boyut
    assert yuklemeler[1].basliklar["offset"] == str(aktarilan)
    assert yuklemeler[1].basliklar["file_size"] == str(toplam)
    assert yuklemeler[1].icerik == video.read_bytes()[aktarilan:]


def test_yukleme_kalici_hatada_tekrar_denenmez(tmp_path: Path, uykusuz: None) -> None:
    video = video_dosyasi_olustur(tmp_path, "bozuk.mp4")
    yanitlar = _tam_akis_yanitlari()
    yanitlar[("POST", YUKLEME_ADRESI)] = [
        httpx.Response(
            400,
            json={
                "debug_info": {
                    "retriable": False,
                    "type": "ProcessingFailedError",
                    "message": "Dosya kabul edilmedi",
                }
            },
        )
    ]
    sahte = SahteHttpIstemci(yanitlar)
    istemci = _istemci_uret(sahte)

    with pytest.raises(KaliciHata):
        istemci.dosya_yukle(KONTEYNER_ID, video, yukleme_adresi=YUKLEME_ADRESI)
    assert len([c for c in sahte.cagrilar if c.adres == YUKLEME_ADRESI]) == 1


def test_bos_dosya_yuklenmez(tmp_path: Path) -> None:
    bos = tmp_path / "bos.mp4"
    bos.write_bytes(b"")
    sahte = SahteHttpIstemci(_tam_akis_yanitlari())
    istemci = _istemci_uret(sahte)

    with pytest.raises(MedyaDogrulamaHatasi):
        istemci.dosya_yukle(KONTEYNER_ID, bos)
    assert sahte.cagrilar == []


# ---------------------------------------------------------------------------
# 4) Yerel dogrulama ve hata cevrimi
# ---------------------------------------------------------------------------


def test_desteklenmeyen_uzanti_konteyner_acmadan_elenir(tmp_path: Path) -> None:
    dosya = tmp_path / "belge.pdf"
    dosya.write_bytes(b"x" * 100)
    sahte = SahteHttpIstemci(_tam_akis_yanitlari())
    istemci = _istemci_uret(sahte)

    with pytest.raises(MedyaDogrulamaHatasi):
        istemci.konteyner_olustur(MedyaTipi.REELS, None, dosya)
    assert sahte.cagrilar == [], "Gecersiz dosya icin API'ye istek gitmemeli"


def test_cok_uzun_aciklama_reddedilir(tmp_path: Path) -> None:
    video = video_dosyasi_olustur(tmp_path, "tanitim.mp4")
    sahte = SahteHttpIstemci(_tam_akis_yanitlari())
    istemci = _istemci_uret(sahte)

    with pytest.raises(MedyaDogrulamaHatasi):
        istemci.konteyner_olustur(MedyaTipi.REELS, "a" * 2500, video)
    assert sahte.cagrilar == []


def test_token_hatasi_dogru_istisnaya_cevrilir(tmp_path: Path, uykusuz: None) -> None:
    video = video_dosyasi_olustur(tmp_path, "tanitim.mp4")
    yanitlar = _tam_akis_yanitlari()
    yanitlar[("POST", f"{GRAFIK}/{IG_ID}/media")] = [
        _hata_yaniti(400, "Error validating access token", hata_kodu=190)
    ]
    sahte = SahteHttpIstemci(yanitlar)
    istemci = _istemci_uret(sahte)

    with pytest.raises(TokenGecersizHatasi):
        istemci.konteyner_olustur(MedyaTipi.REELS, None, video)


def test_izin_hatasi_kalici_hatadir(tmp_path: Path, uykusuz: None) -> None:
    video = video_dosyasi_olustur(tmp_path, "tanitim.mp4")
    yanitlar = _tam_akis_yanitlari()
    yanitlar[("POST", f"{GRAFIK}/{IG_ID}/media")] = [
        _hata_yaniti(403, "Permission denied", hata_kodu=200)
    ]
    sahte = SahteHttpIstemci(yanitlar)
    istemci = _istemci_uret(sahte)

    with pytest.raises(KaliciHata) as bilgi:
        istemci.konteyner_olustur(MedyaTipi.REELS, None, video)
    assert "instagram_content_publish" in str(bilgi.value)


# ---------------------------------------------------------------------------
# 5) Yayin kotasi
# ---------------------------------------------------------------------------


def test_yayin_limiti_okunur(uykusuz: None) -> None:
    sahte = SahteHttpIstemci(_tam_akis_yanitlari())
    istemci = _istemci_uret(sahte)

    kullanilan, kota = istemci.yayin_limiti_kontrol()
    assert (kullanilan, kota) == (3, 50)
    assert istemci.kota_musait_mi() is True


def test_kota_dolu_ise_yayin_yapilmaz(tmp_path: Path, uykusuz: None) -> None:
    video = video_dosyasi_olustur(tmp_path, "tanitim.mp4")
    yanitlar = _tam_akis_yanitlari()
    yanitlar[("GET", f"{GRAFIK}/{IG_ID}/content_publishing_limit")] = [
        _yanit({"data": [{"config": {"quota_total": 25}, "quota_usage": 25}]})
    ]
    sahte = SahteHttpIstemci(yanitlar)
    istemci = _istemci_uret(sahte)

    assert istemci.kota_musait_mi() is False
    with pytest.raises(KotaDoluHatasi):
        istemci.videoyu_yayinla(video, MedyaTipi.REELS, "metin")
    # Kota dolu: konteyner bile acilmamali
    assert f"POST {GRAFIK}/{IG_ID}/media" not in sahte.cagri_sirasi()


def test_kota_yaniti_bos_ise_varsayilan_kullanilir(uykusuz: None) -> None:
    yanitlar = _tam_akis_yanitlari()
    yanitlar[("GET", f"{GRAFIK}/{IG_ID}/content_publishing_limit")] = [
        _yanit({"data": []})
    ]
    sahte = SahteHttpIstemci(yanitlar)
    istemci = _istemci_uret(sahte)

    kullanilan, kota = istemci.yayin_limiti_kontrol()
    assert kullanilan == 0
    assert kota > 0


# ---------------------------------------------------------------------------
# 6) Kurulum kontrolleri
# ---------------------------------------------------------------------------


def test_bos_token_reddedilir() -> None:
    with pytest.raises(ValueError) as bilgi:
        InstagramIstemcisi("", IG_ID, SURUM)
    assert "FB_SAYFA_TOKEN" in str(bilgi.value)


def test_bos_hesap_id_reddedilir() -> None:
    with pytest.raises(ValueError) as bilgi:
        InstagramIstemcisi(TOKEN, "", SURUM)
    assert "IG_HESAP_ID" in str(bilgi.value)


def test_repr_token_sizdirmaz() -> None:
    sahte = SahteHttpIstemci({})
    istemci = _istemci_uret(sahte)
    metin = repr(istemci)
    assert TOKEN not in metin
    assert "****" in metin


def test_disaridan_verilen_istemci_kapatilmaz() -> None:
    sahte = SahteHttpIstemci({})
    with _istemci_uret(sahte):
        pass
    assert sahte.kapandi is False, "Disaridan verilen istemci kapatilmamali"
