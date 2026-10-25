# -*- coding: utf-8 -*-
"""Anahtarsiz acik kaynak GORSEL kaynaklari: Wikimedia Commons, NASA, The Met, Art Institute of Chicago, Cleveland.

Mantik ve ogrenilmis dersler Otopilot'un lib/gorsel-kaynak.js dosyasindan (MIT) tasindi; ucu ucuna 2026-10-25'te CANLI
dogrulandi:
  - The Met'in eski /v1/search ucu 410 Gone (1 Ekim 2026'da kapandi): v1.1 kullanilir, nesne ucu v1'de kaldi.
  - Art Institute of Chicago `AIC-User-Agent` basligi olmadan 403 verir.
  - Wikimedia kucuk resim genislikleri standart basamaklardan (320/640/800/1024/1280/1920/2560/3840) olmali.
  - NASA kucuk resim adresi `~thumb.jpg`; buyuk surum `~orig/~large/~medium` olarak sirayla denenir.
Lisans suzgeci burada calisir: NC/ND ve serbest olmayan dosyalar aday listesine hic girmez.
Her kaynak yalnizca GORSEL doner (video yok). Istekler `getir(url, params=None, basliklar=None) -> dict` ile yapilir:
ag hatasi/kota yonetimi cagiranda (stok.py) oldugu icin burada sahte `getir` ile test edilir.
"""
from __future__ import annotations

import html
import math
import re
import threading
import time
from typing import Callable
from urllib.parse import quote

from .aday import Aday, StokHatasi, aday_kimligi
from .lisans import lisans_coz

Getir = Callable[..., dict]

ACIK_KAYNAKLAR = ("wikimedia", "nasa", "met", "aic", "cleveland")
AIC_BASLIGI = {"AIC-User-Agent": "IcerikStudyosu (acik kaynak masaustu araci)"}

# Kaynaklar arasi en kisa aralik (sn): nazik davran, hiz sinirina takilma (Otopilot'un olculmus degerleri)
ARALIK_SN = {"wikimedia": 1.0, "nasa": 0.3, "met": 0.4, "aic": 0.4, "cleveland": 0.4}


class Sogutucu:
    """Kaynak basina en kisa aralik + 429 sonrasi 'sogutma': kullanici ust uste tiklayip servisi dovmesin."""

    def __init__(self, saat: Callable[[], float] = time.monotonic, uyku: Callable[[float], None] = time.sleep,
                 aralik: dict[str, float] | None = None) -> None:
        self._saat, self._uyku = saat, uyku
        self._aralik = ARALIK_SN if aralik is None else aralik
        self._son: dict[str, float] = {}
        self._kapali: dict[str, float] = {}
        self._kilit = threading.Lock()

    def bekle(self, kaynak: str, ad: str | None = None) -> None:
        with self._kilit:
            simdi = self._saat()
            bitis = self._kapali.get(kaynak)
            if bitis is not None and simdi < bitis:
                raise StokHatasi(
                    f"{ad or kaynak} istek sınırına takıldı; yaklaşık {math.ceil(bitis - simdi)} sn sonra tekrar deneyin.",
                    bekle_sn=bitis - simdi,
                )
            gerek = self._aralik.get(kaynak, 0.0)
            son = self._son.get(kaynak)
            bekleme = max(0.0, son + gerek - simdi) if son is not None else 0.0
            self._son[kaynak] = simdi + bekleme               # sirayi ayir: es zamanli cagrilar birbirini ezmesin
        if bekleme > 0:
            self._uyku(bekleme)

    def sogut(self, kaynak: str, sn: float) -> None:
        with self._kilit:
            self._kapali[kaynak] = self._saat() + max(1.0, sn)


SOGUTUCU = Sogutucu()

# ---------------------------------------------------------------- baslik suzgecleri (Otopilot gorsel-puan.js)
_SINIR_ON, _SINIR_ARKA = r"(?<![^\W_])", r"(?![^\W_])"       # harf/rakam sinirlari (unicode): "chart" ~ "charter"


