# -*- coding: utf-8 -*-
"""Trend / anahtar kelime toplayici.

Kaynaklar (ucretsiz, anahtarsiz):
  - Google arama onerileri
  - YouTube arama onerileri

Instagram'in herkese acik bir trend hashtag API'si YOKTUR. Bu modul hacim, sayi
ya da populerlik puani URETMEZ: sadece "insanlar bu sorgulari yaziyor" bilgisini
ve her ifadenin hangi kaynaktan geldigini dondurur.

Iki gecis vardir:
  1) konu_trendleri   : konudan genel tarama (yazidan ONCE, modele veri olur)
  2) ifade_trendleri  : modelin yazdigi SEO anahtar kelimeleriyle ikinci tarama
                        (yazidan SONRA, hashtag dogrulamasi ve zenginlestirme icin)
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

ADRES = "https://suggestqueries.google.com/complete/search"

KAYNAKLAR = {
    "google": {"client": "firefox"},
    "youtube": {"client": "firefox", "ds": "yt"},
}

GENISLETMELER = {
    "tr": ["", " nasıl", " ile", " için", " en iyi", " ücretsiz"],
    "en": ["", " how to", " with", " for", " best", " free"],
}

DURAK = {
    "tr": {"ile", "ve", "bir", "bu", "şu", "için", "gibi", "en", "da", "de", "mi", "mı", "mu", "mü",
           "ama", "veya", "her", "çok", "daha", "nasıl", "ne", "kadar", "yolu", "yolları"},
    "en": {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "how", "what", "is",
           "are", "your", "my", "by", "at", "from", "as", "ways", "way"},
}
AZAMI_CIFT = 4
YETERSIZ_VERI = 5
IFADE_ASGARI_UZUNLUK = 3


@dataclass
class TrendKelime:
    kelime: str
    kaynaklar: list[str] = field(default_factory=list)


def _tr_kucult(metin: str) -> str:
    return metin.replace("İ", "i").replace("I", "ı").lower()


def normalle(metin: str) -> str:
    """Karsilastirma anahtari: kucuk harf, fazla bosluk yok."""
    return re.sub(r"\s+", " ", _tr_kucult(metin)).strip()


def sozcuklere_ayir(metin: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", _tr_kucult(metin))


def etikete_cevir(kelime: str) -> str | None:
    """'yapay zeka araçları' -> '#yapayzekaaraçları'. Gecersiz/uzunsa None."""
    govde = re.sub(r"[^\w]", "", _tr_kucult(kelime), flags=re.UNICODE).replace("_", "")
    if not govde or len(govde) > 30 or govde.isdigit():
        return None
    return "#" + govde


def icerik_sozcukleri(konu: str, dil: str = "tr") -> list[str]:
    """Konudaki anlamli sozcukler (rakam ve durak sozcukler atilir)."""
    durak = DURAK.get(dil, DURAK["en"])
    return [t for t in sozcuklere_ayir(konu) if len(t) > 1 and t not in durak]


def ilgi_puani(kelime: str, konu_sozcukleri: list[str]) -> int:
    """Ifadenin konudaki kac sozcukle eslestigi (ilk 5 harf koku: 'otomatiklestirme' ~ 'otomasyon')."""
    parcalar = sozcuklere_ayir(kelime)
    return sum(
        1 for sozcuk in set(konu_sozcukleri)
        if any(parca.startswith(sozcuk[:5]) for parca in parcalar)
    )


def _plana_cevir(sorgular: list[str]) -> list[tuple[str, str]]:
    plan, gorulen = [], set()
    for kaynak in KAYNAKLAR:
        for sorgu in sorgular:
            anahtar = (kaynak, normalle(sorgu))
            if sorgu.strip() and anahtar not in gorulen:
                gorulen.add(anahtar)
                plan.append((kaynak, sorgu.strip()))
    return plan


def sorgu_plani(konu: str, dil: str = "tr") -> list[tuple[str, str]]:
    """Uzun bir konu cumlesi arama onerisinde neredeyse hic sonuc vermez: tam cumle bir kez;
    art arda gelen sozcuk ciftleri (kayan pencere) ve en uzun tek sozcuk 'tohum' olarak sorulur.
    Ilk tohum tum eklerle, digerleri kisa eklerle genisletilir."""
    ekler = GENISLETMELER.get(dil, GENISLETMELER["en"])
    sozcukler = icerik_sozcukleri(konu, dil)
    ciftler = [" ".join(sozcukler[i:i + 2]) for i in range(len(sozcukler) - 1)][:AZAMI_CIFT]
    tekler = sorted((w for w in sozcukler if len(w) >= 7), key=len, reverse=True)[:1]

    tohumlar: list[tuple[str, list[str]]] = []
    if ciftler:
        tohumlar.append((ciftler[0], ekler))
        tohumlar += [(c, ekler[:2]) for c in ciftler[1:]]
    elif sozcukler:
        tohumlar.append((sozcukler[0], ekler))
    tohumlar += [(t, ekler[:2]) for t in tekler]
    return _plana_cevir([konu] + [t + e for t, ekl in tohumlar for e in ekl])


def ifade_sorgu_plani(ifadeler: list[str], dil: str = "tr") -> list[tuple[str, str]]:
    """Kisa ifadeler (modelin SEO anahtar kelimeleri): her biri kendisi + 2 ekle sorulur."""
    ekler = GENISLETMELER.get(dil, GENISLETMELER["en"])[:3]
    return _plana_cevir([i + e for i in ifadeler for e in ekler])


def _oneri_al(istemci: httpx.Client, kaynak: str, sorgu: str, dil: str) -> list[str]:
    parametre = {**KAYNAKLAR[kaynak], "hl": dil, "q": sorgu}
    yanit = istemci.get(ADRES, params=parametre)
    yanit.raise_for_status()
    veri = yanit.json()
    if isinstance(veri, list) and len(veri) > 1 and isinstance(veri[1], list):
        return [str(x) for x in veri[1] if isinstance(x, str)]
    return []


def _topla(
    sorgular: list[tuple[str, str]], dil: str, istemci: httpx.Client | None, dislanan: set[str],
) -> tuple[dict[str, TrendKelime], list[str]]:
    kendi = istemci is None
    istemci = istemci or httpx.Client(timeout=8.0, headers={"User-Agent": "Mozilla/5.0"})

    def getir(kaynak_sorgu: tuple[str, str]):
        kaynak, sorgu = kaynak_sorgu
        try:
            return kaynak, sorgu, _oneri_al(istemci, kaynak, sorgu, dil)
        except (httpx.HTTPError, ValueError):
            return kaynak, sorgu, None

    try:
        with ThreadPoolExecutor(max_workers=4) as havuz:
            sonuclar = list(havuz.map(getir, sorgular))
    finally:
        if kendi:
            istemci.close()

    bulunan: dict[str, TrendKelime] = {}
    basarisiz = 0
    for kaynak, sorgu, oneriler in sonuclar:
        if oneriler is None:
            basarisiz += 1
            continue
        for oneri in oneriler:
            anahtar = normalle(oneri)
            if not anahtar or anahtar in dislanan:
                continue
            kayit = bulunan.setdefault(anahtar, TrendKelime(kelime=oneri.strip()))
            if kaynak not in kayit.kaynaklar:
                kayit.kaynaklar.append(kaynak)

    uyarilar: list[str] = []
    if sorgular and basarisiz == len(sorgular):
        uyarilar.append(
            "Trend verisine ulasilamadi (internet veya kaynak sorunu). "
            "Hashtag'ler yalnizca model onerisi olacak, dogrulanmadi."
        )
    elif basarisiz:
        uyarilar.append(f"{basarisiz}/{len(sorgular)} trend sorgusu basarisiz oldu; veri eksik olabilir.")
    return bulunan, uyarilar


def _sirala(bulunan: dict[str, TrendKelime], referans: list[str], azami: int) -> list[TrendKelime]:
    """Once konuyla ilgili olanlar, sonra iki kaynakta birden gecenler; esitlikte bulunma sirasi."""
    return sorted(
        bulunan.values(), key=lambda k: (-ilgi_puani(k.kelime, referans), -len(k.kaynaklar))
    )[:azami]


def konu_trendleri(
    konu: str, dil: str = "tr", *, istemci: httpx.Client | None = None, azami: int = 40,
) -> tuple[list[TrendKelime], list[str]]:
    """1. gecis: konudan genel tarama. Doner: (kelimeler, uyarilar). Asla istisna firlatmaz."""
    konu = konu.strip()
    if not konu:
        return [], []
    bulunan, uyarilar = _topla(sorgu_plani(konu, dil), dil, istemci, {normalle(konu)})
    sirali = _sirala(bulunan, icerik_sozcukleri(konu, dil), azami)
    ulasilamadi = bool(uyarilar) and "ulasilamadi" in uyarilar[0]
    if not ulasilamadi and len(sirali) < YETERSIZ_VERI:
        uyarilar.append(
            f"Az trend verisi bulundu ({len(sirali)} ifade). Konu çok spesifik olabilir; "
            "hashtag'ler büyük ölçüde model önerisi, doğrulanmadı."
        )
    return sirali, uyarilar


def ifade_trendleri(
    ifadeler: list[str], dil: str = "tr", *, istemci: httpx.Client | None = None, azami: int = 40,
) -> tuple[list[TrendKelime], list[str]]:
    """2. gecis: modelin SEO anahtar kelimeleriyle tarama. Asla istisna firlatmaz."""
    temiz = [i.strip() for i in ifadeler if len(i.strip()) >= IFADE_ASGARI_UZUNLUK][:4]
    if not temiz:
        return [], []
    bulunan, uyarilar = _topla(ifade_sorgu_plani(temiz, dil), dil, istemci, {normalle(i) for i in temiz})
    referans = [s for i in temiz for s in icerik_sozcukleri(i, dil)]
    return _sirala(bulunan, referans, azami), uyarilar


def etiket_haritasi(trendler: list[TrendKelime]) -> dict[str, list[str]]:
    """Etiket -> kaynaklar. Bir etiket, gercek bir arama onerisinin ardisik 1-3 sozcuklu
    parcasina karsilik geliyorsa dogrulanmis sayilir: oneri donuyorsa o ifade gercekten
    araniyor demektir. Hic oneri donmeyen (uydurma / cok nadir) bir kelime dogrulanamaz.
    Bu bir HACIM kaniti degildir; yalnizca 'insanlar bunu yaziyor' bilgisidir."""
    harita: dict[str, list[str]] = {}
    for t in trendler:
        parcalar = sozcuklere_ayir(t.kelime)
        for n in (1, 2, 3):
            for i in range(len(parcalar) - n + 1):
                etiket = etikete_cevir(" ".join(parcalar[i:i + n]))
                if etiket:
                    harita.setdefault(etiket, []).extend(t.kaynaklar)
    return harita
