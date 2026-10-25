# -*- coding: utf-8 -*-
"""Anahtarsiz acik kaynak gorsel kaynaklari, lisans suzgeci, sogutucu, yedek URL'li indirme, User-Agent ve panel entegrasyonu.

Fiksturler (testler/fikstur/*.json) 2026-10-25'te GERCEK API yanitlarindan alinmistir; uydurma yanit sekli yoktur.
"""
from __future__ import annotations

import copy
import json
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.icerik import acik_kaynaklar as acik
from src.icerik import stok
from src.icerik.acik_kaynaklar import Sogutucu, muze_sorgu
from src.icerik.anahtarlar import AnahtarDeposu
from src.icerik.lisans import atif_metni, lisans_coz, wikimedia_lisans
from src.icerik.panel import uygulama_olustur
from src.icerik.stok import Aday, Onbellek, StokHatasi, bekleme_suresi, kullanici_ajani

FIKSTUR = Path(__file__).resolve().parent / "fikstur"
PAKET_ID = "20261025-120000-test"


def yukle(ad: str):
    return json.loads((FIKSTUR / ad).read_text(encoding="utf-8"))


class Sahte:
    """(host, yol) -> yanit tablosu; her istegi kaydeder. Yol '*' ile bitiyorsa on ek eslesir."""

    def __init__(self, yollar: dict):
        self.yollar, self.istekler = yollar, []

    def __call__(self, istek: httpx.Request) -> httpx.Response:
        self.istekler.append(istek)
        for (host, yol), yanit in self.yollar.items():
            if istek.url.host == host and (istek.url.path == yol or (yol.endswith("*") and istek.url.path.startswith(yol[:-1]))):
                return yanit(istek) if callable(yanit) else httpx.Response(200, json=yanit)
        return httpx.Response(404)

    def istemci(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def aranan(kaynak, sahte, sorgu="sunflower", adet=6, **kw):
    return stok.ara(kaynak, "gorsel", sorgu, None, adet=adet, istemci=sahte.istemci(), onbellek=Onbellek(), **kw)


# ============================================================ lisans

@pytest.mark.parametrize("ad,kod,zorunlu", [
    ("CC BY-SA 4.0", "by-sa", True), ("CC BY-SA 3.0", "by-sa", True), ("CC BY 2.0", "by", True), ("CC BY 4.0", "by", True),
    ("Attribution", "by", True), ("CC0", "cc0", False), ("CC0 1.0", "cc0", False), ("CC Zero", "cc0", False),
    ("Public domain", "pdm", False), ("PD-old-70", "pdm", False), ("PD-USGov-NASA", "pdm", False), ("No known copyright restrictions", "pdm", False),
    ("Share Alike", "by-sa", True),
])
def test_wikimedia_serbest_lisanslar_dogru_siniflanir(ad, kod, zorunlu):
    l = wikimedia_lisans(ad)
    assert l.kabul and l.kod == kod and l.atif_gerekli is zorunlu, ad


@pytest.mark.parametrize("ad,neden", [
    ("CC BY-NC 2.0", "NC"), ("CC BY-NC-SA 4.0", "NC"), ("CC BY-ND 3.0", "ND"), ("CC BY-NC-ND 4.0", "NC"),
    ("Non-commercial", "NC"), ("No derivs", "ND"), ("GFDL 1.2", "GFDL"),
])
def test_wikimedia_ticari_olmayan_ve_turetilemez_lisanslar_elenir(ad, neden):
    l = wikimedia_lisans(ad)
    assert not l.kabul and neden in l.neden, ad


def test_wikimedia_non_free_bayragi_ve_taninmayan_lisans():
    assert not wikimedia_lisans("CC BY 4.0", "true").kabul and not wikimedia_lisans("Public domain", " TRUE ").kabul
    tanimsiz = wikimedia_lisans("Ozel Lisans X")
    assert tanimsiz.kabul and tanimsiz.kod == "bilinmiyor" and tanimsiz.atif_gerekli and tanimsiz.sira == 3 and "tanınamadı" in tanimsiz.uyari
    assert wikimedia_lisans("").kabul and wikimedia_lisans("").atif_gerekli


def test_by_sa_uyarisi_ve_siralama():
    sa, by, cc0 = wikimedia_lisans("CC BY-SA 4.0"), wikimedia_lisans("CC BY 4.0"), wikimedia_lisans("CC0")
    assert "türev eser" in sa.uyari and by.uyari == "" and cc0.uyari == ""
    assert cc0.sira < by.sira < sa.sira
    assert sa.lisans == "CC BY-SA 4.0" and by.lisans == "CC BY 4.0" and cc0.lisans == "CC0 1.0"


def test_sabit_kaynak_lisanslari_ve_bilinmeyen_kaynak():
    assert lisans_coz("pexels").atif_onerilen and not lisans_coz("pexels").atif_gerekli
    assert lisans_coz("pixabay").kabul and not lisans_coz("pixabay").atif_onerilen
    assert all(lisans_coz(k).kod == "cc0" for k in ("met", "aic", "cleveland"))
    assert lisans_coz("nasa").kod == "nasa" and not lisans_coz("flickr").kabul


def test_atif_metni_bicimi_ve_temizlik():
    assert atif_metni("Gün Batımı", "Ada", "CC BY 4.0", "https://x.example/f") == '"Gün Batımı" — Ada — CC BY 4.0 — https://x.example/f'
    assert atif_metni('Ali "Baba"', "", "CC0", "u") == "\"Ali 'Baba'\" — Bilinmeyen — CC0 — u"
    assert atif_metni("", "x", "y", "z").startswith('"Başlıksız"') and len(atif_metni("b" * 500, "y" * 500, "l", "u")) < 700


# ============================================================ baslik suzgecleri ve muze sorgusu

def test_muze_sorgusu_dolgu_kelimeleri_atar_ve_uc_kelimeyle_sinirlar():
    assert muze_sorgu("Rembrandt Night Watch painting") == "Rembrandt Night Watch"
    assert muze_sorgu("oil painting of sunflowers in the vase") == "oil sunflowers vase"
    assert muze_sorgu("the a of") == "the a of"                         # hepsi dolgu ise orijinal korunur
    assert muze_sorgu("a b c d e f") == "b c d"                          # "a" bir dolgu kelimesidir
    assert muze_sorgu("x y z w") == "x y z"


@pytest.mark.parametrize("baslik,kati", [
    ("Company logo", True), ("World map of rainfall", True), ("Flow chart diagram", True), ("Screenshot of app", True),
    ("Chartered accountants", False), ("Charter school building", False), ("Mapleton sunset", False), ("Sunflower field", False),
    ("Haritalar atlasi", True), ("Sunflower SVG vector", True),
])
def test_kati_kara_liste_kelime_sinirinda_calisir(baslik, kati):
    assert bool(acik.KATI.search(baslik)) is kati, baslik


# ============================================================ Wikimedia

def _wiki_sahte(degistir=None):
    d = yukle("wikimedia_arama.json")
    if degistir:
        degistir(d["query"]["pages"])
    return Sahte({("commons.wikimedia.org", "/w/api.php"): d})


def test_wikimedia_istegi_ve_gercek_yanit_ayristirma():
    s = _wiki_sahte()
    adaylar = aranan("wikimedia", s, "sunflower field")
    i = s.istekler[0]
    p = i.url.params
    assert p["gsrsearch"] == "sunflower field filetype:bitmap" and p["gsrnamespace"] == "6" and p["iiurlwidth"] == "1280"
    assert "iiurlheight" not in p and p["format"] == "json" and p["formatversion"] == "2" and p["prop"] == "imageinfo"
    ua = i.headers["user-agent"]
    assert ua.startswith("IcerikStudyosu/") and "https://" in ua and "python" not in ua.lower()      # Wikimedia: iletisim SART; AIC: kutuphane adi YASAK
    assert len(adaylar) == 4 and all(a.kaynak == "wikimedia" and a.tur == "gorsel" for a in adaylar)
    assert [a.dikey for a in adaylar] == [True, False, False, False]                                  # dikey kadraj once
    ilk = adaylar[0]
    assert ilk.baslik == "Sunflower field Milky Way" and ilk.genislik == 1280 and ilk.yukseklik == 1917
    assert ilk.lisans == "CC BY-SA 4.0" and ilk.lisans_kodu == "by-sa" and ilk.atif_gerekli and "türev eser" in ilk.uyari
    assert ilk.indirme.startswith("https://thumb.wikimedia.org/") and ilk.sayfa.startswith("https://commons.wikimedia.org/wiki/File:")
    assert "<" not in ilk.kredi and "&" not in ilk.kredi and ilk.kredi_adresi == ilk.sayfa             # HTML temizlendi
    assert ilk.yedek and ilk.yedek[0] != ilk.indirme and ilk.yedek[0].startswith("https://upload.wikimedia.org/")   # orijinal dosya yedek
    assert stok.atif_satiri(ilk).startswith('"Sunflower field Milky Way" — ') and "CC BY-SA 4.0" in stok.atif_satiri(ilk)


def test_wikimedia_adet_siniri():
    assert len(aranan("wikimedia", _wiki_sahte(), adet=2)) == 2


def test_wikimedia_nc_nd_ve_non_free_elenir():
    def degistir(sayfalar):
        sayfalar[0]["imageinfo"][0]["extmetadata"]["LicenseShortName"]["value"] = "CC BY-NC 2.0"
        sayfalar[1]["imageinfo"][0]["extmetadata"]["LicenseShortName"]["value"] = "CC BY-ND 4.0"
        sayfalar[2]["imageinfo"][0]["extmetadata"]["NonFree"] = {"value": "true"}
    adaylar = aranan("wikimedia", _wiki_sahte(degistir))
    assert len(adaylar) == 1 and adaylar[0].lisans == "CC BY 2.0"


def test_wikimedia_logo_harita_ve_svg_gif_elenir_zayif_basliklar_sona_kalir():
    def degistir(sayfalar):
        sayfalar[0]["title"] = "File:Company logo.png"
        sayfalar[1]["imageinfo"][0]["mime"] = "image/svg+xml"
        sayfalar[2]["imageinfo"][0]["mime"] = "image/gif"
        sayfalar[3]["title"] = "File:Sunflower painting.jpg"
        sayfalar.append(copy.deepcopy(sayfalar[3]))
        sayfalar[-1]["title"] = "File:Sunflower real photo.jpg"
        sayfalar[-1]["index"] = 99
        sayfalar[-1]["pageid"] = 424242
    adaylar = aranan("wikimedia", _wiki_sahte(degistir))
    assert [a.baslik for a in adaylar] == ["Sunflower real photo", "Sunflower painting"]               # 'painting' yumusak: sona


def test_wikimedia_taninmayan_lisans_kabul_edilir_ama_atif_ister_ve_sona_kalir():
    def degistir(sayfalar):
        sayfalar[1]["imageinfo"][0]["extmetadata"]["LicenseShortName"]["value"] = "Ozel Serbest Lisans"
    adaylar = aranan("wikimedia", _wiki_sahte(degistir))
    ozel = next(a for a in adaylar if a.lisans == "Ozel Serbest Lisans")
    assert ozel.atif_gerekli and "tanınamadı" in ozel.uyari and ozel.lisans_kodu == "bilinmiyor"


@pytest.mark.parametrize("yanit", [{}, {"query": {}}, {"query": {"pages": []}}, {"batchcomplete": True},
                                   {"query": {"pages": [{"title": "File:x.jpg"}, "bozuk", {"imageinfo": []}]}}])
def test_wikimedia_bos_ya_da_bozuk_yanit_cokmez(yanit):
    assert aranan("wikimedia", Sahte({("commons.wikimedia.org", "/w/api.php"): yanit})) == []


def test_wikimedia_sayfalar_sozluk_olarak_gelirse_de_calisir():
    d = yukle("wikimedia_arama.json")
    d["query"]["pages"] = {str(p["pageid"]): p for p in d["query"]["pages"]}
    assert len(aranan("wikimedia", Sahte({("commons.wikimedia.org", "/w/api.php"): d}))) == 4


# ============================================================ NASA

def _nasa_sahte(ek=None):
    d = yukle("nasa_arama.json")
    sablon = d["collection"]["items"][0]
    for baslik, aciklama, nid in (ek or []):
        it = copy.deepcopy(sablon)
        it["data"][0].update({"title": baslik, "description": aciklama, "nasa_id": nid})
        d["collection"]["items"].append(it)
    return Sahte({("images-api.nasa.gov", "/search"): d})


def test_nasa_istegi_gercek_yanit_ve_yedek_adresler():
    s = _nasa_sahte()
    adaylar = aranan("nasa", s, "moon surface")
    p = s.istekler[0].url.params
    assert p["q"] == "moon surface" and p["media_type"] == "image" and int(p["page_size"]) <= 30
    assert len(adaylar) == 4 and all(a.lisans == "NASA (kamu malı)" and a.dikey is None for a in adaylar)
    ilk = adaylar[0]
    assert ilk.indirme.endswith("as11-37-5545~orig.jpg")
    assert [y.rsplit("~", 1)[1] for y in ilk.yedek] == ["large.jpg", "medium.jpg"]                   # orig -> large -> medium
    assert ilk.onizleme.endswith("~medium.jpg") and ilk.sayfa == "https://images.nasa.gov/details/as11-37-5545"
    assert ilk.kredi == "NASA" and ilk.atif_onerilen and not ilk.atif_gerekli


def test_nasa_telifli_ve_kara_listedeki_basliklar_elenir_yumusaklar_sona_kalir():
    s = _nasa_sahte([
        ("Great Moon photo", "Image courtesy of a partner. © 2020 Example Corp", "telifli-1"),
        ("Moon COPYRIGHT shot", "All rights reserved, copyright Example", "telifli-2"),
        ("Rocket test stand diagram", "A diagram", "kara-1"),
        ("Employees at the awards ceremony", "People", "kara-2"),
        ("Artist's illustration of a lunar base", "An illustration", "yumusak-1"),
        ("Lunar crater closeup", "Real photo", "temiz-1"),
    ])
    basliklar = [a.baslik for a in aranan("nasa", s, adet=20)]
    for elenen in ("Great Moon photo", "Moon COPYRIGHT shot", "Rocket test stand diagram", "Employees at the awards ceremony"):
        assert elenen not in basliklar
    assert basliklar[-1] == "Artist's illustration of a lunar base" and "Lunar crater closeup" in basliklar


@pytest.mark.parametrize("oge", [{}, {"data": [], "links": []}, {"data": [{"title": "x"}], "links": [{}]}, "bozuk", None,
                                 {"data": [{"title": "x"}], "links": [{"href": ""}]}])
def test_nasa_bozuk_ogeler_atlanir(oge):
    d = {"collection": {"items": [oge]}}
    assert aranan("nasa", Sahte({("images-api.nasa.gov", "/search"): d})) == []


def test_nasa_href_boyut_eki_yoksa_tek_adres():
    d = yukle("nasa_arama.json")
    d["collection"]["items"][0]["links"][0]["href"] = "https://images-assets.nasa.gov/image/x/x.jpg"
    (a, *_) = aranan("nasa", Sahte({("images-api.nasa.gov", "/search"): d}))
    assert a.indirme.endswith("/x.jpg") and a.yedek == ()


# ============================================================ The Met

def _met_sahte(durumlar=None):
    arama, nesne = yukle("met_arama.json"), yukle("met_nesne.json")
    ids = arama["objectIDs"]
    durumlar = durumlar or {}

    def nesne_yaniti(istek):
        oid = int(istek.url.path.rsplit("/", 1)[1])
        if oid == ids[0]:
            return httpx.Response(200, json=nesne)
        if durumlar.get(oid) == "hata":
            return httpx.Response(500)
        return httpx.Response(200, json={**nesne, "objectID": oid, "isPublicDomain": durumlar.get(oid, False), "title": f"Eser {oid}"})

    return Sahte({("collectionapi.metmuseum.org", "/public/collection/v1.1/search"): arama,
                  ("collectionapi.metmuseum.org", "/public/collection/v1/objects/*"): nesne_yaniti}), ids


def test_met_v1_1_arama_ve_nesne_uclari_ile_gercek_yanit():
    s, ids = _met_sahte({})
    adaylar = aranan("met", s, "oil painting of sunflowers in the vase", adet=3)
    ilk = s.istekler[0]
    assert ilk.url.path == "/public/collection/v1.1/search"                       # eski v1/search 410 Gone
    assert dict(ilk.url.params) == {"hasImages": "true", "isPublicDomain": "true", "q": "oil sunflowers vase"}
    assert [i.url.path for i in s.istekler[1:]] == [f"/public/collection/v1/objects/{o}" for o in ids[:3]]
    assert len(adaylar) == 1 and adaylar[0].baslik == "Sunflowers" and adaylar[0].lisans == "CC0 (The Met Open Access)"
    assert adaylar[0].kredi.endswith("The Metropolitan Museum of Art") and adaylar[0].sayfa.startswith("https://www.metmuseum.org/")
    assert adaylar[0].indirme.startswith("https://images.metmuseum.org/") and len(adaylar[0].yedek) == 1


def test_met_kamu_mali_olmayan_eserler_elenir_nazik_istek_siniri():
    s, ids = _met_sahte({})
    aranan("met", s, adet=20)
    assert len(s.istekler) == 1 + 6                                               # arama + en cok 6 nesne


def test_met_ilk_nesne_hatasi_yukari_cikar_sonrakiler_atlanir():
    arama = yukle("met_arama.json")
    s = Sahte({("collectionapi.metmuseum.org", "/public/collection/v1.1/search"): arama,
               ("collectionapi.metmuseum.org", "/public/collection/v1/objects/*"): lambda i: httpx.Response(500)})
    with pytest.raises(StokHatasi, match="yanıt vermiyor"):
        aranan("met", s, adet=3)
    s2, ids2 = _met_sahte({yukle("met_arama.json")["objectIDs"][1]: "hata"})
    assert len(aranan("met", s2, adet=3)) == 1                                    # 2. nesne 500: atlanir, 1. gelir


def test_met_arama_sonucu_bos():
    s = Sahte({("collectionapi.metmuseum.org", "/public/collection/v1.1/search"): {"total": 0, "objectIDs": None}})
    assert aranan("met", s) == []


# ============================================================ AIC

def _aic_sahte(degistir=None):
    d = yukle("aic_arama.json")
    if degistir:
        degistir(d["data"])
    return Sahte({("api.artic.edu", "/api/v1/artworks/search"): d})


def test_aic_basliksiz_istek_403_aldigi_icin_baslik_zorunlu_ve_iiif_adresleri():
    s = _aic_sahte()
    adaylar = aranan("aic", s, "Rembrandt Night Watch painting")
    i = s.istekler[0]
    assert i.headers["aic-user-agent"] and i.url.params["query[term][is_public_domain]"] == "true"
    assert i.url.params["q"] == "Rembrandt Night Watch" and "image_id" in i.url.params["fields"]
    assert len(adaylar) == 4 and all(a.lisans.startswith("CC0") for a in adaylar)
    a = adaylar[0]
    assert a.indirme == f"https://www.artic.edu/iiif/2/{a.indirme.split('/iiif/2/')[1].split('/')[0]}/full/1686,/0/default.jpg"
    assert a.yedek[0].endswith("/full/843,/0/default.jpg") and a.onizleme == a.yedek[0]
    assert [x.dikey for x in adaylar] == [True, True, True, False]                 # dikey olanlar once; 2784x2250 yatay olan sona
    assert [x.genislik for x in adaylar] == [2314, 1499, 1489, 2784]


def test_aic_kamu_mali_olmayan_ve_gorselsiz_kayitlar_elenir():
    def degistir(veri):
        veri[0]["is_public_domain"] = False
        veri[1]["image_id"] = None
        veri.append("bozuk")
    assert len(aranan("aic", _aic_sahte(degistir))) == 2


# ============================================================ Cleveland

def test_cleveland_istegi_ve_boyutlar_metin_gelse_de_cozulur():
    d = yukle("cleveland_arama.json")
    s = Sahte({("openaccess-api.clevelandart.org", "/api/artworks/"): d})
    adaylar = aranan("cleveland", s, "sunflower")
    p = s.istekler[0].url.params
    assert p["cc0"] == "1" and p["has_image"] == "1" and p["q"] == "sunflower"
    assert len(adaylar) == 3 and all(a.lisans.startswith("CC0") for a in adaylar)
    assert (adaylar[0].genislik, adaylar[0].yukseklik) == (767, 893) and adaylar[0].dikey is True        # metin '767' sayiya cevrildi
    assert [a.dikey for a in adaylar] == [True, True, False] and adaylar[-1].genislik == 1263
    assert adaylar[0].indirme.startswith("https://openaccess-cdn.clevelandart.org/")


def test_cleveland_cc0_olmayanlar_elenir():
    d = yukle("cleveland_arama.json")
    d["data"][0]["share_license_status"] = "Copyrighted"
    d["data"][1]["images"] = {}
    s = Sahte({("openaccess-api.clevelandart.org", "/api/artworks/"): d})
    assert len(aranan("cleveland", s)) == 1


# ============================================================ stok.ara yonlendirme

@pytest.mark.parametrize("kaynak", ["wikimedia", "nasa", "met", "aic", "cleveland"])
def test_acik_kaynaklar_video_aramaz_ve_anahtar_istemez(kaynak):
    with pytest.raises(StokHatasi, match="yalnızca görsel"):
        stok.ara(kaynak, "video", "konu", None, istemci=Sahte({}).istemci(), onbellek=Onbellek())


def test_acik_kaynak_arama_yetki_basligi_gondermez_ve_sonuclari_onbellekler():
    s = _wiki_sahte()
    ob = Onbellek()
    istemci = s.istemci()
    ilk = stok.ara("wikimedia", "gorsel", "sunflower field", None, istemci=istemci, onbellek=ob)
    assert "authorization" not in s.istekler[0].headers and "key" not in s.istekler[0].url.params
    assert stok.ara("wikimedia", "gorsel", "Sunflower   Field", None, istemci=istemci, onbellek=ob) == ilk and len(s.istekler) == 1
    assert ob.aday_getir(ilk[0].kimlik) == ilk[0]


def test_kaynak_bilgi_tutarli():
    assert set(stok.KAYNAK_BILGI) == set(stok.KAYNAKLAR) == set(stok.IZINLI_ALAN_ADLARI) == set(stok.KAYNAK_ADI)
    assert [k for k, v in stok.KAYNAK_BILGI.items() if v["anahtar_gerekli"]] == list(stok.ANAHTARLI)
    assert [k for k, v in stok.KAYNAK_BILGI.items() if v["video"]] == list(stok.VIDEO_KAYNAKLARI)
    assert all(v["adres"].startswith("https://") for v in stok.KAYNAK_BILGI.values())


# ============================================================ sogutucu, 429, Retry-After

class SahteSaat:
    def __init__(self):
        self.t, self.uykular = 1000.0, []

    def saat(self):
        return self.t

    def uyku(self, sn):
        self.uykular.append(round(sn, 3))
        self.t += sn


def test_sogutucu_en_kisa_araligi_bekler():
    s = SahteSaat()
    g = Sogutucu(saat=s.saat, uyku=s.uyku, aralik={"wikimedia": 1.0})
    g.bekle("wikimedia"); assert s.uykular == []                                  # ilk cagri beklemez
    s.t += 0.25; g.bekle("wikimedia"); assert s.uykular == [0.75]                 # 1 sn aralik: 0.75 bekledi
    s.t += 5; g.bekle("wikimedia"); assert s.uykular == [0.75]                    # yeterince gecti
    g.bekle("nasa"); assert s.uykular == [0.75]                                   # baska kaynak etkilenmez


def test_429_sonrasi_kaynak_sogutulur_diger_kaynaklar_etkilenmez_sure_dolunca_acilir():
    s = SahteSaat()
    g = Sogutucu(saat=s.saat, uyku=s.uyku, aralik={})
    sayac = []

    def yanit(istek):
        sayac.append(1)
        return httpx.Response(429, headers={"retry-after": "30"}, json={})
    wiki = Sahte({("commons.wikimedia.org", "/w/api.php"): yanit})
    with pytest.raises(StokHatasi, match="istek sınırı aşıldı") as ilk:
        aranan("wikimedia", wiki, sogutucu=g)
    assert ilk.value.bekle_sn == 30.0 and len(sayac) == 1
    with pytest.raises(StokHatasi, match=r"istek sınırına takıldı; yaklaşık 30 sn") as ikinci:
        aranan("wikimedia", wiki, "baska arama", sogutucu=g)
    assert len(sayac) == 1 and ikinci.value.bekle_sn == 30.0                       # AG'A GITMEDI: servis dovulmuyor
    assert aranan("nasa", _nasa_sahte(), sogutucu=g)                               # baska kaynak calisir
    s.t += 31
    wiki2 = _wiki_sahte()
    assert aranan("wikimedia", wiki2, sogutucu=g) and len(wiki2.istekler) == 1     # sogutma bitti


def test_429_baslik_yoksa_varsayilan_bir_dakika_sogutur():
    s = SahteSaat()
    g = Sogutucu(saat=s.saat, uyku=s.uyku, aralik={})
    with pytest.raises(StokHatasi):
        aranan("nasa", Sahte({("images-api.nasa.gov", "/search"): lambda i: httpx.Response(429)}), sogutucu=g)
    s.t += 59
    with pytest.raises(StokHatasi, match="takıldı"):
        aranan("nasa", _nasa_sahte(), "x2", sogutucu=g)
    s.t += 2
    assert aranan("nasa", _nasa_sahte(), "x3", sogutucu=g)


def test_pexels_429_da_sogutur_ve_anahtar_mesajda_yok():
    s = SahteSaat()
    g = Sogutucu(saat=s.saat, uyku=s.uyku, aralik={})
    pex = Sahte({("api.pexels.com", "/videos/search"): lambda i: httpx.Response(429, headers={"x-ratelimit-reset": str(int(time.time()) + 600)})})
    with pytest.raises(StokHatasi) as hata:
        stok.ara("pexels", "video", "konu", "GIZLI-ANAHTAR-123456", istemci=pex.istemci(), onbellek=Onbellek(), sogutucu=g)
    assert 590 < hata.value.bekle_sn <= 600 and "GIZLI-ANAHTAR" not in str(hata.value)
    with pytest.raises(StokHatasi, match="takıldı") as ikinci:
        stok.ara("pexels", "video", "baska", "GIZLI-ANAHTAR-123456", istemci=pex.istemci(), onbellek=Onbellek(), sogutucu=g)
    assert "GIZLI-ANAHTAR" not in str(ikinci.value)


@pytest.mark.parametrize("basliklar,beklenen", [
    ({"retry-after": "30"}, 30.0), ({"retry-after": "0"}, 0.0), ({"retry-after": "-5"}, 0.0),
    ({"x-ratelimit-reset": "120"}, 120.0),     ({}, None), ({"retry-after": "bozuk"}, None), ({"x-ratelimit-reset": "bozuk"}, None),
])
def test_bekleme_suresi_basliklari(basliklar, beklenen):
    assert bekleme_suresi(basliklar, simdi=5000.0) == beklenen        # 1000000000: gecmis UNIX zamani -> bekleme yok


def test_bekleme_suresi_gecmis_unix_zamani_sifir_bekleme_verir():
    assert bekleme_suresi({"x-ratelimit-reset": "1700000000"}, simdi=1_800_000_000.0) == 0.0


def test_bekleme_suresi_http_tarihi_ve_unix_sifirlama():
    simdi = 1_800_000_000.0
    gelecek = datetime.fromtimestamp(simdi + 90, tz=timezone.utc)
    assert bekleme_suresi({"retry-after": format_datetime(gelecek, usegmt=True)}, simdi=simdi) == pytest.approx(90, abs=1)
    assert bekleme_suresi({"retry-after": format_datetime(gelecek - timedelta(hours=1), usegmt=True)}, simdi=simdi) == 0.0
    assert bekleme_suresi({"x-ratelimit-reset": str(int(simdi) + 45)}, simdi=simdi) == 45.0


# ============================================================ indirme: yedek adresler, kaynaga ozel izin listesi, basliklar

def _aday(kaynak="nasa", indirme="https://images-assets.nasa.gov/image/x/x~orig.jpg", yedek=(), tur="gorsel"):
    return Aday(kimlik="c" * 16, kaynak=kaynak, tur=tur, onizleme="", indirme=indirme, sayfa="https://images.nasa.gov/details/x",
                kredi="NASA", kredi_adresi="", yedek=tuple(yedek))


def test_ana_adres_yoksa_yedek_sirayla_denenir(tmp_path):
    gorulen = []

    def isleyici(istek):
        gorulen.append(istek.url.path)
        if istek.url.path.endswith("~orig.jpg"):
            return httpx.Response(404)
        if istek.url.path.endswith("~large.jpg"):
            return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/html"})        # gorsel degil: sonrakini dene
        return httpx.Response(200, content=b"J" * 100, headers={"content-type": "image/jpeg"})
    yedekler = ("https://images-assets.nasa.gov/image/x/x~large.jpg", "https://images-assets.nasa.gov/image/x/x~medium.jpg")
    ist = httpx.Client(transport=httpx.MockTransport(isleyici))
    yol = stok.indir(_aday(yedek=yedekler), tmp_path, 1, istemci=ist)
    assert [g.rsplit("~", 1)[1] for g in gorulen] == ["orig.jpg", "large.jpg", "medium.jpg"]
    assert yol.read_bytes() == b"J" * 100 and [p.name for p in tmp_path.iterdir()] == [yol.name]


def test_cok_buyuk_orijinal_yedege_dusurur(tmp_path, monkeypatch):
    monkeypatch.setattr(stok, "GORSEL_AZAMI_BAYT", 1000)

    def isleyici(istek):
        if "orig" in istek.url.path:
            return httpx.Response(200, content=b"B" * 5000, headers={"content-type": "image/jpeg"})
        return httpx.Response(200, content=b"k" * 300, headers={"content-type": "image/jpeg"})
    ist = httpx.Client(transport=httpx.MockTransport(isleyici))
    a = _aday(yedek=("https://images-assets.nasa.gov/image/x/x~medium.jpg",))
    assert stok.indir(a, tmp_path, 1, istemci=ist).read_bytes() == b"k" * 300


def test_tum_adresler_basarisizsa_son_hata_doner_ve_dosya_kalmaz(tmp_path):
    ist = httpx.Client(transport=httpx.MockTransport(lambda i: httpx.Response(404)))
    with pytest.raises(StokHatasi, match="başarısız"):
        stok.indir(_aday(yedek=("https://images-assets.nasa.gov/image/x/x~large.jpg",)), tmp_path, 1, istemci=ist)
    assert list(tmp_path.iterdir()) == []


def test_guvenlik_hatasi_yedege_dusmez(tmp_path):
    gorulen = []

    def isleyici(istek):
        gorulen.append(istek.url.host)
        if istek.url.host == "images-assets.nasa.gov":
            return httpx.Response(302, headers={"location": "https://evil.example/x.jpg"})
        return httpx.Response(200, content=b"ZARARLI", headers={"content-type": "image/jpeg"})
    ist = httpx.Client(transport=httpx.MockTransport(isleyici))
    with pytest.raises(StokHatasi, match="yönlendirildi"):
        stok.indir(_aday(yedek=("https://images-assets.nasa.gov/image/x/x~large.jpg",)), tmp_path, 1, istemci=ist)
    assert gorulen == ["images-assets.nasa.gov", "evil.example"]                    # yonlendirme izlendi, kontrolde durdu; yedege DUSMEDI
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kaynak,adres", [
    ("nasa", "https://upload.wikimedia.org/x.jpg"), ("wikimedia", "https://images-assets.nasa.gov/x.jpg"),
    ("met", "https://www.artic.edu/iiif/x.jpg"), ("aic", "https://images.metmuseum.org/x.jpg"),
    ("cleveland", "https://upload.wikimedia.org/x.jpg"), ("pexels", "https://cdn.pixabay.com/x.mp4"), ("pixabay", "https://videos.pexels.com/x.mp4"),
    ("wikimedia", "https://wikimedia.org.evil.example/x.jpg"), ("nasa", "https://evilnasa.gov/x.jpg"), ("aic", "http://www.artic.edu/x.jpg"),
])
def test_izin_listesi_kaynaga_ozeldir_baska_kaynagin_sunucusu_reddedilir(tmp_path, kaynak, adres):
    def ulasmamali(istek):
        raise AssertionError("izinsiz adrese istek atildi")
    with pytest.raises(StokHatasi, match="izin verilen"):
        stok.indir(_aday(kaynak, adres), tmp_path, 1, istemci=httpx.Client(transport=httpx.MockTransport(ulasmamali)))
    assert list(tmp_path.iterdir()) == []


