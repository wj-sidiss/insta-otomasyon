# -*- coding: utf-8 -*-
"""Carousel uretici: dogrulama, HTML kacisi, akis, panel uc noktalari ve gercek cizim."""
from __future__ import annotations

import io
import json
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.icerik import carousel
from src.icerik.anahtarlar import AnahtarDeposu
from src.icerik.carousel import (
    SISTEM, SLAYT_ASGARI, SLAYT_AZAMI, carousel_oku, carousel_olustur, dogrula_carousel, istem_olustur, kalite_uyarilari,
    slayt_html,
)
from src.icerik.panel import uygulama_olustur
from src.icerik.paket import paketi_oku
from src.icerik.saglayici import SaglayiciHatasi
from src.icerik.stok import Onbellek

PAKET_ID = "20261025-120000-test"


def yanit(n: int = 10) -> dict:
    """Gecerli bir model yaniti: kapak + (n-2) icerik/liste + kapanis."""
    slaytlar = [{"tur": "kapak", "baslik": "Kanca basligi", "alt": "Alt cumle"}]
    for i in range(n - 2):
        if i % 3 == 2:
            slaytlar.append({"tur": "liste", "baslik": f"Liste {i}", "maddeler": ["Bir", "Iki", "Uc"]})
        else:
            slaytlar.append({"tur": "icerik", "baslik": f"Fikir {i}", "metin": f"Aciklama {i}."})
    slaytlar.append({"tur": "kapanis", "baslik": "Kapanis", "metin": "Kaydet ve yorum yaz."})
    return {"slaytlar": slaytlar, "caption": "Giris cumlesi.\n\nGovde.\n\nSoru?", "alt_metinler": [f"alt {i}" for i in range(n)]}


class Sahte:
    ad = "sahte"
    aciklama = "Sahte"

    def __init__(self, yanitlar):
        self.yanitlar, self.cagri, self.istemler = list(yanitlar), 0, []

    def hazir_mi(self):
        return True, "hazir"

    def uret(self, sistem, istem, *, zaman_asimi=300):
        self.cagri += 1
        self.istemler.append((sistem, istem))
        y = self.yanitlar.pop(0) if len(self.yanitlar) > 1 else self.yanitlar[0]
        if isinstance(y, Exception):
            raise y
        return y if isinstance(y, str) else json.dumps(y, ensure_ascii=False)


def sahte_ciz(slaytlar, hedef, *, imza="", dil="tr"):
    hedef.mkdir(parents=True, exist_ok=True)
    yollar = []
    for i, _ in enumerate(slaytlar, 1):
        y = hedef / f"slayt-{i:02d}.jpg"
        y.write_bytes(b"\xff\xd8\xff" + f"slayt {i} {imza} {dil}".encode())
        yollar.append(y)
    return yollar


def paket_yaz(klasor: Path, **ek) -> dict:
    hedef = klasor / PAKET_ID
    hedef.mkdir(parents=True)
    paket = {"id": PAKET_ID, "konu": "Yapay zeka ile mesajlara hizli yanit", "dil": "tr", "ton": "samimi", "olusturma": "2026-10-25T12:00:00+00:00",
             "saglayici": "sahte", "basliklar": ["Birinci baslik", "Ikinci"], "secili_baslik": 0, "caption": "c",
             "hashtagler": [{"etiket": "#yapayzeka"}, {"etiket": "#otomasyon"}], "seo": {"anahtar_kelimeler": ["yapay zeka", "otomasyon"]},
             "analiz": {"aci": "pratik", "hedef_kitle": "girisimciler", "hook": "Hala elle mi?"}, "kapak": {}, "trendler": [], "uyarilar": [],
             "medya": {}, "senaryo": [], **ek}
    (hedef / "paket.json").write_text(json.dumps(paket, ensure_ascii=False), encoding="utf-8")
    return paket


# ============================================================ dogrulama

def test_gecerli_yanit_aynen_korunur():
    s = dogrula_carousel(yanit(10))
    assert len(s["slaytlar"]) == 10 and s["slaytlar"][0]["tur"] == "kapak" and s["slaytlar"][-1]["tur"] == "kapanis"
    assert {x["tur"] for x in s["slaytlar"]} == {"kapak", "icerik", "liste", "kapanis"}
    assert s["caption"].startswith("Giris cumlesi.") and len(s["alt_metinler"]) == 10


