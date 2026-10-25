# -*- coding: utf-8 -*-
"""Stok/acik kaynak arama sonucu (Aday) ve ortak hata turu."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass


class StokHatasi(RuntimeError):
    """Kullaniciya oldugu gibi gosterilir. Mesaj anahtari ASLA icermez.
    bekle_sn      : servis 'sonra dene' dediyse (429) onerilen bekleme.
    yedeklenebilir: indirmede bu adres olmadi ama yedek adres denenebilir (404, cok buyuk, gorsel degil ...)."""

    def __init__(self, mesaj: str, *, bekle_sn: float | None = None, yedeklenebilir: bool = False) -> None:
        super().__init__(mesaj)
        self.bekle_sn = bekle_sn
        self.yedeklenebilir = yedeklenebilir


@dataclass(frozen=True)
class Aday:
    kimlik: str
    kaynak: str
    tur: str                       # "video" | "gorsel"
    onizleme: str
    indirme: str
    sayfa: str
    kredi: str
    kredi_adresi: str
    genislik: int | None = None
    yukseklik: int | None = None
    sure_sn: int | None = None
    dikey: bool | None = True      # None: boyut bilinmiyor (ornegin NASA)
    baslik: str = ""
    lisans: str = ""               # kullaniciya gosterilen ad ("CC BY-SA 4.0", "Pexels License" ...)
    lisans_kodu: str = ""
    atif_gerekli: bool = False     # lisans yasal olarak atif ister (CC BY / CC BY-SA)
    atif_onerilen: bool = False
    uyari: str = ""
    yedek: tuple[str, ...] = ()    # indirme basarisiz olursa denenecek ek adresler (ornegin NASA ~orig -> ~large -> ~medium)

    def sozluk(self) -> dict:
        veri = asdict(self)
        veri["yedek"] = list(self.yedek)
        return veri


def aday_kimligi(kaynak: str, tur: str, kaynak_id: object) -> str:
    return hashlib.sha1(f"{kaynak}|{tur}|{kaynak_id}".encode("utf-8")).hexdigest()[:16]