def test_yedek_adres_izin_disiysa_hic_istek_atilmaz(tmp_path):
    def ulasmamali(istek):
        raise AssertionError("istek atilmamaliydi")
    a = _aday(yedek=("https://evil.example/x.jpg",))
    with pytest.raises(StokHatasi, match="izin verilen"):
        stok.indir(a, tmp_path, 1, istemci=httpx.Client(transport=httpx.MockTransport(ulasmamali)))


def test_aic_gorsel_sunucusu_icin_ozel_baslik_gonderilir_digerlerine_gonderilmez(tmp_path):
    gorulen = {}

    def isleyici(istek):
        gorulen[istek.url.host] = dict(istek.headers)
        return httpx.Response(200, content=b"J" * 10, headers={"content-type": "image/jpeg"})
    ist = httpx.Client(transport=httpx.MockTransport(isleyici))
    stok.indir(_aday("aic", "https://www.artic.edu/iiif/2/abc/full/843,/0/default.jpg"), tmp_path, 1, istemci=ist)
    stok.indir(_aday("wikimedia", "https://upload.wikimedia.org/x.jpg"), tmp_path, 2, istemci=ist)
    assert gorulen["www.artic.edu"].get("aic-user-agent") and "aic-user-agent" not in gorulen["upload.wikimedia.org"]