def _re(parcalar: list[str]) -> re.Pattern:
    return re.compile(_SINIR_ON + "(?:" + "|".join(parcalar) + ")" + _SINIR_ARKA, re.I)


# SERT: sosyal medya videosu icin hicbir konuda kullanisiz turler: ELENIR
KATI = _re(["maps?", "diagrams?", "charts?", "graphs?", "plots?", "schematics?", "infographics?", "logos?", "icons?",
            "clip ?art", "screenshots?", "coats? of arms", "banners?", "templates?", "mock-?ups?", "scanned", "book covers?",
            "magazine covers?", "album covers?", "cover art", "vectors?", "svg", "pdf", "wordmark", "emblems?", "badges?",
            "crest", "flowcharts?", "timelines?", "harita(?:lar)?", "grafik(?:ler)?"])
# YUMUSAK / BELGE: istenmeyen ama baska aday yoksa kullanilabilir: ELENMEZ, sona konur
YUMUSAK = _re(["drawings?", "paintings?", "illustrations?", "sketch(?:es)?", "engravings?", "lithographs?", "etchings?",
               "museum", "exhibit(?:ion)?s?", "replicas?", "models?", "cgi", "3d render(?:ing)?s?", "renders?", "rendering",
               "artwork", "digital art", "çizim(?:ler)?", "resim", "müze", "statues?", "statuettes?", "sculptures?", "bronze",
               "figurines?", "toys?", "plush", "stuffed", "taxidermy", "dioramas?", "miniatures?", "lego", "tattoos?",
               "cartoons?", "heykel(?:ler)?", "oyuncak(?:lar)?"])
BELGE = _re(["scans?", "posters?", "manuscripts?", "newspapers?", "stamps?", "coins?", "banknotes?", "postage", "signatures?"])
# NASA'ya ozgu (YALNIZ BASLIKTA aranir: aciklama tesaduf kelime tasir, "Pillars of Creation" aciklamasinda "instrument")
KARA_NASA = _re(["diagrams?", "charts?", "graphs?", "plots?", "schematics?", "spectrum", "light curves?", "infographics?",
                 "logos?", "patch(?:es)?", "insignia", "awards?", "ceremon(?:y|ies)", "employees?", "staff", "meetings?",
                 "conferences?", "briefings?", "cleanrooms?", "laborator(?:y|ies)", "technicians?", "engineers?", "workshops?",
                 "classrooms?", "interviews?", "group photo", "test stands?", "badges?", "screenshots?", "map of", "timelines?"])
NASA_YUMUSAK = _re(["simulations?", "illustrations? of", "portraits? of", "headshots?", "facilit(?:y|ies)", "assembly",
                    "hardware", "instruments?", "antenna", "dish", "telescope mirrors?", "launch pads?", "control rooms?"])
MUZE_DOLGU = re.compile(r"^(paintings?|photos?|photographs?|images?|pictures?|views?|closeups?|close-up|detail|details|"
                        r"restoration|scenes?|the|a|an|of|in|on|at|and|with|for)$", re.I)


def muze_sorgu(sorgu: str) -> str:
    """Muze arama motorlari tum kelimeleri ister (VE): genel dolgu kelimelerini atip en cok 3 kelime tutar."""
    kelimeler = sorgu.split()
    secilen = [k for k in kelimeler if not MUZE_DOLGU.match(k)]
    return " ".join((secilen or kelimeler)[:3])


# ---------------------------------------------------------------- yardimcilar

def _temiz(deger: object, azami: int = 200) -> str:
    """HTML etiketlerini ve varliklarini temizler (kaynaklardan gelen metin guvenilmezdir)."""
    if deger is None:
        return ""
    metin = html.unescape(re.sub(r"<[^>]+>", " ", str(deger)))
    return re.sub(r"\s+", " ", metin).strip()[:azami]


def _int(deger: object) -> int | None:
    """Sayi ya da sayi metni ('1263': Cleveland boyutlari metin olarak verir) -> pozitif int, yoksa None."""
    if isinstance(deger, bool):
        return None
    if isinstance(deger, str) and deger.strip().isdigit():
        deger = int(deger.strip())
    return int(deger) if isinstance(deger, (int, float)) and deger > 0 else None


