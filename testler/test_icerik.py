# -*- coding: utf-8 -*-
"""Icerik Studyosu testleri: yazi motoru, trend, paket, panel guvenligi."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from src.icerik import paket as paket_modulu
from src.icerik import saglayici as sg
from src.icerik.panel import uygulama_olustur
from src.icerik.paket import (
    DOGRULANMADI, dogrula_yanit, gecerli_id_mi, hashtag_kaynaklari, paket_olustur,
    paketi_oku, paketi_sil, paketleri_listele, trendleri_birlestir,
)
from src.icerik.saglayici import SaglayiciHatasi, json_ayikla
from src.icerik.trend import (
    TrendKelime, etiket_haritasi, etikete_cevir, icerik_sozcukleri, ifade_sorgu_plani, ifade_trendleri,
    ilgi_puani, konu_trendleri, sorgu_plani,
)


# ---------------------------------------------------------------- yardimcilar

GECERLI = {
    "basliklar": ["Birinci baslik", "Ikinci baslik", "Ucuncu baslik"],
    "kapak": {"ust_metin": "YAPAY ZEKA", "baslik": "Is akisini otomatiklestir", "alt_metin": "3 pratik yol"},
    "caption": "Tekrar eden isleri birakin.\nNasil mi?\nSizce hangisi en kolay?",
    "hashtagler": [
        {"etiket": "#yapayzeka", "gerekce": "ana konu"},
        {"etiket": "Otomasyon", "gerekce": "isaretsiz de kabul"},
        {"etiket": "#yapayzeka", "gerekce": "tekrar"},
        {"etiket": "#ozelbirseyxyz", "gerekce": "model icadi"},
        {"etiket": "#ücretsiz", "gerekce": "arama onerisinde gecen kelime"},
    ],
    "seo": {"anahtar_kelimeler": ["yapay zeka otomasyon", "is akisi"], "alt_metin": "Koyu zeminde baslik"},
    "analiz": {"aci": "pratik", "hedef_kitle": "girisimciler", "hook": "Bunu hala elle mi yapiyorsun?",
               "neden_isler": "somut", "riskler": "abartma", "trend_ozeti": "ile/nasil sorgulari"},
    "senaryo": [{"sure_sn": 3, "anlatim": "Bunu hala elle mi yapiyorsun?", "ekran_yazisi": "Hala elle mi?"}],
}


class SahteSaglayici:
    ad = "sahte"
    aciklama = "Sahte"

    def __init__(self, yanitlar):
        self.yanitlar = list(yanitlar)
        self.cagri = 0

    def hazir_mi(self):
        return True, "hazir"

    def uret(self, sistem, istem, *, zaman_asimi=300):
        self.cagri += 1
        y = self.yanitlar.pop(0) if len(self.yanitlar) > 1 else self.yanitlar[0]
        if isinstance(y, Exception):
            raise y
        return y if isinstance(y, str) else json.dumps(y, ensure_ascii=False)


def sahte_trend(konu, dil):
    # Gercek bir oneri: 'otomasyon', 'araclari', 'ucretsiz' kelimeleri bu ifadede gecer.
    return [TrendKelime("otomasyon araçları ücretsiz", ["google", "youtube"])], []


def sahte_ifade(ifadeler, dil):
    return [], []


# ---------------------------------------------------------------- json_ayikla

def test_json_ayikla_duz_ve_kod_blogu_ve_etrafinda_metin():
    assert json_ayikla('{"a": 1}') == {"a": 1}
    assert json_ayikla('```json\n{"a": {"b": [1, 2]}}\n```') == {"a": {"b": [1, 2]}}
    assert json_ayikla('Tabii, iste:\n{"a": "su } sekilde"} umarim yarar') == {"a": "su } sekilde"}


@pytest.mark.parametrize("metin", ["", "json yok", "{bozuk", "[1,2,3]"])
def test_json_ayikla_gecersiz_girdide_hata(metin):
    with pytest.raises(ValueError):
        json_ayikla(metin)


# ---------------------------------------------------------------- etiket

@pytest.mark.parametrize("girdi,beklenen", [
    ("yapay zeka", "#yapayzeka"),
    ("İstanbul Çiçekleri", "#istanbulçiçekleri"),
    ("IŞIK", "#ışık"),
    ("  a-b_c!  ", "#abc"),
    ("12345", None),
    ("", None),
    ("x" * 31, None),
])
def test_etikete_cevir(girdi, beklenen):
    assert etikete_cevir(girdi) == beklenen


# ---------------------------------------------------------------- trend

def _istemci(isleyici):
    return httpx.Client(transport=httpx.MockTransport(isleyici))


def test_trend_kaynaklari_birlestirir_ve_tekrarsiz():
    def isleyici(istek):
        yt = "ds=yt" in str(istek.url)
        oneriler = ["yapay zeka ile video", "Yapay Zeka ile Müzik"] if yt else ["yapay zeka ile video", "yapay zeka ile ses"]
        return httpx.Response(200, json=["q", oneriler])

    kelimeler, uyari = konu_trendleri("yapay zeka", "tr", istemci=_istemci(isleyici))
    harita = {k.kelime.lower(): k for k in kelimeler}
    assert not any("basarisiz" in u or "ulasilamadi" in u for u in uyari)   # ag sorunu yok
    assert sorted(harita["yapay zeka ile video"].kaynaklar) == ["google", "youtube"]
    assert harita["yapay zeka ile ses"].kaynaklar == ["google"]
    assert len({k.kelime.lower() for k in kelimeler}) == len(kelimeler)
    assert kelimeler[0].kelime == "yapay zeka ile video"  # iki kaynakta gecen once


def test_trend_ag_tamamen_kapaliysa_cokmez_ve_uyarir():
    def isleyici(istek):
        raise httpx.ConnectError("kapali")

    kelimeler, uyari = konu_trendleri("konu", "tr", istemci=_istemci(isleyici))
    assert kelimeler == []
    assert len(uyari) == 1 and "ulasilamadi" in uyari[0]


def test_trend_kismi_basarisizlikta_uyarir():
    sayac = {"n": 0}

    def isleyici(istek):
        sayac["n"] += 1
        if "ds=yt" in str(istek.url):
            return httpx.Response(500)
        return httpx.Response(200, json=["q", ["bir sey"]])

    kelimeler, uyari = konu_trendleri("konu", "tr", istemci=_istemci(isleyici))
    assert kelimeler and "basarisiz" in uyari[0]


def test_trend_bozuk_json_istisna_firlatmaz():
    kelimeler, uyari = konu_trendleri("konu", "tr", istemci=_istemci(lambda i: httpx.Response(200, text="html <b>")))
    assert kelimeler == [] and uyari


# ---------------------------------------------------------------- dogrulama

def test_dogrula_yanit_normallestirir_ve_tekrari_atar():
    d = dogrula_yanit(GECERLI)
    etiketler = [h["etiket"] for h in d["hashtagler"]]
    assert etiketler == ["#yapayzeka", "#otomasyon", "#ozelbirseyxyz", "#ücretsiz"]
    assert len(d["basliklar"]) == 3 and d["senaryo"][0]["sure_sn"] == 3


@pytest.mark.parametrize("bozucu", [
    lambda d: d.pop("basliklar"),
    lambda d: d.__setitem__("basliklar", "metin"),
    lambda d: d.__setitem__("caption", "   "),
    lambda d: d.__setitem__("hashtagler", []),
    lambda d: d.__setitem__("hashtagler", [{"etiket": "###"}]),
])
def test_dogrula_yanit_yapi_bozuksa_hata(bozucu):
    veri = json.loads(json.dumps(GECERLI))
    bozucu(veri)
    with pytest.raises(ValueError):
        dogrula_yanit(veri)


def test_dogrula_yanit_eksik_kapak_basliktan_doldurulur():
    veri = json.loads(json.dumps(GECERLI)); veri.pop("kapak")
    assert dogrula_yanit(veri)["kapak"]["baslik"] == "Birinci baslik"


def test_hashtag_dogrulama_gercek_onerinin_parcasi_olmali():
    etiketler = [{"etiket": "#videooluşturma", "gerekce": ""}, {"etiket": "#yapayzeka", "gerekce": ""},
                 {"etiket": "#ozelbirseyxyz", "gerekce": ""}]
    trend = TrendKelime("yapay zeka video oluşturma", ["google", "youtube"])
    sonuc = hashtag_kaynaklari(etiketler, [trend])
    assert sonuc[0]["dogrulandi"] and sonuc[0]["kaynak"] == "Google + YouTube önerilerinde geçiyor"
    assert sonuc[1]["dogrulandi"]              # gercek bir aramanin parcasi
    assert not sonuc[2]["dogrulandi"] and sonuc[2]["kaynak"] == DOGRULANMADI   # hic onerisi yok


def test_etiket_haritasi_bir_uc_sozcuklu_parcalari_cikarir():
    harita = etiket_haritasi([TrendKelime("a bb ccc dddd", ["google"])])
    assert {"#bb", "#abb", "#abbccc", "#bbccc", "#bbcccdddd", "#cccdddd"} <= set(harita)
    assert "#abbcccdddd" not in harita          # 4 sozcuklu parca yok


def test_hashtag_oncelikli_ilk_bes():
    etiketler = [{"etiket": f"#e{i}x", "gerekce": ""} for i in range(8)]
    sonuc = hashtag_kaynaklari(etiketler, [])
    assert [h["oncelikli"] for h in sonuc] == [True] * 5 + [False] * 3


# ---------------------------------------------------------------- paket

def _olustur(tmp_path, saglayici=None, **kw):
    return paket_olustur(
        "Yapay zeka ile otomasyon", saglayici=saglayici or SahteSaglayici([GECERLI]),
        klasor=tmp_path, kapak_ciz=False, trend_getir=sahte_trend, ifade_getir=sahte_ifade,
        an=datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc), **kw,
    )


def test_paket_olustur_dosyalari_yazar(tmp_path):
    p = _olustur(tmp_path)
    assert p["id"] == "20261025-120000-yapay-zeka-ile-otomasyon"
    klasor = tmp_path / p["id"]
    assert (klasor / "paket.json").is_file() and (klasor / "kopyala.txt").is_file()
    assert "Birinci baslik" in (klasor / "kopyala.txt").read_text(encoding="utf-8")
    assert paketi_oku(tmp_path, p["id"])["konu"] == "Yapay zeka ile otomasyon"
    dogrulanan = {h["etiket"]: h["dogrulandi"] for h in p["hashtagler"]}
    # 'otomasyon' ve 'ucretsiz' gercek bir onerinin icinde gecer; 'yapayzeka' ve uydurma etiket gecmez
    assert dogrulanan == {"#yapayzeka": False, "#otomasyon": True, "#ozelbirseyxyz": False, "#ücretsiz": True}


def test_paket_ilerleme_adimlari_sirayla_bildirilir(tmp_path):
    adimlar = []
    _olustur(tmp_path, ilerleme=lambda a, m: adimlar.append(a))
    assert adimlar == ["trend", "yazi", "dogrula", "kayit"]  # kapak_ciz=False


def test_bozuk_yanit_bir_kez_yeniden_denenir(tmp_path):
    s = SahteSaglayici(["bu json degil", GECERLI])
    _olustur(tmp_path, saglayici=s)
    assert s.cagri == 2


def test_iki_kez_bozuk_yanit_hata_verir_ve_klasor_olusmaz(tmp_path):
    s = SahteSaglayici(["bozuk"])
    with pytest.raises(SaglayiciHatasi, match="geçerli bir yanıt"):
        _olustur(tmp_path, saglayici=s)
    assert s.cagri == 2 and list(tmp_path.iterdir()) == []


def test_saglayici_hatasi_aynen_yukari_cikar(tmp_path):
    with pytest.raises(SaglayiciHatasi, match="oturum"):
        _olustur(tmp_path, saglayici=SahteSaglayici([SaglayiciHatasi("oturum dustu")]))


def test_kapak_hatasi_paketi_dusurmez_uyari_ekler(tmp_path, monkeypatch):
    def patlat(*a, **k):
        raise RuntimeError("chromium yok")
    monkeypatch.setattr(paket_modulu, "kapak_uret", patlat)
    p = paket_olustur("Yapay zeka ile otomasyon", saglayici=SahteSaglayici([GECERLI]), klasor=tmp_path,
                      kapak_ciz=True, trend_getir=sahte_trend, ifade_getir=sahte_ifade)
    assert p["kapak"]["dosya"] is None and any("chromium" in u for u in p["uyarilar"])


@pytest.mark.parametrize("konu", ["", "  ", "ab", "x" * 301])
def test_gecersiz_konu_reddedilir(tmp_path, konu):
    with pytest.raises(ValueError):
        _olustur(tmp_path) if False else paket_olustur(
            konu, saglayici=SahteSaglayici([GECERLI]), klasor=tmp_path, kapak_ciz=False, trend_getir=sahte_trend, ifade_getir=sahte_ifade)


def test_ayni_saniyede_iki_paket_cakismaz(tmp_path):
    a, b = _olustur(tmp_path), _olustur(tmp_path)
    assert a["id"] != b["id"] and b["id"].endswith("-2")


@pytest.mark.parametrize("kimlik", ["../etc", "..", "a/b", "", "20261025-120000-x/../..", "20261025-120000-X", "x" * 80])
def test_gecersiz_paket_kimligi_reddedilir(tmp_path, kimlik):
    assert not gecerli_id_mi(kimlik)
    assert paketi_oku(tmp_path, kimlik) is None
    assert paketi_sil(tmp_path, kimlik) is False


def test_listele_ve_sil(tmp_path):
    p = _olustur(tmp_path)
    assert [o["id"] for o in paketleri_listele(tmp_path)] == [p["id"]]
    assert paketi_sil(tmp_path, p["id"]) and paketleri_listele(tmp_path) == []


# ---------------------------------------------------------------- saglayici

def _sahte_calistir(monkeypatch, stdout="", stderr="", kod=0):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, kod, stdout, stderr))


def test_oturum_dusmusse_anlasilir_hata(monkeypatch):
    _sahte_calistir(monkeypatch, stdout="Failed to authenticate: OAuth session expired", kod=1)
    with pytest.raises(SaglayiciHatasi, match="oturumu geçersiz|oturumu gecersiz"):
        sg._alt_surec(["claude"], "x", 5)


def test_oturum_hatasi_cikis_kodu_sifirda_da_yakalanir(monkeypatch):
    _sahte_calistir(monkeypatch, stdout="Failed to authenticate: OAuth session expired", kod=0)
    with pytest.raises(SaglayiciHatasi, match="gecersiz"):
        sg._alt_surec(["claude"], "x", 5)


def test_uzun_gecerli_yanit_giris_kelimesi_icerse_de_hata_sayilmaz(monkeypatch):
    uzun = '{"caption": "' + "login ekranini anlatan uzun bir metin " * 10 + '"}'
    _sahte_calistir(monkeypatch, stdout=uzun, kod=0)
    assert sg._alt_surec(["claude"], "x", 5) == uzun


def test_zaman_asimi_anlasilir_hata(monkeypatch):
    def patlat(*a, **k):
        raise subprocess.TimeoutExpired("claude", 1)
    monkeypatch.setattr(subprocess, "run", patlat)
    with pytest.raises(SaglayiciHatasi, match="cevap vermedi"):
        sg._alt_surec(["claude"], "x", 1)


def test_otomatik_secim_ornek_dosyayi_kendiliginden_secmez(monkeypatch):
    monkeypatch.setenv("IC_ORNEK_YANIT", __file__)
    monkeypatch.setattr(sg.shutil, "which", lambda ad: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SaglayiciHatasi, match="Kullanılabilir yazı motoru yok|Kullanilabilir yazi motoru yok"):
        sg.saglayici_sec("auto")


def test_adi_verilen_hazir_olmayan_saglayici_reddedilir(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SaglayiciHatasi, match="ANTHROPIC_API_KEY"):
        sg.saglayici_sec("anthropic_api")


# ---------------------------------------------------------------- panel

def _istemci_panel(tmp_path, saglayici=None, **kw):
    app = uygulama_olustur(
        paket_klasoru=tmp_path, saglayici_bulucu=lambda ad: saglayici or SahteSaglayici([GECERLI]),
        saglayicilar=lambda: [saglayici or SahteSaglayici([GECERLI])],
        izinli_hostlar=("127.0.0.1", "localhost", "testserver"),
        kapak_ciz=False, trend_getir=sahte_trend, ifade_getir=sahte_ifade, **kw,
    )
    return TestClient(app)


def _bekle(c, is_id, sinir=10):
    bitis = time.time() + sinir
    while time.time() < bitis:
        d = c.get(f"/api/is/{is_id}").json()
        if d["durum"] != "calisiyor":
            return d
        time.sleep(0.05)
    raise AssertionError("is bitmedi")


def test_panel_ana_sayfa_ve_durum(tmp_path):
    c = _istemci_panel(tmp_path)
    r = c.get("/")
    assert r.status_code == 200 and "İçerik" in r.text
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert c.get("/api/durum").json()["saglayicilar"][0]["hazir"] is True


def test_panel_uctan_uca_uretim(tmp_path):
    c = _istemci_panel(tmp_path)
    r = c.post("/api/uret", json={"konu": "Yapay zeka ile otomasyon", "dil": "tr", "ton": "samimi"})
    assert r.status_code == 202
    d = _bekle(c, r.json()["is_id"])
    assert d["durum"] == "bitti" and d["hata"] is None
    p = c.get(f"/api/paket/{d['paket_id']}").json()
    assert p["basliklar"][0] == "Birinci baslik"
    assert c.get(f"/paketler/{d['paket_id']}/kopyala.txt").status_code == 200
    assert c.get(f"/paketler/{d['paket_id']}/kapak.jpg").status_code == 404  # kapak cizilmedi
    assert len(c.get("/api/paketler").json()) == 1
    assert c.delete(f"/api/paket/{d['paket_id']}").status_code == 200
    assert c.get(f"/api/paket/{d['paket_id']}").status_code == 404


def test_panel_motor_hatasi_kullaniciya_ulasir(tmp_path):
    c = _istemci_panel(tmp_path, saglayici=SahteSaglayici([SaglayiciHatasi("Oturum süresi dolmuş.")]))
    d = _bekle(c, c.post("/api/uret", json={"konu": "bir konu"}).json()["is_id"])
    assert d["durum"] == "hata" and d["hata"] == "Oturum süresi dolmuş."


def test_panel_ayni_anda_ikinci_uretim_reddedilir(tmp_path):
    kapi = threading.Event()

    class Yavas(SahteSaglayici):
        def uret(self, *a, **k):
            kapi.wait(5)
            return super().uret(*a, **k)

    c = _istemci_panel(tmp_path, saglayici=Yavas([GECERLI]))
    ilk = c.post("/api/uret", json={"konu": "birinci konu"})
    assert ilk.status_code == 202
    assert c.post("/api/uret", json={"konu": "ikinci konu"}).status_code == 409
    kapi.set()
    assert _bekle(c, ilk.json()["is_id"])["durum"] == "bitti"
    assert c.post("/api/uret", json={"konu": "ucuncu konu"}).status_code == 202  # kilit acildi


def test_panel_yabanci_origin_reddedilir(tmp_path):
    c = _istemci_panel(tmp_path)
    r = c.post("/api/uret", json={"konu": "saldiri"}, headers={"Origin": "https://kotu-site.example"})
    assert r.status_code == 403
    assert c.delete("/api/paket/20261025-120000-x", headers={"Origin": "https://kotu-site.example"}).status_code == 403
    assert c.post("/api/uret", json={"konu": "mesru istek"}, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 202


def test_panel_form_gonderimi_reddedilir(tmp_path):
    c = _istemci_panel(tmp_path)
    r = c.post("/api/uret", content="konu=saldiri", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_panel_yabanci_host_reddedilir(tmp_path):
    app = uygulama_olustur(paket_klasoru=tmp_path, saglayici_bulucu=lambda a: SahteSaglayici([GECERLI]),
                           saglayicilar=lambda: [], kapak_ciz=False, trend_getir=sahte_trend, ifade_getir=sahte_ifade)
    assert TestClient(app, base_url="http://kotu-site.example").get("/api/paketler").status_code == 400
    assert TestClient(app, base_url="http://127.0.0.1:8000").get("/api/paketler").status_code == 200


@pytest.mark.parametrize("yol", [
    "/paketler/%2e%2e/kopyala.txt",
    "/paketler/..%2F/kopyala.txt",
    "/paketler/%2e%2e/kapak.jpg",
])
def test_panel_yol_gezintisi_gercek_dosyayi_sizdirmaz(tmp_path, yol):
    # Paket klasorunun DISINDA, tam hedef adli gercek dosyalar: dogrulama yoksa bunlar doner.
    klasor = tmp_path / "paketler"; klasor.mkdir()
    (tmp_path / "kopyala.txt").write_text("GIZLI-ICERIK", encoding="utf-8")
    (tmp_path / "kapak.jpg").write_bytes(b"GIZLI-GORSEL")
    c = _istemci_panel(klasor)
    r = c.get(yol)
    assert r.status_code in (400, 404)
    assert b"GIZLI" not in r.content


def test_panel_paket_json_disari_okunamaz(tmp_path):
    klasor = tmp_path / "paketler"; klasor.mkdir()
    (tmp_path / "paket.json").write_text('{"gizli": 1}', encoding="utf-8")
    c = _istemci_panel(klasor)
    for yol in ("/api/paket/%2e%2e", "/api/paket/..%2F"):
        r = c.get(yol)
        assert r.status_code in (400, 404) and b"gizli" not in r.content


@pytest.mark.parametrize("govde", [
    {"konu": "ab"}, {"konu": "x" * 301}, {"konu": "tamam konu", "dil": "fr"},
    {"konu": "tamam konu", "ton": "bagiran"}, {},
])
def test_panel_gecersiz_girdi_422(tmp_path, govde):
    assert _istemci_panel(tmp_path).post("/api/uret", json=govde).status_code == 422


def test_panel_model_ciktisi_html_olarak_calismaz(tmp_path):
    kotu = json.loads(json.dumps(GECERLI))
    kotu["basliklar"][0] = "<script>alert(1)</script>"
    c = _istemci_panel(tmp_path, saglayici=SahteSaglayici([kotu]))
    d = _bekle(c, c.post("/api/uret", json={"konu": "xss denemesi"}).json()["is_id"])
    # Veri JSON olarak saklanir; arayuz yalnizca textContent kullanir (asagida kontrol edilir).
    assert c.get(f"/api/paket/{d['paket_id']}").json()["basliklar"][0] == "<script>alert(1)</script>"
    for yol in ("/", "/panel.js"):                       # sayfa VE betik: hicbirinde HTML enjeksiyonu yuzeyi yok
        html = c.get(yol).text
        assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "document.write" not in html, yol
    assert "textContent" in c.get("/panel.js").text


# ---------------------------------------------------------------- claude oturum durumu

@pytest.fixture
def temiz_oturum_onbellegi():
    sg._OTURUM_ONBELLEK.clear()
    yield
    sg._OTURUM_ONBELLEK.clear()


def test_claude_giris_yapilmamissa_hazir_degil(monkeypatch, temiz_oturum_onbellegi):
    _sahte_calistir(monkeypatch, stdout='{"loggedIn": false, "authMethod": "none"}')
    hazir, neden = sg._claude_oturumu("claude")
    assert hazir is False and "/login" in neden


def test_claude_giris_yapilmissa_hazir(monkeypatch, temiz_oturum_onbellegi):
    _sahte_calistir(monkeypatch, stdout='{"loggedIn": true, "authMethod": "oauth"}')
    assert sg._claude_oturumu("claude")[0] is True


@pytest.mark.parametrize("cikti", ["", "yok boyle komut", "[1, 2]", '{"baska": 1}'])
def test_claude_durum_okunamazsa_engellemez_ama_durust_soyler(monkeypatch, temiz_oturum_onbellegi, cikti):
    _sahte_calistir(monkeypatch, stdout=cikti)
    hazir, neden = sg._claude_oturumu("claude")
    assert hazir is True and "denetlenemedi" in neden


def test_claude_durum_sorgusu_onbelleklenir(monkeypatch, temiz_oturum_onbellegi):
    sayac = {"n": 0}

    def calistir(*a, **k):
        sayac["n"] += 1
        return subprocess.CompletedProcess(a, 0, '{"loggedIn": true}', "")

    monkeypatch.setattr(subprocess, "run", calistir)
    for _ in range(5):
        sg._claude_oturumu("claude")
    assert sayac["n"] == 1


def test_auto_secim_girisi_olmayan_claudeyi_atlar_ve_nedeni_yazar(monkeypatch, temiz_oturum_onbellegi):
    monkeypatch.setattr(sg.shutil, "which", lambda ad: "claude" if ad == "claude" else None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _sahte_calistir(monkeypatch, stdout='{"loggedIn": false}')
    with pytest.raises(SaglayiciHatasi) as hata:
        sg.saglayici_sec("auto")
    assert "/login" in str(hata.value) and "Kullanılabilir yazı motoru yok" in str(hata.value)


# ---------------------------------------------------------------- trend: sorgu plani, ikinci gecis

KONU = "Yapay zeka ile tekrar eden işleri otomatikleştirmenin 3 yolu"


def test_icerik_sozcukleri_rakam_ve_durak_atar():
    assert icerik_sozcukleri(KONU) == ["yapay", "zeka", "tekrar", "eden", "işleri", "otomatikleştirmenin"]
    assert icerik_sozcukleri("How to use the best tools", "en") == ["use", "best", "tools"]


def test_sorgu_plani_tam_cumle_ve_kayan_pencere_tohumlari():
    plan = sorgu_plani(KONU)
    google = [q for ks, q in plan if ks == "google"]
    assert google[0] == KONU
    for beklenen in ("yapay zeka", "yapay zeka nasıl", "tekrar eden", "eden işleri", "otomatikleştirmenin"):
        assert beklenen in google
    assert "yapay zeka tekrar" not in google           # eski, bos donen kesit artik yok
    assert len(plan) == len(set(plan)) and {ks for ks, _ in plan} == {"google", "youtube"}
    assert len(plan) <= 40                              # kaynaklara nazik: sinirli istek sayisi


def test_sorgu_plani_tek_sozcuklu_konuda_cokmez():
    assert sorgu_plani("otomasyon")
    assert sorgu_plani("ab")[0][1] == "ab"


def test_ilgi_puani_kok_eslesmesi():
    soz = icerik_sozcukleri(KONU)
    # 'otomasyon' ~ 'otomatikleştirmenin' (ilk 5 harf), 'tekrar' tam eslesir
    assert ilgi_puani("tekrar eden işler otomasyon", soz) > ilgi_puani("yapay zeka ücretsiz", soz)
    assert ilgi_puani("alakasiz seyler", soz) == 0


def test_konu_trendleri_konuya_yakin_ifadeyi_basa_alir():
    def isleyici(istek):
        q = istek.url.params["q"]
        if q == "yapay zeka":
            return httpx.Response(200, json=[q, ["yapay zeka ücretsiz", "yapay zeka fotoğraf"]])
        if q == "tekrar eden":
            return httpx.Response(200, json=[q, ["tekrar eden işleri otomatikleştirme"]])
        return httpx.Response(200, json=[q, []])

    kelimeler, _ = konu_trendleri(KONU, "tr", istemci=_istemci(isleyici))
    assert kelimeler[0].kelime == "tekrar eden işleri otomatikleştirme"


def test_az_veri_uyarisi_ama_ag_hatasinda_cift_uyari_yok():
    kelimeler, uyari = konu_trendleri(KONU, "tr", istemci=_istemci(lambda i: httpx.Response(200, json=["q", ["tek"]])))
    assert len(kelimeler) == 1 and any("Az trend verisi" in u for u in uyari)

    def kapali(istek):
        raise httpx.ConnectError("kapali")
    _, uyari2 = konu_trendleri(KONU, "tr", istemci=_istemci(kapali))
    assert len(uyari2) == 1 and "ulasilamadi" in uyari2[0]


def test_ifade_trendleri_modelin_seo_kelimelerini_sorar_ve_kisa_ifadeyi_atar():
    sorulan = []

    def isleyici(istek):
        sorulan.append(istek.url.params["q"])
        return httpx.Response(200, json=["q", ["iş akışı otomasyonu araçları"]])

    kelimeler, _ = ifade_trendleri(["iş akışı otomasyonu", "ab", "  "], "tr", istemci=_istemci(isleyici))
    assert "iş akışı otomasyonu" in sorulan and "ab" not in sorulan
    assert kelimeler and kelimeler[0].kelime == "iş akışı otomasyonu araçları"
    assert ifade_trendleri([], "tr") == ([], []) and ifade_trendleri(["ab"], "tr") == ([], [])
    assert len(ifade_sorgu_plani(["iş akışı otomasyonu"])) == 6    # 3 ek x 2 kaynak


def test_ifade_trendleri_ag_kapaliysa_cokmez():
    def kapali(istek):
        raise httpx.ConnectError("kapali")
    kelimeler, uyari = ifade_trendleri(["iş akışı otomasyonu"], "tr", istemci=_istemci(kapali))
    assert kelimeler == [] and uyari


def test_trendleri_birlestir_tekrarsiz_ve_kaynak_toplar():
    a = [TrendKelime("Yapay Zeka Video", ["google"])]
    b = [TrendKelime("yapay zeka  video", ["youtube"]), TrendKelime("baska", ["google"])]
    sonuc = trendleri_birlestir(a, b)
    assert len(sonuc) == 2
    ilk = sonuc[0]
    assert ilk.kaynaklar == ["google", "youtube"]


def test_ikinci_gecis_sonuclari_pakete_girer_ve_hashtagi_dogrular(tmp_path):
    def ifade(ifadeler, dil):
        assert ifadeler == GECERLI["seo"]["anahtar_kelimeler"]     # modelin SEO kelimeleri soruldu
        trend = TrendKelime("yapay zeka otomasyon ozelbirseyxyz rehberi", ["youtube"])
        return [trend], ["ikinci gecis uyarisi"]

    p = paket_olustur("Yapay zeka ile otomasyon", saglayici=SahteSaglayici([GECERLI]), klasor=tmp_path,
                      kapak_ciz=False, trend_getir=sahte_trend, ifade_getir=ifade)
    dogru = {h["etiket"]: h["dogrulandi"] for h in p["hashtagler"]}
    assert dogru["#ozelbirseyxyz"] is True and dogru["#yapayzeka"] is True
    assert any(t["kelime"].endswith("rehberi") for t in p["trendler"])
    assert "ikinci gecis uyarisi" in p["uyarilar"]


# ---------------------------------------------------------------- claude.CMD cok satirli arguman hatasi (regresyon)

def _claude_cagrisini_yakala(monkeypatch):
    """ClaudeCli.uret'in alt sureci nasil cagirdigini yakalar (gercek claude calismaz)."""
    yakalanan = {}

    def sahte_calistir(komut, **kw):
        yakalanan["komut"], yakalanan["kw"] = komut, kw
        return subprocess.CompletedProcess(komut, 0, '{"ok": 1}', "")

    monkeypatch.setattr(sg.shutil, "which", lambda ad: r"C:\Users\x\AppData\Roaming\npm\claude.CMD")
    monkeypatch.setattr(subprocess, "run", sahte_calistir)
    return yakalanan