def test_indirilen_dosya_adi_yeni_kaynaklari_kapsar(tmp_path):
    from src.icerik.paket import MEDYA_DOSYA_DESENI
    ist = httpx.Client(transport=httpx.MockTransport(lambda i: httpx.Response(200, content=b"J" * 10, headers={"content-type": "image/jpeg"})))
    for kaynak, adres in (("wikimedia", "https://upload.wikimedia.org/a.jpg"), ("nasa", "https://images-assets.nasa.gov/a.jpg"),
                          ("met", "https://images.metmuseum.org/a.jpg"), ("aic", "https://www.artic.edu/a.jpg"),
                          ("cleveland", "https://openaccess-cdn.clevelandart.org/a.jpg")):
        yol = stok.indir(_aday(kaynak, adres), tmp_path, 7, istemci=ist)
        assert MEDYA_DOSYA_DESENI.fullmatch(yol.name), yol.name


# ============================================================ User-Agent ve SSRF korumali varsayilan istemci

def test_kullanici_ajani_iletisim_icerir_kutuphane_adi_icermez(monkeypatch):
    monkeypatch.delenv("IC_ILETISIM", raising=False)
    ua = kullanici_ajani()
    assert ua.startswith("IcerikStudyosu/1.0 (") and stok.PROJE_ADRESI in ua and "python" not in ua.lower() and "httpx" not in ua.lower()
    monkeypatch.setenv("IC_ILETISIM", "ben@ornek.com")
    assert "(ben@ornek.com;" in kullanici_ajani()