def test_ilk_slayt_her_zaman_kapak_son_slayt_her_zaman_kapanis():
    v = yanit(6)
    v["slaytlar"][0] = {"tur": "icerik", "baslik": "Ilk", "metin": "Ilk metin"}
    v["slaytlar"][-1] = {"tur": "liste", "baslik": "Son", "maddeler": ["a b", "c d"]}
    s = dogrula_carousel(v)["slaytlar"]
    assert s[0] == {"tur": "kapak", "baslik": "Ilk", "alt": "Ilk metin"}
    assert s[-1]["tur"] == "kapanis" and s[-1]["baslik"] == "Son" and "a b" in s[-1]["metin"]


def test_aradaki_kapak_ve_kapanis_icerige_cevrilir():
    v = yanit(6)
    v["slaytlar"][2] = {"tur": "kapak", "baslik": "Ortada kapak", "alt": "alt metin"}
    v["slaytlar"][3] = {"tur": "kapanis", "baslik": "Ortada kapanis", "metin": "kapanis metni"}
    ara = dogrula_carousel(v)["slaytlar"]
    assert ara[2] == {"tur": "icerik", "baslik": "Ortada kapak", "metin": "alt metin"}
    assert ara[3] == {"tur": "icerik", "baslik": "Ortada kapanis", "metin": "kapanis metni"}


def test_fazla_slayt_ortadan_atilir_gercek_kapanis_korunur():
    v = yanit(12)
    v["slaytlar"][-1]["baslik"] = "GERCEK KAPANIS"
    s = dogrula_carousel(v, 10)["slaytlar"]
    assert len(s) == 10 and s[-1]["baslik"] == "GERCEK KAPANIS" and s[0]["baslik"] == "Kanca basligi"


@pytest.mark.parametrize("istenen,beklenen", [(3, SLAYT_ASGARI), (4, 4), (7, 7), (10, 10), (50, SLAYT_AZAMI), (-5, SLAYT_ASGARI)])
def test_adet_sinirlanir(istenen, beklenen):
    assert len(dogrula_carousel(yanit(10), istenen)["slaytlar"]) == beklenen


@pytest.mark.parametrize("bozucu,mesaj", [
    (lambda v: v.pop("slaytlar"), "slaytlar"), (lambda v: v.update(slaytlar="metin"), "slaytlar"),
    (lambda v: v.update(slaytlar=v["slaytlar"][:3]), "En az 4"), (lambda v: v.update(caption="   "), "caption"),
    (lambda v: v.pop("caption"), "caption"), (lambda v: v.update(slaytlar=[None, "x", 3, []]), "En az 4"),
])
def test_bozuk_yanit_reddedilir(bozucu, mesaj):
    v = yanit(8)
    bozucu(v)
    with pytest.raises(ValueError, match=mesaj):
        dogrula_carousel(v)
    with pytest.raises(ValueError):
        dogrula_carousel([1, 2, 3])


def test_bos_ve_gecersiz_slaytlar_atlanir_liste_kurallari():
    v = yanit(8)
    v["slaytlar"][2] = {"tur": "icerik", "baslik": "", "metin": ""}                                   # bos: atlanir
    v["slaytlar"][3] = {"tur": "liste", "baslik": "Tek maddeli", "maddeler": ["sadece bir"]}          # <2 madde: icerige
    v["slaytlar"][4] = {"tur": "liste", "baslik": "", "maddeler": ["a", "b", "c"]}                    # basliksiz liste: atlanir
    v["slaytlar"][5] = {"tur": "bilinmeyen", "baslik": "Tur yok", "metin": "x"}                       # bilinmeyen tur: icerik
    s = dogrula_carousel(v)["slaytlar"]
    basliklar = [x["baslik"] for x in s]
    assert "Tek maddeli" in basliklar and next(x for x in s if x["baslik"] == "Tek maddeli")["tur"] == "icerik"
    assert "Tur yok" in basliklar and next(x for x in s if x["baslik"] == "Tur yok")["tur"] == "icerik"
    assert all(x["baslik"] for x in s) and len(s) == 6