COK_SATIRLI_SISTEM = 'Satir bir.\nSatir iki: {"anahtar": "deger"} & | < > ^ % talimat\nSatir uc.'


def test_claude_hicbir_talimati_komut_satirindan_gondermez(monkeypatch):
    # cmd.exe cok satirli argumani ilk satirda keser: sistem talimati argv'ye ASLA girmemeli.
    y = _claude_cagrisini_yakala(monkeypatch)
    sg.ClaudeCli().uret(COK_SATIRLI_SISTEM, "ISTEM-METNI")
    for arguman in y["komut"]:
        assert "\n" not in arguman and "\r" not in arguman, f"cok satirli arguman: {arguman!r}"
        assert "Satir iki" not in arguman and "Satir uc" not in arguman
        if arguman != y["komut"][0]:                     # ilki claude.CMD yolu
            assert not any(k in arguman for k in "&|<>^%"), f"cmd.exe ozel karakteri: {arguman!r}"
    assert y["kw"]["input"] == f"{COK_SATIRLI_SISTEM}\n\nISTEM-METNI"   # her sey stdin'den gider


def test_claude_bos_klasorden_ve_izole_baglamla_calisir(monkeypatch, tmp_path):
    y = _claude_cagrisini_yakala(monkeypatch)
    monkeypatch.chdir(tmp_path)
    sg.ClaudeCli().uret("sistem", "istem")
    komut = y["komut"]
    assert y["kw"]["cwd"] and y["kw"]["cwd"] != str(tmp_path)          # kullanicinin klasorunde degil
    assert "--strict-mcp-config" in komut and "--disable-slash-commands" in komut
    assert komut[komut.index("--tools") + 1] == ""                      # arac yok
    assert "--no-session-persistence" in komut