@pytest.mark.parametrize("girdi", ["ben@ornek.com\r\nX-Evil: 1", "x\ny", "a" * 500, "<script>alert(1)</script>", "\x01\x02\x1b[31m"])
def test_kullanici_ajani_baslik_enjeksiyonuna_kapali(monkeypatch, girdi):
    monkeypatch.setenv("IC_ILETISIM", girdi)
    ua = kullanici_ajani()
    assert "\r" not in ua and "\n" not in ua and "\x00" not in ua and "<" not in ua and ">" not in ua and len(ua) < 200


def test_varsayilan_istemci_ssrf_korumali_ve_ua_ile_olusturulur(monkeypatch, tmp_path):
    """Test istemcisi verilmezse ara() ve indir() ag_guvenlik.guvenli_istemci kullanmali (gercek ag yok: sahte yapici)."""
    olusturulan = []

    def sahte_yapici(**ayar):
        olusturulan.append(ayar)
        return httpx.Client(transport=httpx.MockTransport(
            lambda i: httpx.Response(200, json=yukle("nasa_arama.json")) if i.url.host == "images-api.nasa.gov"
            else httpx.Response(200, content=b"J" * 10, headers={"content-type": "image/jpeg"})))

    monkeypatch.setattr(stok, "guvenli_istemci", sahte_yapici)
    (a, *_) = stok.ara("nasa", "gorsel", "moon", None, onbellek=Onbellek())
    stok.indir(a, tmp_path, 1)
    assert len(olusturulan) == 2 and all(o["headers"]["User-Agent"] == kullanici_ajani() for o in olusturulan)