def _dikey(g: int | None, y: int | None) -> bool | None:
    return None if not g or not y else y > g


def _sirala(ogeler: list[tuple[Aday, bool, int]], adet: int) -> list[Aday]:
    """Dikey kadraj once, zayif (yumusak/belge) olanlar sonra, iyi lisans once. Kararli siralama."""
    def anahtar(oge: tuple[Aday, bool, int]):
        aday, zayif, lisans_sira = oge
        return ({True: 0, None: 1, False: 2}[aday.dikey], zayif, lisans_sira)
    return [o[0] for o in sorted(ogeler, key=anahtar)][:adet]


# ---------------------------------------------------------------- Wikimedia Commons

def wikimedia_ara(sorgu: str, adet: int, getir: Getir) -> list[Aday]:
    d = getir("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "generator": "search", "gsrsearch": f"{sorgu} filetype:bitmap", "gsrnamespace": 6,
        "gsrlimit": min(max(adet * 3, 10), 30), "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": 1280,                                           # standart basamak; iiurlheight EKLENMEZ (429 alabilir)
        "iiextmetadatafilter": "LicenseShortName|Artist|ImageDescription|ObjectName|NonFree|Categories",
        "format": "json", "formatversion": 2, "origin": "*",
    })
    sayfalar = (d.get("query") or {}).get("pages") or []
    if isinstance(sayfalar, dict):
        sayfalar = list(sayfalar.values())
    sayfalar = sorted((p for p in sayfalar if isinstance(p, dict)), key=lambda p: p.get("index", 0))
    ogeler: list[tuple[Aday, bool, int]] = []
    for p in sayfalar:
        ii = (p.get("imageinfo") or [None])[0]
        if not isinstance(ii, dict) or not ii.get("thumburl") or ii.get("mime") not in ("image/jpeg", "image/png"):
            continue
        m = ii.get("extmetadata") or {}

        def v(k: str) -> str:
            return str((m.get(k) or {}).get("value", ""))

        lis = lisans_coz("wikimedia", _temiz(v("LicenseShortName")), _temiz(v("NonFree")))
        if not lis.kabul:
            continue
        baslik = re.sub(r"[_-]+", " ", re.sub(r"\.\w{2,4}$", "", re.sub(r"^File:", "", str(p.get("title", "")), flags=re.I))).strip()
        if KATI.search(baslik):
            continue
        g, y = _int(ii.get("thumbwidth") or ii.get("width")), _int(ii.get("thumbheight") or ii.get("height"))
        orijinal = ii.get("url")
        aday = Aday(
            kimlik=aday_kimligi("wikimedia", "gorsel", p.get("pageid") or p.get("title")), kaynak="wikimedia", tur="gorsel",
            onizleme=ii["thumburl"], indirme=ii["thumburl"], sayfa=str(ii.get("descriptionurl") or ""),
            kredi=_temiz(v("Artist"), 120) or "Wikimedia Commons", kredi_adresi=str(ii.get("descriptionurl") or ""),
            genislik=g, yukseklik=y, dikey=_dikey(g, y), baslik=baslik,
            lisans=lis.lisans, lisans_kodu=lis.kod, atif_gerekli=lis.atif_gerekli, atif_onerilen=lis.atif_onerilen, uyari=lis.uyari,
            yedek=(orijinal,) if orijinal and orijinal != ii["thumburl"] else (),
        )
        ogeler.append((aday, bool(YUMUSAK.search(baslik) or BELGE.search(baslik)), lis.sira))
    return _sirala(ogeler, adet)


# ---------------------------------------------------------------- NASA

_NASA_BOYUT = re.compile(r"~(thumb|small|medium|large|orig)\.(jpe?g|png)$", re.I)