def test_codex_da_talimati_stdin_den_gonderir(monkeypatch):
    y = {}
    monkeypatch.setattr(sg.shutil, "which", lambda ad: "codex.CMD")
    monkeypatch.setattr(subprocess, "run", lambda komut, **kw: y.update(komut=komut, kw=kw) or subprocess.CompletedProcess(komut, 0, "{}", ""))
    sg.CodexCli().uret(COK_SATIRLI_SISTEM, "ISTEM")
    assert all("\n" not in a for a in y["komut"]) and COK_SATIRLI_SISTEM in y["kw"]["input"]


# ---------------------------------------------------------------- akilli kesme ve kalite denetimi

from src.icerik.paket import _kes, kalite_uyarilari


def test_kes_cumle_sinirinda_keser_ve_ortadan_bolmez():
    metin = "Birinci cumle burada biter ve yeterince uzundur. Ikinci cumle epey uzun ve yarim kalacak"
    sonuc = _kes(metin, 70)
    assert sonuc == "Birinci cumle burada biter ve yeterince uzundur." and len(sonuc) <= 70
    # ilk cumle cok kisaysa (esigin altinda) cumle sinirina inmez, kelime sinirinda keser
    assert _kes("Kisa. " + "kelime " * 30, 60).endswith("…")


def test_kes_cumle_yoksa_kelime_sinirinda_ucnokta_ile_keser():
    sonuc = _kes("kelime " * 30, 50)
    assert sonuc.endswith("…") and len(sonuc) <= 51 and "kelim…" not in sonuc


