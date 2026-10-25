# -*- coding: utf-8 -*-
"""Kullanicinin kendi API anahtarlari.

Ilkeler:
  - Anahtarlar YALNIZCA bu bilgisayarda, `config/anahtarlar.json` dosyasinda duz metin olarak durur.
    Bize hicbir sey gonderilmez; bizim hesabimiz/anahtarimiz yoktur. Maliyet kullaniciya aittir.
  - Bir anahtar hicbir yanitta, hata mesajinda veya logda tam gorunmez (yalnizca maskeli son 4 hane).
  - Ortam degiskeni dosyadan once gelir (silinemez, yalnizca okunur).
Not: Dosya sifrelenmez. Paylasimli bir bilgisayarda isletim sistemi hesabinizin guvenligine guvenirsiniz.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Mapping

DOSYA_ADI = "anahtarlar.json"
ANAHTAR_DESENI = re.compile(r"^[A-Za-z0-9_.\-]{8,300}$")

SAGLAYICILAR: dict[str, dict] = {
    "pexels": {
        "ad": "Pexels",
        "ne_icin": "Stok video ve görsel arama",
        "ortam": "PEXELS_API_KEY",
        "anahtar_al": "https://www.pexels.com/api/",
        "maliyet": (
            "Ücretsiz. Varsayılan sınır: saatte 200, ayda 20.000 istek (Pexels belgesi). "
            "Pexels'e belirgin bir bağlantı vermeniz ve içerik sahibine kredi vermeniz istenir."
        ),
    },
    "pixabay": {
        "ad": "Pixabay",
        "ne_icin": "Stok video ve görsel arama",
        "ortam": "PIXABAY_API_KEY",
        "anahtar_al": "https://pixabay.com/api/docs/",
        "maliyet": (
            "Ücretsiz. Varsayılan sınır: 60 saniyede 100 istek; sonuçların 24 saat önbelleğe alınması gerekir. "
            "Kaynağı (Pixabay) kullanıcıya göstermeniz istenir."
        ),
    },
    "anthropic_api": {
        "ad": "Anthropic API",
        "ne_icin": "Yazı motoru (claude komutu yerine)",
        "ortam": "ANTHROPIC_API_KEY",
        "anahtar_al": "https://console.anthropic.com/",
        "maliyet": "Kullandıkça ödenir (ücretlidir). Maliyet size aittir; güncel fiyatlar için Anthropic'in fiyat sayfasına bakın.",
    },
}


class AnahtarHatasi(ValueError):
    """Gecersiz saglayici ya da anahtar. Mesaj anahtarin KENDISINI asla icermez."""


def maskele(deger: str) -> str:
    return "••••" if len(deger) < 12 else "•••• " + deger[-4:]


def varsayilan_dosya() -> Path:
    return Path(os.environ.get("IC_ANAHTAR_DOSYASI", Path("config") / DOSYA_ADI))


class AnahtarDeposu:
    def __init__(self, yol: Path | None = None, ortam: Mapping[str, str] | None = None) -> None:
        self.yol = Path(yol) if yol else varsayilan_dosya()
        self._ortam = os.environ if ortam is None else ortam
        self._kilit = threading.Lock()

    # ------------------------------------------------------------------ okuma

    def _dosyadan(self) -> dict[str, str]:
        try:
            veri = json.loads(self.yol.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(veri, dict):
            return {}
        return {k: v for k, v in veri.items() if k in SAGLAYICILAR and isinstance(v, str) and ANAHTAR_DESENI.match(v)}

    def getir(self, ad: str) -> str | None:
        """Anahtarin kendisi. YALNIZCA is mantigi icin; arayuze/loga gitmez."""
        if ad not in SAGLAYICILAR:
            return None
        ortamdan = (self._ortam.get(SAGLAYICILAR[ad]["ortam"]) or "").strip()
        if ortamdan:
            return ortamdan
        with self._kilit:
            return self._dosyadan().get(ad)

    def durum(self) -> list[dict]:
        """Arayuze verilen ozet: anahtar ASLA tam donmez."""
        with self._kilit:
            dosya = self._dosyadan()
        sonuc = []
        for ad, bilgi in SAGLAYICILAR.items():
            ortamdan = (self._ortam.get(bilgi["ortam"]) or "").strip()
            deger, kaynak = (ortamdan, "ortam") if ortamdan else (dosya.get(ad), "dosya" if ad in dosya else None)
            sonuc.append({
                "ad": ad,
                "baslik": bilgi["ad"],
                "ne_icin": bilgi["ne_icin"],
                "anahtar_al": bilgi["anahtar_al"],
                "maliyet": bilgi["maliyet"],
                "ortam_degiskeni": bilgi["ortam"],
                "tanimli": bool(deger),
                "kaynak": kaynak,                      # "ortam" | "dosya" | None
                "maske": maskele(deger) if deger else None,
            })
        return sonuc

    # ------------------------------------------------------------------ yazma

    def kaydet(self, ad: str, deger: str) -> None:
        if ad not in SAGLAYICILAR:
            raise AnahtarHatasi("Bilinmeyen sağlayıcı.")
        if not isinstance(deger, str) or not ANAHTAR_DESENI.match(deger.strip()):
            raise AnahtarHatasi(
                "Anahtar geçersiz: 8-300 karakter olmalı; yalnızca harf, rakam ve _ . - içerebilir (boşluk yok)."
            )
        with self._kilit:
            veri = self._dosyadan()
            veri[ad] = deger.strip()
            self._yaz(veri)

    def sil(self, ad: str) -> bool:
        if ad not in SAGLAYICILAR:
            raise AnahtarHatasi("Bilinmeyen sağlayıcı.")
        with self._kilit:
            veri = self._dosyadan()
            if ad not in veri:
                return False
            del veri[ad]
            self._yaz(veri)
            return True

    def _yaz(self, veri: dict[str, str]) -> None:
        self.yol.parent.mkdir(parents=True, exist_ok=True)
        gecici = self.yol.with_name(self.yol.name + ".tmp")
        gecici.write_text(json.dumps(veri, indent=2), encoding="utf-8")
        try:
            os.chmod(gecici, 0o600)               # Windows'ta etkisi sinirlidir
        except OSError:
            pass
        os.replace(gecici, self.yol)              # atomik: yarim dosya kalmaz