def test_ssrf_korumasi_acik_kaynak_aramada_yerel_adrese_gitmez(monkeypatch):
    """Gercek guvenli_istemci: arama adresi yerel aga cozulurse ag cagrisi yapilmadan anlasilir hata."""
    from src.icerik import ag_guvenlik
    monkeypatch.setattr(ag_guvenlik, "_sistem_cozucu", lambda ad: ["127.0.0.1"])
    ag_guvenlik._dns_onbellek.clear()
    with pytest.raises(StokHatasi, match="çözülen ad engellendi") as hata:
        stok.ara("nasa", "gorsel", "moon", None, onbellek=Onbellek())
    assert hata.value.__cause__ is None and hata.value.__suppress_context__ is True
    ag_guvenlik._dns_onbellek.clear()


# ============================================================ panel

def _paket_yaz(klasor: Path) -> None:
    hedef = klasor / PAKET_ID
    hedef.mkdir(parents=True)
    paket = {"id": PAKET_ID, "konu": "test", "olusturma": "2026-10-25T12:00:00+00:00", "saglayici": "sahte", "basliklar": ["b"],
             "secili_baslik": 0, "caption": "c", "hashtagler": [], "seo": {}, "analiz": {}, "kapak": {}, "trendler": [], "uyarilar": [],
             "medya": {}, "senaryo": [{"sure_sn": 3, "anlatim": "bir", "ekran_yazisi": "", "gorsel_aramasi": "sunflower field", "gorsel_istemi": ""}]}
    (hedef / "paket.json").write_text(json.dumps(paket, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def panel(tmp_path):
    klasor = tmp_path / "paketler"
    klasor.mkdir()
    _paket_yaz(klasor)
    sahte = Sahte({
        ("commons.wikimedia.org", "/w/api.php"): yukle("wikimedia_arama.json"),
        ("thumb.wikimedia.org", "*"): lambda i: httpx.Response(200, content=b"WIKI" * 50, headers={"content-type": "image/jpeg"}),
        ("images-api.nasa.gov", "/search"): yukle("nasa_arama.json"),
    })
    depo = AnahtarDeposu(tmp_path / "k.json", ortam={})
    app = uygulama_olustur(
        paket_klasoru=klasor, saglayicilar=lambda: [], saglayici_bulucu=lambda a: None, izinli_hostlar=("127.0.0.1", "testserver"),
        kapak_ciz=False, anahtar_depo=depo, stok_istemci=sahte.istemci(), stok_onbellek=Onbellek(),
    )
    c = TestClient(app)
    c.klasor, c.sahte, c.depo = klasor, sahte, depo
    return c


def test_panel_kaynaklar_uc_noktasi_anahtar_gizli(panel):
    r = panel.get("/api/kaynaklar")
    assert r.status_code == 200
    liste = {k["ad"]: k for k in r.json()}
    assert list(liste) == ["pexels", "pixabay", "wikimedia", "nasa", "met", "aic", "cleveland"]
    assert liste["pexels"]["anahtar_gerekli"] and liste["pexels"]["anahtar_tanimli"] is False and liste["pexels"]["video"]
    assert liste["wikimedia"]["anahtar_gerekli"] is False and liste["wikimedia"]["anahtar_tanimli"] is None and not liste["wikimedia"]["video"]
    panel.depo.kaydet("pexels", "SIRRI-ANAHTAR-0123456789abcdef")
    r2 = panel.get("/api/kaynaklar")
    assert "SIRRI-ANAHTAR" not in r2.text and next(k for k in r2.json() if k["ad"] == "pexels")["anahtar_tanimli"] is True


def test_panel_anahtarsiz_kaynakta_arama_ve_secim_cc_by_sa_atif_zorunlu(panel):
    r = panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "gorsel", "kaynak": "wikimedia", "sorgu": "sunflower field"})
    assert r.status_code == 200 and r.json()["kaynak_adresi"] == "https://commons.wikimedia.org" and r.json()["kaynak_adi"] == "Wikimedia Commons"
    aday = r.json()["adaylar"][0]
    assert aday["lisans"] == "CC BY-SA 4.0" and aday["atif_gerekli"] is True and aday["dikey"] is True and aday["yedek"]
    sec = panel.post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, "aday_kimligi": aday["kimlik"]})
    assert sec.status_code == 200
    kayit = sec.json()
    assert kayit["lisans"] == "CC BY-SA 4.0" and kayit["atif_gerekli"] is True and kayit["atif"].startswith('"Sunflower field Milky Way"')
    assert "türev eser" in kayit["uyari"] and kayit["dosya"].startswith("01-wikimedia-")
    krediler = (panel.klasor / PAKET_ID / "krediler.txt").read_text(encoding="utf-8")
    assert "CC BY-SA 4.0" in krediler and "[ATIF ZORUNLU: açıklamaya ekleyin]" in krediler
    assert (panel.klasor / PAKET_ID / "medya" / kayit["dosya"]).read_bytes() == b"WIKI" * 50


