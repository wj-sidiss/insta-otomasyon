# -*- coding: utf-8 -*-
"""Gorsel lisans suzgeci ve atif metni.

Mantik, Otopilot projesinin lib/gorsel-lisans.js dosyasindan (MIT) Python'a tasindi.

Kanal para kazanabilir: NC (ticari degil) ve ND (turetilemez) lisanslar ELENIR. Siralama (kod_sira):
CC0 / kamu mali / NASA / Pexels / Pixabay (0)  >  CC BY (1)  >  CC BY-SA (2)  >  tanimsiz (3).

atif_gerekli  : lisans yasal olarak ister (CC BY, CC BY-SA); aciklamaya KONMAZSA ihlal
atif_onerilen : yasal zorunluluk yok ama kaynagi/sahibini belirtmek nezaket ya da API yonergesi (Pexels, NASA, muzeler)

Bu bir hukuki gorus degildir; sahibi/kaynak sayfasindaki kosullar esastir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ETIKET = {"cc0": "CC0 1.0", "pdm": "Kamu malı (Public Domain)", "by": "CC BY", "by-sa": "CC BY-SA"}
_SIRA = {"cc0": 0, "pdm": 0, "by": 1, "by-sa": 2}


@dataclass(frozen=True)
class Lisans:
    kabul: bool
    lisans: str = ""            # kullaniciya gosterilen ad
    kod: str = ""               # cc0 | pdm | by | by-sa | pexels | pixabay | nasa | bilinmiyor
    sira: int = 0               # 0 en iyi ... 3 en kotu
    atif_gerekli: bool = False
    atif_onerilen: bool = False
    neden: str = ""             # kabul=False ise elenme nedeni
    uyari: str = ""


_SABIT = {
    "pexels": Lisans(True, "Pexels License", "pexels", 0, False, True),
    "pixabay": Lisans(True, "Pixabay License", "pixabay", 0, False, False),
    "nasa": Lisans(True, "NASA (kamu malı)", "nasa", 0, False, True),
    "met": Lisans(True, "CC0 (The Met Open Access)", "cc0", 0, False, True),
    "aic": Lisans(True, "CC0 (Art Institute of Chicago Public Domain)", "cc0", 0, False, True),
    "cleveland": Lisans(True, "CC0 (Cleveland Museum of Art Open Access)", "cc0", 0, False, True),
}


def _kod_sonuc(kod: str, surum: str = "") -> Lisans:
    etiket = ETIKET[kod] + (f" {surum}" if surum and kod in ("by", "by-sa") else "")
    zorunlu = kod in ("by", "by-sa")
    uyari = ""
    if kod == "by-sa":
        uyari = "CC BY-SA: video türev eser sayılabilir; açıklamaya atıf ekleyin ve aynı lisansı koruyun (ya da bu görseli kullanmayın)."
    return Lisans(True, etiket, kod, _SIRA[kod], zorunlu, zorunlu, uyari=uyari)


def wikimedia_lisans(kisa_ad: str, non_free: str = "") -> Lisans:
    """Wikimedia Commons extmetadata.LicenseShortName: "CC BY-SA 4.0", "CC0", "Public domain", "PD-old-70", "Attribution" ..."""
    a = (kisa_ad or "").strip()
    if str(non_free or "").strip().lower() == "true":
        return Lisans(False, neden="serbest olmayan (non-free) dosya")
    if re.search(r"(^|[^a-z])(nc|non[- ]?commercial)([^a-z]|$)", a, re.I):
        return Lisans(False, neden="ticari kullanıma kapalı (NC)")
    if re.search(r"(^|[^a-z])(nd|no[- ]?derivs?)([^a-z]|$)", a, re.I):
        return Lisans(False, neden="türetmeye kapalı (ND)")
    if re.match(r"gfdl", a, re.I):
        return Lisans(False, neden="yalnızca GFDL (video için uygun değil)")
    surum_eslesme = re.search(r"(\d\.\d)", a)
    surum = surum_eslesme.group(1) if surum_eslesme else ""
    if re.search(r"cc0|cc[- ]zero", a, re.I):
        return _kod_sonuc("cc0")
    if re.search(r"public domain|^pd([- ]|$)|pd-|no known copyright|usgov|pdm", a, re.I):
        return _kod_sonuc("pdm")
    if re.search(r"cc[- ]?by[- ]?sa|share ?alike", a, re.I):
        return _kod_sonuc("by-sa", surum)
    if re.search(r"cc[- ]?by|attribution", a, re.I):
        return _kod_sonuc("by", surum)
    # Commons yalnizca serbest lisansli dosya barindirir ama bu lisansi taniyamadik: kabul, ama atif iste, sona koy
    return Lisans(
        True, a or "Commons (serbest lisans, ad bilinmiyor)", "bilinmiyor", 3, True, True,
        uyari=f"Lisans adı tanınamadı ({a or 'boş'}): kaynak sayfasındaki koşulları kontrol edin.",
    )


def lisans_coz(kaynak: str, kisa_ad: str = "", non_free: str = "") -> Lisans:
    if kaynak in _SABIT:
        return _SABIT[kaynak]
    if kaynak == "wikimedia":
        return wikimedia_lisans(kisa_ad, non_free)
    return Lisans(False, neden=f"bilinmeyen kaynak: {kaynak}")


def _kisalt(metin: object, azami: int) -> str:
    return re.sub(r"\s+", " ", str(metin if metin is not None else "")).strip()[:azami]


def atif_metni(baslik: str, yazar: str, lisans: str, adres: str) -> str:
    """Tek atif satiri: "Baslik" - Yazar - Lisans - adres."""
    b = _kisalt(baslik, 160).replace('"', "'") or "Başlıksız"
    y = _kisalt(yazar, 120) or "Bilinmeyen"
    return f'"{b}" — {y} — {lisans} — {adres}'