def nasa_ara(sorgu: str, adet: int, getir: Getir) -> list[Aday]:
    d = getir("https://images-api.nasa.gov/search", params={
        "q": sorgu, "media_type": "image", "page_size": min(max(adet * 3, 10), 30)})
    lis = lisans_coz("nasa")
    ogeler: list[tuple[Aday, bool, int]] = []
    for it in ((d.get("collection") or {}).get("items") or []):
        v = (it.get("data") or [None])[0] if isinstance(it, dict) else None
        bag = (it.get("links") or [None])[0] if isinstance(it, dict) else None
        if not isinstance(v, dict) or not isinstance(bag, dict) or not bag.get("href"):
            continue
        aciklama = _temiz(v.get("description"), 300)
        if re.search(r"©|\bcopyright\b", aciklama, re.I):                 # NASA arsivindeki ucuncu taraf telifli goruntuler
            continue
        baslik = _temiz(v.get("title")) or sorgu
        if KARA_NASA.search(baslik) or KATI.search(baslik):
            continue
        href = str(bag["href"])
        taban = _NASA_BOYUT.sub("", href)
        adresler = [taban + "~orig.jpg", taban + "~large.jpg", taban + "~medium.jpg"] if taban != href else [href]
        nasa_id = str(v.get("nasa_id") or "")
        yazar = " / ".join(x for x in (v.get("photographer"), v.get("secondary_creator")) if x) or "NASA"
        sayfa = f"https://images.nasa.gov/details/{quote(nasa_id)}" if nasa_id else ""
        aday = Aday(
            kimlik=aday_kimligi("nasa", "gorsel", nasa_id or href), kaynak="nasa", tur="gorsel", onizleme=href, indirme=adresler[0],
            sayfa=sayfa, kredi=_temiz(yazar, 120), kredi_adresi=sayfa, dikey=None, baslik=baslik,
            lisans=lis.lisans, lisans_kodu=lis.kod, atif_gerekli=lis.atif_gerekli, atif_onerilen=lis.atif_onerilen,
            yedek=tuple(adresler[1:]),
        )
        ogeler.append((aday, bool(NASA_YUMUSAK.search(baslik) or YUMUSAK.search(baslik)), lis.sira))
    return _sirala(ogeler, adet)


# ---------------------------------------------------------------- Muzeler (yalniz CC0 / kamu mali)

def met_ara(sorgu: str, adet: int, getir: Getir) -> list[Aday]:
    kok = "https://collectionapi.metmuseum.org/public/collection/"
    d = getir(kok + "v1.1/search", params={"hasImages": "true", "isPublicDomain": "true", "q": muze_sorgu(sorgu)})
    ids = (d.get("objectIDs") or [])[: min(adet, 6)]                    # arama kimlik listesi doner, her eser ayri alinir
    lis = lisans_coz("met")
    ogeler: list[tuple[Aday, bool, int]] = []
    for i, oid in enumerate(ids):
        try:
            e = getir(f"{kok}v1/objects/{int(oid)}")
        except StokHatasi:
            if i == 0:
                raise
            continue
        if not isinstance(e, dict) or not e.get("isPublicDomain") or not e.get("primaryImage"):
            continue
        yazar = ", ".join(x for x in (e.get("artistDisplayName") or e.get("culture"), e.get("objectDate")) if x)
        sayfa = str(e.get("objectURL") or "")
        ogeler.append((Aday(
            kimlik=aday_kimligi("met", "gorsel", oid), kaynak="met", tur="gorsel",
            onizleme=str(e.get("primaryImageSmall") or e["primaryImage"]), indirme=str(e["primaryImage"]), sayfa=sayfa,
            kredi=_temiz(f"{yazar} — The Metropolitan Museum of Art" if yazar else "The Metropolitan Museum of Art", 120),
            kredi_adresi=sayfa, dikey=None, baslik=_temiz(e.get("title")),
            lisans=lis.lisans, lisans_kodu=lis.kod, atif_gerekli=lis.atif_gerekli, atif_onerilen=lis.atif_onerilen,
            yedek=(str(e["primaryImageSmall"]),) if e.get("primaryImageSmall") else (),
        ), False, 0))
    return _sirala(ogeler, adet)