def test_panel_nasa_atif_zorunlu_degil_onerilir(panel):
    aday = panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "gorsel", "kaynak": "nasa", "sorgu": "moon surface"}).json()["adaylar"][0]
    assert aday["atif_gerekli"] is False and aday["atif_onerilen"] is True and aday["dikey"] is None


def test_panel_acik_kaynakta_video_istenirse_anlasilir_hata(panel):
    r = panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "video", "kaynak": "wikimedia", "sorgu": "sunflower"})
    assert r.status_code == 400 and "yalnızca görsel" in r.json()["detail"]
    assert panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "gorsel", "kaynak": "flickr", "sorgu": "x y"}).status_code == 422


def test_panel_anahtarli_kaynak_hala_anahtar_ister(panel):
    r = panel.post(f"/api/paket/{PAKET_ID}/stok", json={"sahne": 1, "tur": "gorsel", "kaynak": "pexels", "sorgu": "sunflower"})
    assert r.status_code == 400 and "anahtarı tanımlı değil" in r.json()["detail"]


def test_panel_aday_kaynagi_ile_sunucu_uyusmazligi_indirilmez(panel):
    """Wikimedia adayi baska bir kaynagin sunucusuna isaret ediyorsa (ele gecirilmis yanit) indirme reddedilir."""
    zararli = Aday(kimlik="d" * 16, kaynak="wikimedia", tur="gorsel", onizleme="", indirme="https://images-assets.nasa.gov/x.jpg",
                   sayfa="https://commons.wikimedia.org/wiki/File:x.jpg", kredi="x", kredi_adresi="")
    ob = Onbellek()
    ob.arama_koy(("x",), [zararli])
    app = uygulama_olustur(paket_klasoru=panel.klasor, saglayicilar=lambda: [], saglayici_bulucu=lambda a: None,
                           izinli_hostlar=("testserver",), kapak_ciz=False, stok_istemci=Sahte({}).istemci(), stok_onbellek=ob)
    r = TestClient(app).post(f"/api/paket/{PAKET_ID}/medya", json={"sahne": 1, "aday_kimligi": "d" * 16})
    assert r.status_code == 400 and "izin verilen" in r.json()["detail"]