def test_kes_kisa_metne_dokunmaz():
    assert _kes("kisa metin", 50) == "kisa metin"


def test_analiz_alani_artik_cumle_ortasinda_kesilmez():
    veri = json.loads(json.dumps(GECERLI))
    veri["analiz"]["trend_ozeti"] = ("Bu bir cumle. " * 150).strip() + " Son yarim kalan cumle burada"
    n = dogrula_yanit(veri)["analiz"]["trend_ozeti"]
    assert n.endswith(".") and len(n) <= 1200


def _icerik(**ust):
    d = dogrula_yanit(GECERLI)
    d.update(ust)
    return d


def test_kalite_temiz_icerikte_uyari_yok():
    d = _icerik()
    d["hashtagler"] = d["hashtagler"] * 2     # >=5 etiket
    d["senaryo"] = [{"sure_sn": 30, "anlatim": "bir iki", "ekran_yazisi": ""}]
    assert kalite_uyarilari(d) == []


def test_kalite_yan_yana_tekrar_eden_kelimeyi_yakalar():
    d = _icerik(caption='İlk 1000 takipçi takipçi "kasmakla" gelmez.\nSoru?')
    u = kalite_uyarilari(d)
    assert any("takipçi takipçi" in x and "caption" in x for x in u)


def test_kalite_tekrar_yakalamasi_yanlis_alarm_vermez():
    d = _icerik(caption="Bu iş çok güzel. İş ile ilgili işler. Su suyu seviyor.")
    assert not any("yan yana" in x for x in kalite_uyarilari(d))


def test_kalite_uzun_caption_ilk_satiri_ve_baslik_ve_sure_ve_etiket():
    d = _icerik(caption="x" * 140 + "\nkisa", basliklar=["b" * 90])
    d["senaryo"] = [{"sure_sn": 3, "anlatim": "bir", "ekran_yazisi": ""}]
    u = " | ".join(kalite_uyarilari(d))
    assert "140 karakter" in u and "Başlık 1 90 karakter" in u and "3 sn" in u and "Yalnızca 4" in u


def test_kalite_uyarilari_pakete_girer(tmp_path):
    kotu = json.loads(json.dumps(GECERLI))
    kotu["caption"] = "Bu takipçi takipçi bir deneme.\nSoru?"
    p = paket_olustur("Yapay zeka ile otomasyon", saglayici=SahteSaglayici([kotu]), klasor=tmp_path,
                      kapak_ciz=False, trend_getir=sahte_trend, ifade_getir=sahte_ifade)
    assert any("'takipçi takipçi'" in u.lower() for u in p["uyarilar"])