def aic_ara(sorgu: str, adet: int, getir: Getir) -> list[Aday]:
    d = getir("https://api.artic.edu/api/v1/artworks/search", basliklar=AIC_BASLIGI, params={
        "q": muze_sorgu(sorgu), "query[term][is_public_domain]": "true", "limit": min(max(adet * 2, 8), 20),
        "fields": "id,title,artist_display,date_display,image_id,is_public_domain,thumbnail,subject_titles,style_title"})
    iiif = str((d.get("config") or {}).get("iiif_url") or "https://www.artic.edu/iiif/2")
    lis = lisans_coz("aic")
    ogeler: list[tuple[Aday, bool, int]] = []
    for e in d.get("data") or []:
        if not isinstance(e, dict) or not e.get("is_public_domain") or not e.get("image_id"):
            continue
        url = f"{iiif}/{e['image_id']}/full/1686,/0/default.jpg"          # AIC'nin onerdigi guvenli genislik
        kucuk = f"{iiif}/{e['image_id']}/full/843,/0/default.jpg"
        sayfa = f"https://www.artic.edu/artworks/{e.get('id')}"
        tn = e.get("thumbnail") if isinstance(e.get("thumbnail"), dict) else {}
        g, y = _int(tn.get("width")), _int(tn.get("height"))
        yazar = _temiz(e.get("artist_display"), 100)
        ogeler.append((Aday(
            kimlik=aday_kimligi("aic", "gorsel", e.get("id")), kaynak="aic", tur="gorsel", onizleme=kucuk, indirme=url, sayfa=sayfa,
            kredi=f"{yazar} — Art Institute of Chicago" if yazar else "Art Institute of Chicago", kredi_adresi=sayfa,
            genislik=g, yukseklik=y, dikey=_dikey(g, y), baslik=_temiz(e.get("title")),
            lisans=lis.lisans, lisans_kodu=lis.kod, atif_gerekli=lis.atif_gerekli, atif_onerilen=lis.atif_onerilen, yedek=(kucuk,),
        ), False, 0))
    return _sirala(ogeler, adet)


def cleveland_ara(sorgu: str, adet: int, getir: Getir) -> list[Aday]:
    d = getir("https://openaccess-api.clevelandart.org/api/artworks/", params={
        "q": muze_sorgu(sorgu), "has_image": 1, "cc0": 1, "limit": min(max(adet * 2, 8), 20)})
    lis = lisans_coz("cleveland")
    ogeler: list[tuple[Aday, bool, int]] = []
    for e in d.get("data") or []:
        if not isinstance(e, dict) or str(e.get("share_license_status") or "").upper() != "CC0":
            continue
        im = e.get("images") if isinstance(e.get("images"), dict) else {}
        web, baski = (im.get("web") or {}), (im.get("print") or {})
        buyuk = web.get("url") or baski.get("url")
        if not buyuk:
            continue
        g, y = _int(web.get("width")), _int(web.get("height"))
        yazar = _temiz(((e.get("creators") or [{}])[0] or {}).get("description"), 100)
        sayfa = str(e.get("url") or "")
        ogeler.append((Aday(
            kimlik=aday_kimligi("cleveland", "gorsel", e.get("id")), kaynak="cleveland", tur="gorsel", onizleme=str(web.get("url") or buyuk),
            indirme=str(buyuk), sayfa=sayfa, kredi=f"{yazar} — Cleveland Museum of Art" if yazar else "Cleveland Museum of Art",
            kredi_adresi=sayfa, genislik=g, yukseklik=y, dikey=_dikey(g, y), baslik=_temiz(e.get("title")),
            lisans=lis.lisans, lisans_kodu=lis.kod, atif_gerekli=lis.atif_gerekli, atif_onerilen=lis.atif_onerilen,
            yedek=(str(baski["url"]),) if baski.get("url") and baski.get("url") != buyuk else (),
        ), False, 0))
    return _sirala(ogeler, adet)


_ISLEVLER = {"wikimedia": wikimedia_ara, "nasa": nasa_ara, "met": met_ara, "aic": aic_ara, "cleveland": cleveland_ara}


def ara(kaynak: str, sorgu: str, adet: int, getir: Getir) -> list[Aday]:
    return _ISLEVLER[kaynak](sorgu, adet, getir)