def test_uzunluklar_kirpilir_cumle_sinirinda():
    v = yanit(6)
    v["slaytlar"][1] = {"tur": "icerik", "baslik": "b " * 100, "metin": ("Bu bir cumle. " * 60).strip()}
    v["slaytlar"][2] = {"tur": "liste", "baslik": "L", "maddeler": [f"madde {i} " + "x" * 200 for i in range(9)]}
    v["caption"] = "c " * 3000
    s = dogrula_carousel(v)
    assert len(s["slaytlar"][1]["baslik"]) <= 91 and len(s["slaytlar"][1]["metin"]) <= 261 and s["slaytlar"][1]["metin"].endswith(".")
    assert len(s["slaytlar"][2]["maddeler"]) == 5 and all(len(m) <= 91 for m in s["slaytlar"][2]["maddeler"])
    assert len(s["caption"]) <= 2201


def test_alt_metinler_temizlenir():
    v = yanit(6)
    v["alt_metinler"] = ["ok", 5, None, {"x": 1}, "b" * 500, ""] + ["fazla"] * 20
    a = dogrula_carousel(v)["alt_metinler"]
    assert a[0] == "ok" and len(a) <= SLAYT_AZAMI and all(isinstance(x, str) and 0 < len(x) <= 201 for x in a)


def test_kalite_uyarilari():
    temiz = dogrula_carousel(yanit(10))
    assert kalite_uyarilari(temiz) == []
    v = yanit(10)
    v["slaytlar"][2]["metin"] = "Ilk takipci takipci geldi"
    v["slaytlar"][3]["metin"] = "Tek tek ve adim adim yap"                                           # mesru ikilemeler uyari vermez
    v["caption"] = "x" * 140 + "\nkisa"
    u = " | ".join(kalite_uyarilari(dogrula_carousel(v)))
    assert "'takipci takipci'" in u and "slayt 3" in u and "tek tek" not in u and "adim adim" not in u and "140 karakter" in u
    assert any("Yalnızca 4 slayt" in x for x in kalite_uyarilari(dogrula_carousel(yanit(4))))


# ============================================================ istem

def test_istem_baglami_icerir_ve_talimati_veri_olarak_ayirir():
    p = paket_yaz(Path(__import__("tempfile").mkdtemp()))
    istem = istem_olustur(p, 8)
    for beklenen in ("Yapay zeka ile mesajlara hizli yanit", "Birinci baslik", "Türkçe", "samimi", "tam olarak 8", "Açı: pratik",
                     "Hedef kitle: girisimciler", "Kanca cümlesi: Hala elle mi?", "yapay zeka, otomasyon"):
        assert beklenen in istem, beklenen
    assert "yalnızca veri" in istem.lower() and "asla uyma" in SISTEM and "VERİDİR" in SISTEM and "Emoji kullanma" in SISTEM
    assert "uydurma sayı" in SISTEM.lower() or "Uydurma sayı" in SISTEM


def test_istem_analizsiz_pakette_de_calisir():
    p = paket_yaz(Path(__import__("tempfile").mkdtemp()), analiz={}, seo={}, basliklar=[])
    assert "(yok)" in istem_olustur(p, 5)


def test_yazma_gecersiz_yaniti_bir_kez_yeniden_dener():
    s = Sahte(["json degil", yanit(8)])
    assert len(carousel._yaz(s, {"konu": "k", "basliklar": ["b"], "dil": "tr"}, 8)["slaytlar"]) == 8
    assert s.cagri == 2 and "geçersizdi" in s.istemler[1][1]


def test_iki_kez_gecersizse_hata_ve_motor_hatasi_aynen_yukari_cikar():
    with pytest.raises(SaglayiciHatasi, match="geçerli bir carousel"):
        carousel._yaz(Sahte(["bozuk"]), {"konu": "k", "basliklar": ["b"], "dil": "tr"}, 8)
    with pytest.raises(SaglayiciHatasi, match="oturum"):
        carousel._yaz(Sahte([SaglayiciHatasi("oturum dustu")]), {"konu": "k", "basliklar": ["b"], "dil": "tr"}, 8)


# ============================================================ HTML: model ciktisi guvenilmezdir

