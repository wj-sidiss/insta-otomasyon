# -*- coding: utf-8 -*-
"""Yazi motoru adaptoru.

Kullanici kendi makinesindeki yapay zeka aracini kullanir; bizim anahtarimiz yok.
Desteklenenler:
  - claude_cli    : `claude` komutu (giris yapilmissa API anahtari gerekmez)
  - codex_cli     : `codex` komutu            (BU MAKINEDE TEST EDILMEDI: codex kurulu degil)
  - anthropic_api : ANTHROPIC_API_KEY ortam degiskeni
  - ornek_dosya   : IC_ORNEK_YANIT ile verilen JSON dosyasi (yalnizca test/demo)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .anahtarlar import AnahtarDeposu


class SaglayiciHatasi(RuntimeError):
    """Yazi motoru calismadi. Mesaj kullaniciya oldugu gibi gosterilir."""


class Saglayici(Protocol):
    ad: str
    aciklama: str

    def hazir_mi(self) -> tuple[bool, str]: ...

    def uret(self, sistem: str, istem: str, *, zaman_asimi: int = 300) -> str: ...


_GIRIS_IZLERI = ("authenticate", "oauth", "log in", "login", "unauthorized", "expired", "api key")


# Tek satir, ozel karaktersiz: komut satirindan guvenle gecer. Asil talimat stdin'den gider.
CLAUDE_KISA_SISTEM = "Yalnizca istenen JSON nesnesini dondur. Baska hicbir sey yazma."


def _alt_surec(komut: list[str], girdi: str, zaman_asimi: int, *, bos_klasorde: bool = False) -> str:
    """Komutu calistirir. bos_klasorde=True ise gecici BOS bir klasorden baslatir: arac,
    kullanicinin projesindeki veya kendi bagliminda bulunan dosyalari/hafizayi yuklemesin."""
    try:
        with tempfile.TemporaryDirectory(prefix="icerik-") as gecici:
            sonuc = subprocess.run(
                komut,
                input=girdi,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=zaman_asimi,
                cwd=gecici if bos_klasorde else None,
            )
    except subprocess.TimeoutExpired as hata:
        raise SaglayiciHatasi(
            f"Yazi motoru {zaman_asimi} saniyede cevap vermedi."
        ) from hata
    except OSError as hata:
        raise SaglayiciHatasi(f"Komut calistirilamadi: {hata}") from hata

    cikti = (sonuc.stdout or "").strip()
    hata_metni = (sonuc.stderr or "").strip()
    toplam = f"{cikti}\n{hata_metni}".lower()
    if sonuc.returncode != 0 or (not cikti and hata_metni):
        if any(iz in toplam for iz in _GIRIS_IZLERI):
            raise SaglayiciHatasi(
                "Yazi motoru oturumu gecersiz veya suresi dolmus. "
                "Bir terminalde ilgili aracin icinde yeniden giris yapin "
                "(Claude icin: `claude` yazip /login)."
            )
        ayrinti = (hata_metni or cikti)[:300]
        raise SaglayiciHatasi(f"Yazi motoru hata verdi (kod {sonuc.returncode}): {ayrinti}")
    # Bazi araclar giris hatasini cikis kodu 0 ile stdout'a yazar.
    if cikti and len(cikti) < 200 and any(iz in cikti.lower() for iz in _GIRIS_IZLERI):
        raise SaglayiciHatasi(
            "Yazi motoru oturumu gecersiz veya suresi dolmus. "
            "Bir terminalde ilgili aracin icinde yeniden giris yapin "
            "(Claude icin: `claude` yazip /login). "
            f"Arac mesaji: {cikti}"
        )
    return cikti


_OTURUM_ONBELLEK: dict[str, tuple[float, tuple[bool, str]]] = {}
_OTURUM_SURESI_SN = 30.0
GIRIS_YOK = "Kurulu ama giriş yapılmamış. Bir terminalde `claude` yazıp /login ile giriş yapın."


def _claude_oturumu(komut: str) -> tuple[bool, str]:
    """`claude auth status` ile gercek oturum durumunu sorar (30 sn onbellekli)."""
    simdi = time.monotonic()
    kayit = _OTURUM_ONBELLEK.get(komut)
    if kayit and simdi - kayit[0] < _OTURUM_SURESI_SN:
        return kayit[1]
    try:
        sonuc = subprocess.run(
            [komut, "auth", "status"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        )
        durum = json.loads(sonuc.stdout)
        if not isinstance(durum, dict) or "loggedIn" not in durum:
            raise ValueError("beklenmeyen bicim")
        cevap = (True, "Kurulu ve giriş yapılmış.") if durum["loggedIn"] else (False, GIRIS_YOK)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Surum farki vb.: kesin bilemiyoruz, engelleme ama durustce soyle.
        cevap = (True, "Kurulu (oturum durumu denetlenemedi, ilk kullanımda anlaşılır).")
    _OTURUM_ONBELLEK[komut] = (simdi, cevap)
    return cevap


@dataclass
class ClaudeCli:
    ad: str = "claude_cli"
    aciklama: str = "Claude Code CLI (`claude`)"

    def _komut(self) -> str | None:
        return shutil.which("claude")

    def hazir_mi(self) -> tuple[bool, str]:
        komut = self._komut()
        if not komut:
            return False, "`claude` komutu bulunamadi."
        return _claude_oturumu(komut)

    def uret(self, sistem: str, istem: str, *, zaman_asimi: int = 300) -> str:
        komut = self._komut()
        if not komut:
            raise SaglayiciHatasi("`claude` komutu bulunamadi.")
        # ONEMLI: Windows'ta `claude` bir .CMD kabugudur ve cmd.exe cok satirli bir argumani
        # ILK SATIRDA KESER. Bu yuzden talimatin HICBIR parcasi komut satirindan gonderilmez;
        # sistem talimati ve istem birlikte stdin'den verilir. Argumanlar tek satir ve ozel
        # karaktersizdir (cmd.exe'de & | < > ^ % anlam tasir).
        return _alt_surec(
            [
                komut, "-p",
                "--tools", "",
                "--no-session-persistence",
                "--output-format", "text",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--system-prompt", CLAUDE_KISA_SISTEM,
            ],
            f"{sistem}\n\n{istem}",
            zaman_asimi,
            bos_klasorde=True,
        )


@dataclass
class CodexCli:
    ad: str = "codex_cli"
    aciklama: str = "Codex CLI (`codex`) - test edilmedi"

    def _komut(self) -> str | None:
        return shutil.which("codex")

    def hazir_mi(self) -> tuple[bool, str]:
        if not self._komut():
            return False, "`codex` komutu bulunamadi."
        return True, "Kurulu (bu entegrasyon henuz gercek cihazda denenmedi)."

    def uret(self, sistem: str, istem: str, *, zaman_asimi: int = 300) -> str:
        komut = self._komut()
        if not komut:
            raise SaglayiciHatasi("`codex` komutu bulunamadi.")
        # Sistem talimati ve istem tek metinde birlestirilir; stdin'den okunur.
        return _alt_surec(
            [komut, "exec", "--skip-git-repo-check", "-"],
            f"{sistem}\n\n{istem}",
            zaman_asimi,
        )


@dataclass
class AnthropicApi:
    ad: str = "anthropic_api"
    aciklama: str = "Anthropic API (ANTHROPIC_API_KEY)"
    model: str = "claude-sonnet-5"

    def hazir_mi(self) -> tuple[bool, str]:
        if not AnahtarDeposu().getir("anthropic_api"):
            return False, "ANTHROPIC_API_KEY tanimli degil (ortam degiskeni ya da panelden girilen anahtar)."
        return True, "API anahtari tanimli (kullanim ucreti size aittir)."

    def uret(self, sistem: str, istem: str, *, zaman_asimi: int = 300) -> str:
        anahtar = AnahtarDeposu().getir("anthropic_api")
        if not anahtar:
            raise SaglayiciHatasi("ANTHROPIC_API_KEY tanimli degil (ortam degiskeni ya da panelden girilen anahtar).")
        try:
            yanit = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anahtar,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": os.environ.get("IC_MODEL", self.model),
                    "max_tokens": 6000,
                    "system": sistem,
                    "messages": [{"role": "user", "content": istem}],
                },
                timeout=zaman_asimi,
            )
        except httpx.HTTPError:
            raise SaglayiciHatasi("Anthropic API'ye ulasilamadi (internet veya servis sorunu).") from None
        if yanit.status_code in (401, 403):
            raise SaglayiciHatasi("API anahtari gecersiz veya yetkisiz.")
        if yanit.status_code >= 400:
            raise SaglayiciHatasi(f"API hatasi {yanit.status_code}: {yanit.text[:300]}")
        parcalar = yanit.json().get("content", [])
        return "".join(p.get("text", "") for p in parcalar if p.get("type") == "text").strip()


@dataclass
class OrnekDosya:
    """Testler ve gosterim icin: hazir bir JSON yanitini dosyadan dondurur."""

    ad: str = "ornek_dosya"
    aciklama: str = "Ornek yanit dosyasi (yalnizca test/demo)"

    def _yol(self) -> Path | None:
        yol = os.environ.get("IC_ORNEK_YANIT")
        return Path(yol) if yol else None

    def hazir_mi(self) -> tuple[bool, str]:
        yol = self._yol()
        if yol is None:
            return False, "IC_ORNEK_YANIT tanimli degil."
        return (yol.is_file(), "Hazir." if yol.is_file() else f"Dosya yok: {yol}")

    def uret(self, sistem: str, istem: str, *, zaman_asimi: int = 300) -> str:
        yol = self._yol()
        if yol is None or not yol.is_file():
            raise SaglayiciHatasi("Ornek yanit dosyasi bulunamadi.")
        return yol.read_text(encoding="utf-8")


def tum_saglayicilar() -> list[Saglayici]:
    return [ClaudeCli(), CodexCli(), AnthropicApi(), OrnekDosya()]


def saglayici_sec(ad: str = "auto") -> Saglayici:
    """'auto' ise hazir olan ilk saglayici; degilse adi verilen."""
    adaylar = tum_saglayicilar()
    if ad and ad != "auto":
        for aday in adaylar:
            if aday.ad == ad:
                hazir, neden = aday.hazir_mi()
                if not hazir:
                    raise SaglayiciHatasi(f"{aday.aciklama} kullanilamiyor: {neden}")
                return aday
        raise SaglayiciHatasi(f"Bilinmeyen yazi motoru: {ad}")
    nedenler = []
    for aday in adaylar:
        if aday.ad == "ornek_dosya":
            continue  # otomatik secimde asla kendiliginden secilmez
        hazir, neden = aday.hazir_mi()
        if hazir:
            return aday
        nedenler.append(f"- {aday.aciklama}: {neden}")
    raise SaglayiciHatasi(
        "Kullanılabilir yazı motoru yok.\n" + "\n".join(nedenler)
        + "\nBirini hazırlayın: `claude` girişi, `codex` kurulumu veya ANTHROPIC_API_KEY."
    )


def json_ayikla(metin: str) -> dict:
    """Modelin yanitindan ilk gecerli JSON nesnesini cikarir (kod blogu/aciklama olsa da)."""
    if not metin:
        raise ValueError("Bos yanit.")
    kodlayici = json.JSONDecoder()
    konum = 0
    while True:
        bas = metin.find("{", konum)
        if bas == -1:
            break
        try:
            nesne, _ = kodlayici.raw_decode(metin[bas:])
            if isinstance(nesne, dict):
                return nesne
        except json.JSONDecodeError:
            pass
        konum = bas + 1
    raise ValueError("Yanitta gecerli bir JSON nesnesi bulunamadi.")
