# -*- coding: utf-8 -*-
"""Video siteleri katalogu, API anahtari deposu, stok video/gorsel arama-indirme ve panel uc noktalari."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.icerik import stok
from src.icerik.anahtarlar import SAGLAYICILAR, AnahtarDeposu, AnahtarHatasi, maskele
from src.icerik.paket import (
    MEDYA_DOSYA_DESENI, SISTEM, dogrula_yanit, medya_kaldir, medya_kaydet, paketi_oku,
)
from src.icerik.panel import uygulama_olustur
from src.icerik.saglayici import AnthropicApi, SaglayiciHatasi
from src.icerik.stok import Aday, Onbellek, StokHatasi
from src.icerik.video_siteleri import SITELER, UCRETSIZ_DEGERLERI, gecerli_mi, katalog

ANAHTAR = "SIRRI-ANAHTAR-0123456789abcdef"
PAKET_ID = "20261025-120000-test"


# ============================================================ video siteleri katalogu

def test_katalogdaki_her_kayit_gecerli_ve_adlar_benzersiz():
    for site in SITELER:
        assert gecerli_mi(site) == [], site["ad"]
    adlar = [s["ad"] for s in SITELER]
    assert len(adlar) == len(set(adlar))


@pytest.mark.parametrize("bozuk", [
    {"adres": "http://ornek.com/"},                      # https degil
    {"adres": "javascript:alert(1)"},
    {"kaynak": "ftp://ornek.com/x"},
    {"adres": "https://kullanici:sifre@ornek.com/"},     # gomulu kimlik bilgisi
    {"ucretsiz": "belki"},
    {"tur": "bilinmeyen"},
    {"ozet": ""},
])
def test_katalog_dogrulayici_bozuk_kaydi_yakalar(bozuk):
    kayit = {**SITELER[0], **bozuk}
    assert gecerli_mi(kayit) != []


def test_dogrulanamayan_kayit_nedenini_aciklar_ve_yok_olanlar_ayri_gruptadir():
    for s in SITELER:
        if s["ucretsiz"] == "bilinmiyor":
            assert s["belirtilmemis"], f"{s['ad']}: neden dogrulanamadigi yazilmali"
    k = katalog()
    gruplar = {g["anahtar"]: g["siteler"] for g in k["gruplar"]}
    assert {s["ucretsiz"] for s in gruplar["ucretsiz"]} <= {"var", "kisitli"}
    assert {s["ucretsiz"] for s in gruplar["yok"]} == {"yok"}
    assert {s["ucretsiz"] for s in gruplar["bilinmiyor"]} == {"bilinmiyor"}
    tum = [s["ad"] for g in k["gruplar"] for s in g["siteler"]]
    assert sorted(tum) == sorted(s["ad"] for s in SITELER)         # her site tam bir kez
    assert all(s["dogrulama"] == k["dogrulama_tarihi"] for g in k["gruplar"] for s in g["siteler"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", k["dogrulama_tarihi"])
    assert "sık değişir" in k["uyari"] and "size aittir" in k["uyari"]


def test_ucretsiz_degerleri_sabit_kume():
    assert set(UCRETSIZ_DEGERLERI) == {"var", "kisitli", "yok", "bilinmiyor"}


# ============================================================ API anahtari deposu

@pytest.fixture
def depo(tmp_path):
    return AnahtarDeposu(tmp_path / "anahtarlar.json", ortam={})


def test_kaydet_getir_ve_dosya_icerigi(depo):
    depo.kaydet("pexels", ANAHTAR)
    assert depo.getir("pexels") == ANAHTAR
    assert json.loads(depo.yol.read_text(encoding="utf-8")) == {"pexels": ANAHTAR}
    assert not list(depo.yol.parent.glob("*.tmp"))                  # atomik yazim: gecici dosya kalmaz


def test_durum_anahtari_asla_tam_gostermez(depo):
    depo.kaydet("pexels", ANAHTAR)
    metin = json.dumps(depo.durum(), ensure_ascii=False)
    assert ANAHTAR not in metin and ANAHTAR[:-4] not in metin
    pexels = next(d for d in depo.durum() if d["ad"] == "pexels")
    assert pexels["tanimli"] and pexels["kaynak"] == "dosya" and pexels["maske"] == maskele(ANAHTAR)
    assert maskele(ANAHTAR).endswith(ANAHTAR[-4:]) and maskele("kisa") == "••••"
    yok = next(d for d in depo.durum() if d["ad"] == "pixabay")
    assert not yok["tanimli"] and yok["maske"] is None and yok["kaynak"] is None


@pytest.mark.parametrize("deger", [
    "kisa", "", " " * 10, "bosluk iceren anahtar", "yeni\nsatir-12345", "turkce-ğüşiöç-12345",
    '"><script>alert(1)</script>', "a" * 301, None, 12345678,
])
def test_gecersiz_anahtar_reddedilir_ve_mesaj_degeri_icermez(depo, deger):
    with pytest.raises(AnahtarHatasi) as hata:
        depo.kaydet("pexels", deger)
    if isinstance(deger, str) and len(deger.strip()) > 3:
        assert deger.strip() not in str(hata.value)
    assert not depo.yol.exists()


def test_bilinmeyen_saglayici_reddedilir(depo):
    with pytest.raises(AnahtarHatasi):
        depo.kaydet("rastgele", ANAHTAR)
    with pytest.raises(AnahtarHatasi):
        depo.sil("rastgele")
    assert depo.getir("rastgele") is None


def test_ortam_degiskeni_dosyadan_once_gelir_ve_silinemez(tmp_path):
    depo = AnahtarDeposu(tmp_path / "k.json", ortam={"PEXELS_API_KEY": "ORTAMDAN-ANAHTAR-12345"})
    depo.kaydet("pexels", ANAHTAR)
    assert depo.getir("pexels") == "ORTAMDAN-ANAHTAR-12345"
    d = next(x for x in depo.durum() if x["ad"] == "pexels")
    assert d["kaynak"] == "ortam" and d["tanimli"]
    assert depo.sil("pexels") is True                    # dosyadaki silinir...
    assert depo.getir("pexels") == "ORTAMDAN-ANAHTAR-12345"   # ...ortam degiskeni kalir
    assert depo.sil("pexels") is False


def test_sil_ve_bozuk_dosya_cokmez(depo):
    depo.kaydet("pexels", ANAHTAR); depo.kaydet("pixabay", "digeranahtar-0001")
    assert depo.sil("pexels") is True and depo.getir("pexels") is None and depo.getir("pixabay") == "digeranahtar-0001"
    depo.yol.write_text("{bozuk json", encoding="utf-8")
    assert depo.getir("pixabay") is None and all(not d["tanimli"] for d in depo.durum())
    depo.yol.write_text(json.dumps({"pexels": ["liste"], "bilinmeyen": ANAHTAR, "pixabay": "kisa"}), encoding="utf-8")
    assert depo.getir("pexels") is None and depo.getir("pixabay") is None   # gecersiz girdiler yok sayilir
    depo.kaydet("pexels", ANAHTAR)                       # bozuk dosyanin ustune temiz yazabilir
    assert json.loads(depo.yol.read_text(encoding="utf-8")) == {"pexels": ANAHTAR}


def test_her_saglayicinin_maliyeti_ve_anahtar_adresi_https():
    for ad, bilgi in SAGLAYICILAR.items():
        assert bilgi["anahtar_al"].startswith("https://"), ad
        assert "ücretsiz" in bilgi["maliyet"].lower() or "ücretli" in bilgi["maliyet"].lower(), ad
        assert bilgi["ortam"].endswith("_API_KEY")


def test_anthropic_motoru_panelden_girilen_anahtari_kullanir_ve_hata_anahtari_sizdirmaz(monkeypatch):
    motor = AnthropicApi()
    assert motor.hazir_mi()[0] is False
    AnahtarDeposu().kaydet("anthropic_api", ANAHTAR)      # conftest: gecici dosya (IC_ANAHTAR_DOSYASI)
    assert motor.hazir_mi()[0] is True

    def patlat(*a, **k):
        raise httpx.ConnectError(f"baglanti kurulamadi: x-api-key={ANAHTAR}")
    monkeypatch.setattr(httpx, "post", patlat)
    with pytest.raises(SaglayiciHatasi) as hata:
        motor.uret("sistem", "istem")
    assert ANAHTAR not in str(hata.value) and hata.value.__cause__ is None and hata.value.__suppress_context__ is True


# ============================================================ stok: arama

def _istemci(isleyici):
    return httpx.Client(transport=httpx.MockTransport(isleyici))


def _pexels_video_yaniti():
    def dosya(i, kalite, w, h, tur="video/mp4"):
        return {"id": i, "quality": kalite, "file_type": tur, "width": w, "height": h,
                "link": f"https://videos.pexels.com/video-files/111/{w}x{h}.{'mp4' if tur == 'video/mp4' else 'webm'}"}
    return {"videos": [{
        "id": 111, "url": "https://www.pexels.com/video/ornek-111/", "image": "https://images.pexels.com/videos/111/t.jpeg",
        "duration": 12, "user": {"name": "Ada", "url": "https://www.pexels.com/@ada"},
        "video_files": [dosya(1, "sd", 540, 960), dosya(2, "hd", 1080, 1920), dosya(3, "hd", 2160, 3840),
                        dosya(4, "hd", 1080, 1920, "video/webm")],
    }, {"id": 112, "url": "https://www.pexels.com/video/yok/", "video_files": []}]}      # dosyasiz kayit atlanir


def test_pexels_video_istegi_ve_ayristirma():
    istekler = []

    def isleyici(istek):
        istekler.append(istek)
        return httpx.Response(200, json=_pexels_video_yaniti())

    adaylar = stok.ara("pexels", "video", "  woman   typing  ", ANAHTAR, adet=4, istemci=_istemci(isleyici), onbellek=Onbellek())
    i = istekler[0]
    assert i.url.host == "api.pexels.com" and i.url.path == "/videos/search"
    assert i.headers["Authorization"] == ANAHTAR and "key" not in i.url.params           # anahtar basliktadir, URL'de degil
    assert dict(i.url.params) == {"query": "woman typing", "orientation": "portrait", "per_page": "4"}
    assert len(adaylar) == 1                                                             # dosyasiz kayit atlandi
    a = adaylar[0]
    assert a.indirme.endswith("1080x1920.mp4")                                           # 1080'e en yakin, asmayan mp4
    assert (a.kaynak, a.tur, a.kredi, a.sure_sn, a.dikey) == ("pexels", "video", "Ada", 12, True)
    assert a.kredi_adresi == "https://www.pexels.com/@ada" and a.sayfa.startswith("https://www.pexels.com/video/")
    assert re.fullmatch(r"[0-9a-f]{16}", a.kimlik)


def test_pexels_gorsel_istegi_ve_ayristirma():
    def isleyici(istek):
        assert istek.url.path == "/v1/search" and istek.url.params["orientation"] == "portrait"
        return httpx.Response(200, json={"photos": [{
            "id": 7, "url": "https://www.pexels.com/photo/7/", "photographer": "Can", "photographer_url": "https://www.pexels.com/@can",
            "width": 4000, "height": 6000,
            "src": {"medium": "https://images.pexels.com/photos/7/m.jpg", "large2x": "https://images.pexels.com/photos/7/l2x.jpg"}}]})

    (a,) = stok.ara("pexels", "gorsel", "laptop", ANAHTAR, istemci=_istemci(isleyici), onbellek=Onbellek())
    assert a.indirme.endswith("l2x.jpg") and a.onizleme.endswith("m.jpg") and a.kredi == "Can" and a.dikey


def _pixabay_video_yaniti():
    def hit(i, w, h):
        return {"id": i, "pageURL": f"https://pixabay.com/videos/id-{i}/", "user": "Bob", "duration": 8,
                "videos": {"large": {"url": "", "width": 0, "height": 0, "size": 0, "thumbnail": ""},
                           "medium": {"url": f"https://cdn.pixabay.com/video/{i}.mp4", "width": w, "height": h, "size": 1,
                                      "thumbnail": f"https://cdn.pixabay.com/video/{i}.jpg"}}}
    return {"total": 2, "hits": [hit(9, 1920, 1080), hit(10, 1080, 1920)]}


def test_pixabay_video_istegi_dikeyler_once_siralanir():
    istekler = []

    def isleyici(istek):
        istekler.append(istek)
        return httpx.Response(200, json=_pixabay_video_yaniti())

    adaylar = stok.ara("pixabay", "video", "ocean", ANAHTAR, adet=6, istemci=_istemci(isleyici), onbellek=Onbellek())
    i = istekler[0]
    assert i.url.host == "pixabay.com" and i.url.path == "/api/videos/" and "authorization" not in i.headers
    assert i.url.params["key"] == ANAHTAR and i.url.params["safesearch"] == "true" and i.url.params["q"] == "ocean"
    assert int(i.url.params["per_page"]) == 18                       # yonelim filtresi yok: 3 kat isteyip suzeriz
    assert [a.dikey for a in adaylar] == [True, False]               # dikey olan once
    assert adaylar[0].indirme.endswith("/10.mp4") and adaylar[0].onizleme.endswith("/10.jpg") and adaylar[0].kredi == "Bob"


def test_pixabay_gorsel_istegi():
    def isleyici(istek):
        p = istek.url.params
        assert (p["orientation"], p["image_type"], p["safesearch"]) == ("vertical", "photo", "true")
        return httpx.Response(200, json={"hits": [{"id": 5, "pageURL": "https://pixabay.com/photos/x-5/", "user": "Eli",
                                                   "webformatURL": "https://pixabay.com/get/w.jpg",
                                                   "largeImageURL": "https://pixabay.com/get/l.jpg",
                                                   "imageWidth": 2000, "imageHeight": 3000}]})

    (a,) = stok.ara("pixabay", "gorsel", "tree", ANAHTAR, istemci=_istemci(isleyici), onbellek=Onbellek())
    assert a.indirme.endswith("l.jpg") and a.onizleme.endswith("w.jpg") and a.dikey


@pytest.mark.parametrize("kaynak,durum,govde,beklenen", [
    ("pexels", 401, "", "anahtarı geçersiz"),
    ("pexels", 403, "", "anahtarı geçersiz"),
    ("pixabay", 400, "[ERROR 400] Invalid or missing API key", "anahtarı geçersiz"),
    ("pexels", 429, "", "sınırı aşıldı"),
    ("pixabay", 429, "API rate limit exceeded", "sınırı aşıldı"),
    ("pexels", 503, "", "yanıt vermiyor"),
    ("pixabay", 400, "baska bir hata", "reddetti"),
    ("pexels", 404, "", "reddetti"),
])
def test_stok_hata_kodlari_anlasilir_mesaj_verir_ve_anahtari_sizdirmaz(kaynak, durum, govde, beklenen):
    ist = _istemci(lambda i: httpx.Response(durum, text=f"{govde} key={ANAHTAR}"))
    with pytest.raises(StokHatasi) as hata:
        stok.ara(kaynak, "gorsel", "konu", ANAHTAR, istemci=ist, onbellek=Onbellek())
    assert beklenen in str(hata.value) and ANAHTAR not in str(hata.value)


@pytest.mark.parametrize("kaynak", ["pexels", "pixabay"])
def test_ag_hatasi_anahtari_ve_url_yi_sizdirmaz(kaynak):
    def kapali(istek):
        raise httpx.ConnectError(f"baglanti hatasi: {istek.url}")
    with pytest.raises(StokHatasi) as hata:
        stok.ara(kaynak, "video", "konu", ANAHTAR, istemci=_istemci(kapali), onbellek=Onbellek())
    assert ANAHTAR not in str(hata.value) and "ulaşılamadı" in str(hata.value) and hata.value.__cause__ is None and hata.value.__suppress_context__ is True


@pytest.mark.parametrize("yanit", [httpx.Response(200, text="<html>"), httpx.Response(200, json=[1, 2]), httpx.Response(200, text="")])
def test_beklenmeyen_yanit_bicimi(yanit):
    with pytest.raises(StokHatasi, match="beklenmeyen"):
        stok.ara("pexels", "video", "konu", ANAHTAR, istemci=_istemci(lambda i: yanit), onbellek=Onbellek())


@pytest.mark.parametrize("kaynak,tur,sorgu,anahtar,beklenen", [
    ("pexels", "video", "a", ANAHTAR, "2-100"), ("pexels", "video", "x" * 101, ANAHTAR, "2-100"),
    ("pexels", "video", "   ", ANAHTAR, "2-100"), ("pexels", "video", "konu", "", "tanımlı değil"),
    ("flickr", "video", "konu", ANAHTAR, "Bilinmeyen"), ("pexels", "ses", "konu", ANAHTAR, "Bilinmeyen"),
])
def test_arama_girdileri_dogrulanir_ve_ag_cagrisi_yapilmaz(kaynak, tur, sorgu, anahtar, beklenen):
    def ulasmamali(istek):
        raise AssertionError("gecersiz girdiyle ag cagrisi yapilmamali")
    with pytest.raises(StokHatasi, match=beklenen):
        stok.ara(kaynak, tur, sorgu, anahtar, istemci=_istemci(ulasmamali), onbellek=Onbellek())


def test_adet_sinirlanir():
    gorulen = []

    def isleyici(istek):
        gorulen.append(int(istek.url.params["per_page"]))
        return httpx.Response(200, json={"photos": []})

    for adet in (0, 500):
        stok.ara("pexels", "gorsel", "konu", ANAHTAR, adet=adet, istemci=_istemci(isleyici), onbellek=Onbellek())
    assert gorulen == [1, 20]


def test_sonuclar_24_saat_onbelleklenir_ve_sure_dolunca_yenilenir():
    zaman = [1000.0]
    cagri = []

    def isleyici(istek):
        cagri.append(1)
        return httpx.Response(200, json=_pexels_video_yaniti())

    ob = Onbellek(sure_sn=24 * 3600, saat=lambda: zaman[0])
    ist = _istemci(isleyici)
    ilk = stok.ara("pexels", "video", "Woman Typing", ANAHTAR, istemci=ist, onbellek=ob)
    assert stok.ara("pexels", "video", "woman   typing", ANAHTAR, istemci=ist, onbellek=ob) == ilk and len(cagri) == 1
    stok.ara("pexels", "video", "baska konu", ANAHTAR, istemci=ist, onbellek=ob)
    assert len(cagri) == 2                                           # farkli arama: yeni istek
    zaman[0] += 24 * 3600 - 1
    assert ob.aday_getir(ilk[0].kimlik) == ilk[0]                    # sure dolmadan aday hala kayitli
    zaman[0] += 2
    assert ob.aday_getir(ilk[0].kimlik) is None                      # sure dolunca silinir
    stok.ara("pexels", "video", "woman typing", ANAHTAR, istemci=ist, onbellek=ob)
    assert len(cagri) == 3


# ============================================================ stok: indirme

def _aday(indirme="https://videos.pexels.com/video-files/1/hd.mp4", tur="video", kaynak="pexels"):
    return Aday(kimlik="a" * 16, kaynak=kaynak, tur=tur, onizleme="", indirme=indirme,
                sayfa="https://www.pexels.com/video/1/", kredi="Ada", kredi_adresi="")


def test_indirme_dosyayi_beklenen_adla_yazar(tmp_path):
    ist = _istemci(lambda i: httpx.Response(200, content=b"V" * 2048, headers={"content-type": "video/mp4"}))
    yol = stok.indir(_aday(), tmp_path / "medya", 3, istemci=ist)
    assert yol.name == "03-pexels-aaaaaaaaaaaaaaaa.mp4" and yol.read_bytes() == b"V" * 2048
    assert MEDYA_DOSYA_DESENI.fullmatch(yol.name)
    assert [p.name for p in (tmp_path / "medya").iterdir()] == [yol.name]       # gecici dosya kalmadi


@pytest.mark.parametrize("tur,ctype,uzanti", [("gorsel", "image/jpeg", ".jpg"), ("gorsel", "image/png", ".png"),
                                              ("gorsel", "image/webp", ".webp"), ("video", "application/octet-stream", ".mp4"),
                                              ("gorsel", "application/octet-stream", ".jpg")])
def test_indirme_uzantisi_icerik_turune_gore(tmp_path, tur, ctype, uzanti):
    ist = _istemci(lambda i: httpx.Response(200, content=b"x" * 100, headers={"content-type": ctype}))
    adres = "https://videos.pexels.com/a" if tur == "video" else "https://images.pexels.com/a"
    assert stok.indir(_aday(adres, tur), tmp_path, 1, istemci=ist).suffix == uzanti


@pytest.mark.parametrize("adres", [
    "https://evil.example/video.mp4", "http://videos.pexels.com/v.mp4", "https://pexels.com.evil.example/v.mp4",
    "https://evilpexels.com/v.mp4", "file:///C:/Windows/win.ini", "https://kullanici:sifre@videos.pexels.com/v.mp4",
    "ftp://videos.pexels.com/v.mp4", "//videos.pexels.com/v.mp4", "",
])
def test_izin_listesi_disindaki_adresler_ag_cagrisi_yapmadan_reddedilir(tmp_path, adres):
    def ulasmamali(istek):
        raise AssertionError("izinsiz adrese istek atildi")
    with pytest.raises(StokHatasi, match="izin verilen"):
        stok.indir(_aday(adres), tmp_path, 1, istemci=_istemci(ulasmamali))
    assert list(tmp_path.iterdir()) == []


def test_izinli_adresten_yabanci_adrese_yonlendirme_durdurulur_ve_dosya_birakilmaz(tmp_path):
    def isleyici(istek):
        if istek.url.host == "videos.pexels.com":
            return httpx.Response(302, headers={"location": "https://evil.example/gizli.mp4"})
        return httpx.Response(200, content=b"ZARARLI", headers={"content-type": "video/mp4"})
    with pytest.raises(StokHatasi, match="yönlendirildi"):
        stok.indir(_aday(), tmp_path, 1, istemci=_istemci(isleyici))
    assert list(tmp_path.iterdir()) == []


def test_izinli_adrese_yonlendirme_calisir(tmp_path):
    def isleyici(istek):
        if istek.url.host == "pixabay.com":
            return httpx.Response(302, headers={"location": "https://cdn.pixabay.com/photo/x.jpg"})
        return httpx.Response(200, content=b"J" * 10, headers={"content-type": "image/jpeg"})
    yol = stok.indir(_aday("https://pixabay.com/get/x.jpg", "gorsel", "pixabay"), tmp_path, 2, istemci=_istemci(isleyici))
    assert yol.suffix == ".jpg" and yol.name.startswith("02-pixabay-")


@pytest.mark.parametrize("yanit,beklenen", [
    (httpx.Response(404), "başarısız"), (httpx.Response(200, content=b"", headers={"content-type": "video/mp4"}), "Boş"),
    (httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}), "video veya görsel değil"),
])
def test_indirme_hatalari_temiz_birakir(tmp_path, yanit, beklenen):
    with pytest.raises(StokHatasi, match=beklenen):
        stok.indir(_aday(), tmp_path, 1, istemci=_istemci(lambda i: yanit))
    assert list(tmp_path.iterdir()) == []


def test_boyut_siniri_content_length_ve_akis_sirasinda_uygulanir(tmp_path, monkeypatch):
    monkeypatch.setattr(stok, "VIDEO_AZAMI_BAYT", 1000)
    buyuk = httpx.Response(200, content=b"B" * 5000, headers={"content-type": "video/mp4", "content-length": "5000"})
    with pytest.raises(StokHatasi, match="boyuttan büyük"):
        stok.indir(_aday(), tmp_path, 1, istemci=_istemci(lambda i: buyuk))

    def akis(istek):                                               # content-length yok, gercek boyut sinirdan buyuk
        r = httpx.Response(200, content=b"B" * 5000, headers={"content-type": "video/mp4"})
        r.headers.pop("content-length", None)
        return r
    with pytest.raises(StokHatasi, match="boyuttan büyük"):
        stok.indir(_aday(), tmp_path, 1, istemci=_istemci(akis))
    assert list(tmp_path.iterdir()) == []


def test_indirme_ag_hatasi_temiz_mesaj(tmp_path):
    def kapali(istek):
        raise httpx.ReadError("kesildi")
    with pytest.raises(StokHatasi, match="bağlantı hatası") as hata:
        stok.indir(_aday(), tmp_path, 1, istemci=_istemci(kapali))
    assert hata.value.__cause__ is None and hata.value.__suppress_context__ is True and list(tmp_path.iterdir()) == []


def test_kredi_metni():
    assert stok.kredi_metni(_aday()) == "Video: Ada / Pexels"
    assert stok.kredi_metni(_aday(tur="gorsel", kaynak="pixabay")) == "Fotoğraf: Ada / Pixabay"


# ============================================================ paket: sema ve medya kayitlari

def test_istem_modelden_sahne_basina_gorsel_alanlarini_ister_ve_dogrulama_korur():
    assert "gorsel_aramasi" in SISTEM and "gorsel_istemi" in SISTEM
    veri = {
        "basliklar": ["b"], "caption": "c", "hashtagler": ["#abcdef"],
        "senaryo": [
            {"sure_sn": 3, "anlatim": "bir", "ekran_yazisi": "x", "gorsel_aramasi": "woman typing", "gorsel_istemi": "A close-up shot"},
            {"sure_sn": 3, "anlatim": "iki"},                      # alanlar yoksa bos string
            {"anlatim": "uc", "gorsel_aramasi": "q" * 500, "gorsel_istemi": ["liste"]},
        ],
    }
    sen = dogrula_yanit(veri)["senaryo"]
    assert (sen[0]["gorsel_aramasi"], sen[0]["gorsel_istemi"]) == ("woman typing", "A close-up shot")
    assert (sen[1]["gorsel_aramasi"], sen[1]["gorsel_istemi"]) == ("", "")
    assert len(sen[2]["gorsel_aramasi"]) <= 101 and sen[2]["gorsel_istemi"] == ""


def _paket_yaz(klasor: Path, sahne_sayisi: int = 2) -> Path:
    hedef = klasor / PAKET_ID
    hedef.mkdir(parents=True)
    paket = {"id": PAKET_ID, "konu": "test", "olusturma": "2026-10-25T12:00:00+00:00", "saglayici": "sahte",
             "basliklar": ["b"], "secili_baslik": 0, "caption": "c", "hashtagler": [], "seo": {}, "analiz": {},
             "kapak": {}, "trendler": [], "uyarilar": [], "medya": {},
             "senaryo": [{"sure_sn": 3, "anlatim": f"sahne {i}", "ekran_yazisi": "", "gorsel_aramasi": f"query {i}",
                          "gorsel_istemi": ""} for i in range(1, sahne_sayisi + 1)]}
    (hedef / "paket.json").write_text(json.dumps(paket, ensure_ascii=False), encoding="utf-8")
    return hedef


def _kayit(dosya="01-pexels-aaaaaaaaaaaaaaaa.mp4"):
    return {"kaynak": "pexels", "tur": "video", "dosya": dosya, "sayfa": "https://www.pexels.com/video/1/",
            "kredi": "Ada", "kredi_adresi": "", "kredi_metni": "Video: Ada / Pexels"}


def test_medya_kaydet_kaldir_ve_krediler(tmp_path):
    hedef = _paket_yaz(tmp_path)
    (hedef / "medya").mkdir()
    (hedef / "medya" / "01-pexels-aaaaaaaaaaaaaaaa.mp4").write_bytes(b"v")
    assert medya_kaydet(tmp_path, PAKET_ID, 1, _kayit())["medya"]["1"]["dosya"].endswith(".mp4")
    assert (hedef / "krediler.txt").read_text(encoding="utf-8") == "Sahne 1: Video: Ada / Pexels - https://www.pexels.com/video/1/\n"
    assert paketi_oku(tmp_path, PAKET_ID)["medya"]["1"]["kredi"] == "Ada"
    assert medya_kaldir(tmp_path, PAKET_ID, 1) is True
    assert not (hedef / "medya" / "01-pexels-aaaaaaaaaaaaaaaa.mp4").exists() and not (hedef / "krediler.txt").exists()
    assert paketi_oku(tmp_path, PAKET_ID)["medya"] == {} and medya_kaldir(tmp_path, PAKET_ID, 1) is False


def test_medya_kaldir_yalnizca_dogrulanmis_dosya_adini_siler(tmp_path):
    hedef = _paket_yaz(tmp_path)
    disarida = tmp_path / "gizli.txt"
    disarida.write_text("silinmemeli", encoding="utf-8")
    for kotu in ("../../gizli.txt", "..\\..\\gizli.txt", "/etc/passwd", "gizli.txt"):
        medya_kaydet(tmp_path, PAKET_ID, 1, _kayit(kotu))
        assert medya_kaldir(tmp_path, PAKET_ID, 1) is True
    assert disarida.read_text(encoding="utf-8") == "silinmemeli"


@pytest.mark.parametrize("kimlik", ["../x", "", "20261025-120000-X", "a/b"])
def test_medya_islemleri_gecersiz_paket_kimligini_reddeder(tmp_path, kimlik):
    assert medya_kaydet(tmp_path, kimlik, 1, _kayit()) is None and medya_kaldir(tmp_path, kimlik, 1) is False


# ============================================================ panel

def _stok_sunucusu(istekler=None):
    """Pexels API'si + CDN'ini taklit eder."""
    def isleyici(istek):
        if istekler is not None:
            istekler.append(istek)
        host = istek.url.host
        if host == "api.pexels.com":
            if istek.headers.get("Authorization") != ANAHTAR:
                return httpx.Response(401, json={"status": 401, "message": "Invalid API key"})
            return httpx.Response(200, json=_pexels_video_yaniti() if "videos" in istek.url.path else
                                  {"photos": [{"id": 7, "url": "https://www.pexels.com/photo/7/", "photographer": "Can",
                                               "photographer_url": "https://www.pexels.com/@can", "width": 10, "height": 20,
                                               "src": {"medium": "https://images.pexels.com/m.jpg", "large2x": "https://images.pexels.com/l.jpg"}}]})
        if host == "videos.pexels.com":
            return httpx.Response(200, content=b"MP4" * 100, headers={"content-type": "video/mp4"})
        if host == "images.pexels.com":
            return httpx.Response(200, content=b"JPG" * 50, headers={"content-type": "image/jpeg"})
        return httpx.Response(404)
    return isleyici


@pytest.fixture
def panel(tmp_path):
    klasor = tmp_path / "paketler"
    klasor.mkdir()
    _paket_yaz(klasor)
    istekler: list = []
    depo = AnahtarDeposu(tmp_path / "anahtarlar.json", ortam={})
    app = uygulama_olustur(
        paket_klasoru=klasor, saglayicilar=lambda: [], saglayici_bulucu=lambda a: None,
        izinli_hostlar=("127.0.0.1", "localhost", "testserver"), kapak_ciz=False,
        anahtar_depo=depo, stok_istemci=_istemci(_stok_sunucusu(istekler)), stok_onbellek=Onbellek(),
    )
    istemci = TestClient(app)
    istemci.depo, istemci.klasor, istemci.istekler, istemci.app_ = depo, klasor, istekler, app
    return istemci


def test_panel_video_siteleri_ve_guvenlik_basliklari(panel):
    r = panel.get("/api/video-siteleri")
    assert r.status_code == 200 and {g["anahtar"] for g in r.json()["gruplar"]} == {"ucretsiz", "yok", "bilinmiyor"}
    csp = panel.get("/").headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'none'" in csp and "object-src 'none'" in csp and "connect-src 'self'" in csp


def test_panel_anahtar_akisi_anahtar_hicbir_yanitta_gorunmez(panel):
    assert all(not a["tanimli"] for a in panel.get("/api/anahtarlar").json())
    r = panel.put("/api/anahtar/pexels", json={"deger": ANAHTAR})
    assert r.status_code == 200 and ANAHTAR not in r.text and r.json() == {"ad": "pexels", "tanimli": True}
    liste = panel.get("/api/anahtarlar")
    assert ANAHTAR not in liste.text and next(a for a in liste.json() if a["ad"] == "pexels")["tanimli"]
    assert panel.depo.getir("pexels") == ANAHTAR
    assert panel.delete("/api/anahtar/pexels").status_code == 200
    assert panel.delete("/api/anahtar/pexels").status_code == 404 and panel.delete("/api/anahtar/rastgele").status_code == 404


@pytest.mark.parametrize("govde,kod", [({"deger": "kisa"}, 422), ({}, 422), ({"deger": "x" * 400}, 422),
                                       ({"deger": "bosluk iceren anahtar degeri"}, 400), ({"deger": "turkce-ğüşiöç-12345"}, 400)])
def test_panel_gecersiz_anahtar_reddedilir_ve_degeri_geri_yansitmaz(panel, govde, kod):
    r = panel.put("/api/anahtar/pexels", json=govde)
    assert r.status_code == kod
    for parca in govde.values():
        assert str(parca) not in r.text or len(str(parca)) < 6
    assert panel.put("/api/anahtar/rastgele", json={"deger": ANAHTAR}).status_code == 400


def test_panel_anahtar_uclari_siteler_arasi_isteklere_kapali(panel):
    assert panel.put("/api/anahtar/pexels", json={"deger": ANAHTAR}, headers={"Origin": "https://kotu.example"}).status_code == 403
    assert panel.delete("/api/anahtar/pexels", headers={"Origin": "https://kotu.example"}).status_code == 403
    assert panel.put("/api/anahtar/pexels", content=f"deger={ANAHTAR}",
                     headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert panel.put("/api/anahtar/pexels", content=f"deger={ANAHTAR}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert panel.depo.getir("pexels") is None


def test_panel_stok_ara_anahtar_yoksa_yonlendirir(panel):
    r = panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "video", "kaynak": "pexels", "sorgu": "typing"})
    assert r.status_code == 400 and "anahtarı tanımlı değil" in r.json()["detail"] and panel.istekler == []


def test_panel_stok_ara_ve_gecersiz_girdiler(panel):
    panel.depo.kaydet("pexels", ANAHTAR)
    r = panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "video", "kaynak": "pexels", "sorgu": "typing"})
    j = r.json()
    assert r.status_code == 200 and j["kaynak_adi"] == "Pexels" and j["kaynak_adresi"] == "https://www.pexels.com"
    assert len(j["adaylar"]) == 1 and j["adaylar"][0]["kredi"] == "Ada"
    for kotu in ({"sahne": 0}, {"sahne": 13}, {"tur": "ses"}, {"kaynak": "flickr"}, {"sorgu": "a"}, {"sorgu": "x" * 101}):
        assert panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "video", "kaynak": "pexels", "sorgu": "typing", **kotu}).status_code == 422
    assert panel.post("/api/paket/20261025-999999-yok/stok", json={"sahne": 1, "tur": "video", "kaynak": "pexels", "sorgu": "typing"}).status_code == 404
    assert panel.post("/api/paket/..%2F..%2Fx/stok", json={"sahne": 1, "tur": "video", "kaynak": "pexels", "sorgu": "typing"}).status_code in (404, 422)


def test_panel_gecersiz_anahtarla_arama_anlasilir_hata_verir_ve_anahtari_sizdirmaz(panel):
    panel.depo.kaydet("pexels", "YANLIS-ANAHTAR-0123456789")
    r = panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "video", "kaynak": "pexels", "sorgu": "typing"})
    assert r.status_code == 400 and "geçersiz" in r.json()["detail"] and "YANLIS-ANAHTAR" not in r.text


def _ara(panel, tur="video"):
    panel.depo.kaydet("pexels", ANAHTAR)
    r = panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": tur, "kaynak": "pexels", "sorgu": "typing"})
    assert r.status_code == 200
    return r.json()["adaylar"][0]


def test_panel_medya_secimi_uctan_uca(panel):
    aday = _ara(panel)
    r = panel.post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, "aday_kimligi": aday["kimlik"]})
    assert r.status_code == 200
    kayit = r.json()
    dosya = panel.klasor / PAKET_ID / "medya" / kayit["dosya"]
    assert dosya.read_bytes() == b"MP4" * 100 and MEDYA_DOSYA_DESENI.fullmatch(kayit["dosya"]) and kayit["kredi_metni"] == "Video: Ada / Pexels"
    paket = panel.get(f"/api/paket/{PAKET_ID}").json()
    assert paket["medya"]["1"]["dosya"] == kayit["dosya"]
    krediler = (panel.klasor / PAKET_ID / "krediler.txt").read_text(encoding="utf-8")
    assert krediler == 'Sahne 1: "ornek" — Ada — Pexels License — https://www.pexels.com/video/ornek-111/\n'
    assert "ATIF ZORUNLU" not in krediler            # Pexels atif ONERIR, zorunlu kilmaz
    assert kayit["lisans"] == "Pexels License" and kayit["atif_gerekli"] is False
    sunulan = panel.get(f"/paketler/{PAKET_ID}/medya/{kayit['dosya']}")
    assert sunulan.status_code == 200 and sunulan.headers["content-type"] == "video/mp4" and sunulan.content == b"MP4" * 100
    # ayni sahneye baska medya secilirse eskisinin dosyasi silinir
    gorsel = _ara(panel, "gorsel")
    k2 = panel.post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, "aday_kimligi": gorsel["kimlik"]}).json()
    assert k2["dosya"].endswith(".jpg") and not dosya.exists() and (panel.klasor / PAKET_ID / "medya" / k2["dosya"]).is_file()
    assert [p.name for p in (panel.klasor / PAKET_ID / "medya").iterdir()] == [k2["dosya"]]
    # kaldir
    assert panel.delete(f"/api/paket/{PAKET_ID}/medya/1").status_code == 200
    assert list((panel.klasor / PAKET_ID / "medya").iterdir()) == [] and not (panel.klasor / PAKET_ID / "krediler.txt").exists()
    assert panel.delete(f"/api/paket/{PAKET_ID}/medya/1").status_code == 404


def test_panel_istemci_url_gonderemez_yalnizca_kendi_adaylarimiz_indirilir(panel):
    for kotu in ({"aday_kimligi": "0" * 16}, {"aday_kimligi": "a" * 16}):
        assert panel.post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, **kotu}).status_code == 404
    for kotu in ({"aday_kimligi": "https://evil.example/x.mp4"}, {"aday_kimligi": "../../etc"}, {"aday_kimligi": "Z" * 16}, {"aday_kimligi": ""}):
        assert panel.post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, **kotu}).status_code == 422
    assert panel.post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, "aday_kimligi": "0" * 16, "indirme": "https://evil.example/x"}).status_code == 404
    assert not (panel.klasor / PAKET_ID / "medya").exists()


def test_panel_zararli_aday_adresi_indirilmez(panel):
    # Ele gecirilmis bir API yanitini simule eder: onbellekteki aday izin disi bir adrese isaret ediyor.
    zararli = Aday(kimlik="b" * 16, kaynak="pexels", tur="video", onizleme="", indirme="https://evil.example/x.mp4",
                   sayfa="https://www.pexels.com/video/1/", kredi="X", kredi_adresi="")
    onbellek = Onbellek()
    onbellek.arama_koy(("x",), [zararli])

    def ulasmamali(istek):
        raise AssertionError("ag cagrisi yapilmamali")

    app2 = uygulama_olustur(
        paket_klasoru=panel.klasor, saglayicilar=lambda: [], saglayici_bulucu=lambda a: None,
        izinli_hostlar=("testserver",), kapak_ciz=False, anahtar_depo=panel.depo,
        stok_istemci=_istemci(ulasmamali), stok_onbellek=onbellek,
    )
    r = TestClient(app2).post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, "aday_kimligi": "b" * 16})
    assert r.status_code == 400 and "izin verilen" in r.json()["detail"]
    assert not (panel.klasor / PAKET_ID / "medya").exists() or list((panel.klasor / PAKET_ID / "medya").iterdir()) == []


def test_panel_sahne_siniri_ve_medya_ucu_origin_denetimi(panel):
    aday = _ara(panel)
    assert panel.post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 3, "aday_kimligi": aday["kimlik"]}).status_code == 400   # 2 sahne var
    assert panel.post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, "aday_kimligi": aday["kimlik"]},
                      headers={"Origin": "https://kotu.example"}).status_code == 403
    assert panel.delete(f"/api/paket/{PAKET_ID}/medya/1", headers={"Origin": "https://kotu.example"}).status_code == 403


@pytest.mark.parametrize("yol", [
    f"/paketler/{PAKET_ID}/medya/..%2F..%2Fpaket.json", f"/paketler/{PAKET_ID}/medya/..%5Cpaket.json",
    f"/paketler/{PAKET_ID}/medya/paket.json", f"/paketler/{PAKET_ID}/medya/01-evil-aaaa.mp4",
    f"/paketler/{PAKET_ID}/medya/01-pexels-aaaa.exe", "/paketler/..%2Fx/medya/01-pexels-aaaa.mp4",
    f"/paketler/{PAKET_ID}/medya/01-pexels-aaaa.mp4",       # desene uyuyor ama dosya yok
])
def test_panel_medya_dosyasi_yol_gezintisi_ve_desen_disi_adlar_reddedilir(panel, yol):
    (panel.klasor / PAKET_ID / "medya").mkdir(exist_ok=True)
    (panel.klasor.parent / "paket.json").write_text("GIZLI", encoding="utf-8")
    r = panel.get(yol)
    assert r.status_code in (400, 404) and b"GIZLI" not in r.content and b'"konu"' not in r.content


def test_arayuz_dis_baglantilari_yalnizca_https_ile_ve_guvenli_olusturur():
    html = (Path(__file__).resolve().parent.parent / "src" / "icerik" / "panel.js").read_text(encoding="utf-8")
    assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "document.write" not in html and "eval(" not in html
    assert "noopener noreferrer" in html and "protocol === 'https:'" in html
    assert 'type = \'password\'' in html and "autocomplete = 'off'" in html     # anahtar girisi maskeli
    assert "localStorage" not in html and "sessionStorage" not in html          # anahtar tarayicida saklanmaz


# ============================================================ ikilemeler ve depo hijyeni

def test_mesru_ikilemeler_yanlis_alarm_vermez_ama_gercek_tekrar_verir():
    from src.icerik.paket import kalite_uyarilari

    def icerik(caption):
        return {"caption": caption, "basliklar": ["b"], "senaryo": [{"sure_sn": 30, "anlatim": "x"}], "hashtagler": [{}] * 5}

    temiz = kalite_uyarilari(icerik("Mesajları tek tek yanıtla. Çok çok önemli. Adım adım ilerle. TEK TEK de olur."))
    assert not any("yan yana" in u for u in temiz)
    assert any("takipçi takipçi" in u for u in kalite_uyarilari(icerik("İlk takipçi takipçi geldi")))


def test_gitignore_anahtar_dosyasini_ve_uretilen_icerigi_yoksayar():
    satirlar = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    for gerekli in ("config/.env", "config/anahtarlar.json", "paketler/", "*.db"):
        assert gerekli in satirlar, f".gitignore'da yok: {gerekli}"