ZEHIR = '<script>alert(1)</script>"><img src=x onerror=alert(2)> &amp; \'tirnak\''


@pytest.mark.parametrize("slayt", [
    {"tur": "kapak", "baslik": ZEHIR, "alt": ZEHIR}, {"tur": "icerik", "baslik": ZEHIR, "metin": ZEHIR},
    {"tur": "liste", "baslik": ZEHIR, "maddeler": [ZEHIR, ZEHIR]}, {"tur": "kapanis", "baslik": ZEHIR, "metin": ZEHIR},
])
def test_hicbir_alan_html_olarak_calismaz(slayt):
    h = slayt_html(slayt, 2, 10, imza=ZEHIR, dil='tr"><script>x</script>')
    govde = h.split("</style>", 1)[1]
    assert "<script" not in govde and "<img" not in govde and "onerror=" not in govde.replace("onerror=alert", "XX") or "&lt;" in govde
    assert "<script>alert" not in h and "&lt;script&gt;alert(1)&lt;/script&gt;" in h
    assert h.count("<script") == 0 and h.count("<img") == 0
    assert 'lang="tr&quot;&gt;' in h                                                                      # dil alani da kacirilir


def test_yer_tutucu_enjeksiyonu_ve_satir_sonu():
    h = slayt_html({"tur": "icerik", "baslik": "A\nB", "metin": "__IMZA__ {x} %s"}, 3, 10, imza="__UST__")
    assert "A<br>B" in h and "__IMZA__ {x} %s" in h and "__UST__" in h


def test_numarali_rozet_yok_ve_son_slaytta_tek_cagri():
    ic = slayt_html({"tur": "icerik", "baslik": "Adim 1", "metin": "x"}, 3, 10)
    assert "rozet" not in ic and 'class="cizgi"' in ic and "Kaydır →" in ic and "03 / 10" in ic
    son = slayt_html({"tur": "kapanis", "baslik": "Bitti", "metin": "x"}, 10, 10)
    assert son.count("Kaydet · Paylaş") == 1 and "Kaydır" not in son and "10 / 10" in son
    assert "Swipe →" in slayt_html({"tur": "kapak", "baslik": "x"}, 1, 5, dil="en") and "Save · Share" in slayt_html({"tur": "kapanis", "baslik": "x"}, 5, 5, dil="en")
    assert "Kaydır" in slayt_html({"tur": "kapak", "baslik": "x"}, 1, 5, dil="fr")                           # bilinmeyen dil: Turkce


def test_tur_basina_yapi():
    assert slayt_html({"tur": "liste", "baslik": "L", "maddeler": ["a", "b", "c"]}, 4, 10).count('class="satir"') == 3
    assert "cagri" in slayt_html({"tur": "kapanis", "baslik": "B"}, 10, 10) and "<p" not in slayt_html({"tur": "kapak", "baslik": "B"}, 1, 10)


@pytest.mark.parametrize("metin,beklenen", [("a" * 10, 112), ("a" * 30, 112), ("a" * 31, 92), ("a" * 50, 92), ("a" * 75, 76), ("a" * 76, 64)])
def test_baslik_boyutu_uzunluga_gore_kuculur(metin, beklenen):
    assert f"font-size:{beklenen}px" in slayt_html({"tur": "kapak", "baslik": metin}, 1, 10)


# ============================================================ akis

def test_olustur_dosyalari_ve_ozeti_yazar(tmp_path):
    klasor = tmp_path / "paketler"
    p = paket_yaz(klasor)
    adimlar = []
    s = carousel_olustur(p, saglayici=Sahte([yanit(8)]), klasor=klasor, adet=8, imza="@x", ciz=sahte_ciz, ilerleme=lambda a, m: adimlar.append(a))
    assert adimlar == ["yazi", "ciz", "kayit"] and s["dosyalar"] == [f"slayt-{i:02d}.jpg" for i in range(1, 9)]
    assert (klasor / PAKET_ID / "carousel" / "slayt-01.jpg").read_bytes().endswith(b"slayt 1 @x tr")
    kayit = carousel_oku(klasor, PAKET_ID)
    assert kayit["caption"] == s["caption"] and len(kayit["slaytlar"]) == 8 and kayit["olusturma"]
    assert paketi_oku(klasor, PAKET_ID)["carousel"] == {"slayt_sayisi": 8, "olusturma": kayit["olusturma"], "cizildi": True}
    assert not list((klasor / PAKET_ID / "carousel").glob("*.tmp"))


def test_yeniden_uretimde_eski_slaytlar_kalmaz(tmp_path):
    klasor = tmp_path / "paketler"
    p = paket_yaz(klasor)
    carousel_olustur(p, saglayici=Sahte([yanit(10)]), klasor=klasor, ciz=sahte_ciz)
    carousel_olustur(p, saglayici=Sahte([yanit(5)]), klasor=klasor, adet=5, ciz=sahte_ciz)
    assert sorted(x.name for x in (klasor / PAKET_ID / "carousel").glob("slayt-*.jpg")) == [f"slayt-{i:02d}.jpg" for i in range(1, 6)]


def test_cizim_hatasi_uyari_olur_metin_kaybolmaz(tmp_path):
    klasor = tmp_path / "paketler"
    p = paket_yaz(klasor)

    def patlat(*a, **k):
        raise RuntimeError("chromium yok")
    s = carousel_olustur(p, saglayici=Sahte([yanit(6)]), klasor=klasor, adet=6, ciz=patlat)
    assert s["dosyalar"] == [] and any("chromium" in u for u in s["uyarilar"])
    assert carousel_oku(klasor, PAKET_ID)["caption"] and paketi_oku(klasor, PAKET_ID)["carousel"]["cizildi"] is False


@pytest.mark.parametrize("kimlik", ["../x", "", "20261025-120000-X", "a/b"])
def test_gecersiz_paket_kimligi_reddedilir(tmp_path, kimlik):
    with pytest.raises(ValueError):
        carousel_olustur({"id": kimlik, "konu": "k", "basliklar": ["b"]}, saglayici=Sahte([yanit()]), klasor=tmp_path, ciz=sahte_ciz)
    assert carousel_oku(tmp_path, kimlik) is None


def test_bozuk_carousel_json_none_doner(tmp_path):
    (tmp_path / PAKET_ID / "carousel").mkdir(parents=True)
    (tmp_path / PAKET_ID / "carousel" / "carousel.json").write_text("{bozuk", encoding="utf-8")
    assert carousel_oku(tmp_path, PAKET_ID) is None


# ============================================================ gercek cizim (chromium yoksa atlanir)

def test_gercek_tarayici_cizimi_dort_turu_de_1080x1350_jpeg_uretir(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    slaytlar = [
        {"tur": "kapak", "baslik": "Cok uzun bir kapak basligi " * 3, "alt": "Alt cumle"},
        {"tur": "icerik", "baslik": ZEHIR, "metin": ZEHIR * 3},
        {"tur": "liste", "baslik": "Liste", "maddeler": ["Madde " * 20, "Iki", "Uc", "Dort", "Bes"]},
        {"tur": "kapanis", "baslik": "Kapanis", "metin": "Kaydet"},
    ]
    try:
        yollar = carousel.slaytlari_ciz(slaytlar, tmp_path, imza="@x")
    except RuntimeError as hata:
        pytest.skip(f"chromium yok: {hata}")
    assert [y.name for y in yollar] == [f"slayt-0{i}.jpg" for i in range(1, 5)]
    for y in yollar:
        im = PIL.open(y)
        assert im.size == (1080, 1350) and im.format == "JPEG", y.name


# ============================================================ panel

@pytest.fixture
def panel(tmp_path):
    klasor = tmp_path / "paketler"
    klasor.mkdir()
    paket_yaz(klasor)
    saglayici = Sahte([yanit(10)])
    app = uygulama_olustur(
        paket_klasoru=klasor, saglayicilar=lambda: [saglayici], saglayici_bulucu=lambda ad: saglayici, izinli_hostlar=("testserver",),
        kapak_ciz=False, anahtar_depo=AnahtarDeposu(tmp_path / "k.json", ortam={}), stok_onbellek=Onbellek(), carousel_ciz=sahte_ciz, imza="@wj",
    )
    c = TestClient(app)
    c.klasor, c.saglayici = klasor, saglayici
    return c


def _bekle(c, is_id):
    import time
    bitis = time.time() + 10
    while time.time() < bitis:
        d = c.get(f"/api/is/{is_id}").json()
        if d["durum"] != "calisiyor":
            return d
        time.sleep(0.03)
    raise AssertionError("is bitmedi")


def _uret(c, **govde):
    r = c.post(f"/api/paket/{PAKET_ID}/carousel", json=govde)
    assert r.status_code == 202, r.text
    d = _bekle(c, r.json()["is_id"])
    assert d["durum"] == "bitti", d
    return d


def test_panel_uctan_uca_uretim_slayt_ve_zip(panel):
    assert panel.get(f"/api/paket/{PAKET_ID}/carousel").status_code == 404                  # henuz yok
    assert panel.get(f"/paketler/{PAKET_ID}/carousel.zip").status_code == 404
    d = _uret(panel, adet=10)
    assert [a["adim"] for a in d["adimlar"]] == ["yazi", "ciz", "kayit"] and d["paket_id"] == PAKET_ID
    c = panel.get(f"/api/paket/{PAKET_ID}/carousel").json()
    assert len(c["slaytlar"]) == 10 and len(c["dosyalar"]) == 10 and c["caption"].startswith("Giris cumlesi")
    assert panel.get(f"/api/paket/{PAKET_ID}").json()["carousel"]["slayt_sayisi"] == 10
    s = panel.get(f"/paketler/{PAKET_ID}/carousel/slayt-03.jpg")
    assert s.status_code == 200 and s.headers["content-type"] == "image/jpeg" and s.content.endswith(b"slayt 3 @wj tr")
    z = panel.get(f"/paketler/{PAKET_ID}/carousel.zip")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip" and "carousel-" + PAKET_ID in z.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert sorted(zf.namelist()) == sorted([f"slayt-{i:02d}.jpg" for i in range(1, 11)] + ["aciklama.txt"])
        aciklama = zf.read("aciklama.txt").decode("utf-8")
    assert aciklama.startswith("Giris cumlesi.") and "#yapayzeka #otomasyon" in aciklama and aciklama.endswith("\n")


def test_panel_adet_ve_saglayici_dogrulamasi(panel):
    for govde in ({"adet": 3}, {"adet": 11}, {"adet": "x"}, {"adet": None}, {"saglayici": "x" * 50}):
        assert panel.post(f"/api/paket/{PAKET_ID}/carousel", json=govde).status_code == 422, govde
    assert panel.post("/api/paket/20261025-999999-yok/carousel", json={}).status_code == 404
    assert panel.post("/api/paket/..%2Fx/carousel", json={}).status_code in (404, 422)
    assert panel.saglayici.cagri == 0                                                        # gecersiz istek modele gitmedi


def test_panel_varsayilan_adet_10(panel):
    _uret(panel)
    assert "tam olarak 10" in panel.saglayici.istemler[0][1]
    _uret(panel, adet=6)
    assert "tam olarak 6" in panel.saglayici.istemler[1][1]


def test_panel_motor_hatasi_kullaniciya_ulasir(tmp_path):
    klasor = tmp_path / "paketler"
    klasor.mkdir()
    paket_yaz(klasor)
    s = Sahte([SaglayiciHatasi("Oturum süresi dolmuş.")])
    app = uygulama_olustur(paket_klasoru=klasor, saglayicilar=lambda: [s], saglayici_bulucu=lambda ad: s, izinli_hostlar=("testserver",),
                           carousel_ciz=sahte_ciz, anahtar_depo=AnahtarDeposu(tmp_path / "k.json", ortam={}))
    c = TestClient(app)
    d = _bekle(c, c.post(f"/api/paket/{PAKET_ID}/carousel", json={}).json()["is_id"])
    assert d["durum"] == "hata" and d["hata"] == "Oturum süresi dolmuş."
    assert c.get(f"/api/paket/{PAKET_ID}/carousel").status_code == 404


def test_panel_ayni_anda_ikinci_uretim_reddedilir(tmp_path):
    kapi = threading.Event()

    class Yavas(Sahte):
        def uret(self, *a, **k):
            kapi.wait(5)
            return super().uret(*a, **k)

    klasor = tmp_path / "paketler"
    klasor.mkdir()
    paket_yaz(klasor)
    s = Yavas([yanit(10)])
    app = uygulama_olustur(paket_klasoru=klasor, saglayicilar=lambda: [s], saglayici_bulucu=lambda ad: s, izinli_hostlar=("testserver",),
                           carousel_ciz=sahte_ciz, anahtar_depo=AnahtarDeposu(tmp_path / "k.json", ortam={}))
    c = TestClient(app)
    ilk = c.post(f"/api/paket/{PAKET_ID}/carousel", json={})
    assert ilk.status_code == 202 and c.post(f"/api/paket/{PAKET_ID}/carousel", json={}).status_code == 409
    assert c.post("/api/uret", json={"konu": "baska uretim"}).status_code == 409                # paket uretimiyle de kilitli
    kapi.set()
    assert _bekle(c, ilk.json()["is_id"])["durum"] == "bitti"


@pytest.mark.parametrize("dosya", ["slayt-1.jpg", "slayt-001.jpg", "slayt-01.png", "carousel.json", "..%2F..%2Fpaket.json", "slayt-01.jpg.exe",
                                   "SLAYT-01.jpg", "slayt-aa.jpg", "%2e%2e/paket.json", "slayt-01.jpg%00.png"])
def test_panel_slayt_dosya_adi_deseni_ve_yol_gezintisi(panel, dosya):
    _uret(panel, adet=5)
    (panel.klasor.parent / "paket.json").write_text("GIZLI", encoding="utf-8")
    r = panel.get(f"/paketler/{PAKET_ID}/carousel/{dosya}")
    assert r.status_code in (400, 404) and b"GIZLI" not in r.content and b'"konu"' not in r.content


def test_panel_slayt_ve_zip_icin_gecersiz_paket_kimligi(panel):
    for kimlik in ("..", "..%2Fx", "20261025-120000-X", "a%2Fb"):
        assert panel.get(f"/paketler/{kimlik}/carousel/slayt-01.jpg").status_code in (400, 404)
        assert panel.get(f"/paketler/{kimlik}/carousel.zip").status_code in (400, 404)


def test_panel_zip_yalnizca_desene_uyan_dosyalari_icerir(panel):
    _uret(panel, adet=5)
    yol = panel.klasor / PAKET_ID / "carousel"
    (yol / "gizli.txt").write_text("SIZMAMALI", encoding="utf-8")
    kayit = json.loads((yol / "carousel.json").read_text(encoding="utf-8"))
    kayit["dosyalar"] += ["gizli.txt", "../../paket.json", "..\\kotu.jpg"]                  # bozulmus/zehirli kayit
    (yol / "carousel.json").write_text(json.dumps(kayit), encoding="utf-8")
    with zipfile.ZipFile(io.BytesIO(panel.get(f"/paketler/{PAKET_ID}/carousel.zip").content)) as zf:
        assert sorted(zf.namelist()) == sorted([f"slayt-0{i}.jpg" for i in range(1, 6)] + ["aciklama.txt"])
        assert b"SIZMAMALI" not in b"".join(zf.read(n) for n in zf.namelist())


def test_panel_yabanci_site_carousel_isteklerini_yapamaz(panel):
    _uret(panel, adet=5)
    assert panel.post(f"/api/paket/{PAKET_ID}/carousel", json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert panel.post(f"/api/paket/{PAKET_ID}/carousel", json={}, headers={"Origin": "https://kotu.example"}).status_code == 403
    assert panel.post(f"/api/paket/{PAKET_ID}/carousel", content="adet=5", headers={"Content-Type": "text/plain"}).status_code == 415
    for yol in (f"/api/paket/{PAKET_ID}/carousel", f"/paketler/{PAKET_ID}/carousel/slayt-01.jpg", f"/paketler/{PAKET_ID}/carousel.zip"):
        assert panel.get(yol, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403, yol
        assert panel.get(yol, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200, yol


def test_panel_paket_silinince_carousel_de_gider(panel):
    _uret(panel, adet=5)
    assert panel.delete(f"/api/paket/{PAKET_ID}").status_code == 200
    assert panel.get(f"/api/paket/{PAKET_ID}/carousel").status_code == 404 and not (panel.klasor / PAKET_ID).exists()
