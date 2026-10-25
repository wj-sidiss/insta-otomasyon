# -*- coding: utf-8 -*-
"""Ag guvenligi: "bu adres yerel/ozel agda mi?" karari (SSRF savunmasi).

Mantik, Otopilot projesinin lib/ag-guvenlik.js dosyasindan (MIT, ayni sahibin projesi) Python'a tasindi ve
ayni test tablosuyla sinandi. Basit "127.0.0.1 / 10.x / 192.168.x" kontrolu yetmez; su bicimler de gecerdi:
  [::ffff:127.0.0.1] (IPv6'da gomulu IPv4), [::7f00:1], NAT64 / 6to4, "localhost." (sondaki nokta),
  127.1 / 0x7f000001 / 2130706433 / 0177.0.0.1 (kisaltmali IPv4), tek etiketli adlar (http://router/),
  100.64.0.0/10 (CGNAT), 198.18.0.0/15, 192.0.0.0/24, coklu yayin ve ayrilmis araliklar.
Alan adinin yerel adrese COZULMESI (localtest.me, 127.0.0.1.nip.io) ayrica `ad_yerel_cozuluyor_mu` ile denetlenir.

Bilinen sinir: ad sonradan baska bir adrese cozulurse (DNS rebinding) yakalanmaz.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as ZamanAsimiHatasi
from typing import Callable, Iterable

import httpx

YEREL_SONEKLER = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa", ".lan", ".intranet")
DNS_ZAMAN_ASIMI_SN = 3.0
DNS_ONBELLEK_SN = 60.0
YEREL_AG_IZIN_ORTAM = "IC_YEREL_AG_IZIN"


class AgGuvenlikHatasi(RuntimeError):
    """Istek yerel/ozel bir aga gidecegi icin engellendi. Mesaj kullaniciya gosterilebilir (adres disinda sir icermez)."""


def _ipv4_yerel(a: int, b: int, c: int) -> bool:
    return (
        a == 0 or a == 10 or a == 127 or a >= 224
        or (a == 100 and 64 <= b <= 127)                    # CGNAT
        or (a == 169 and b == 254)                          # link-local, bulut metadata (169.254.169.254)
        or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
        or (a == 192 and b == 0 and c in (0, 2)) or (a == 192 and b == 88 and c == 99)
        or (a == 198 and b in (18, 19)) or (a == 198 and b == 51 and c == 100) or (a == 203 and b == 0 and c == 113)
    )


def ipv4_yerel(deger: int) -> bool:
    return _ipv4_yerel((deger >> 24) & 255, (deger >> 16) & 255, (deger >> 8) & 255)


def _ipv6_yerel(ip: ipaddress.IPv6Address) -> bool:
    g = [int.from_bytes(ip.packed[i:i + 2], "big") for i in range(0, 16, 2)]

    def sifir(*k: int) -> bool:
        return all(g[i] == 0 for i in k)

    def v4(ust: int, alt: int) -> bool:
        return _ipv4_yerel(ust >> 8, ust & 255, alt >> 8)

    if sifir(0, 1, 2, 3, 4, 5, 6) and g[7] in (0, 1):
        return True                                          # :: ve ::1
    if sifir(0, 1, 2, 3, 4) and g[5] == 0xFFFF:
        return v4(g[6], g[7])                                # IPv4-eslemeli ::ffff:a.b.c.d
    if sifir(0, 1, 2, 3, 4, 5):
        return True                                          # IPv4-uyumlu ::a.b.c.d (kullanimdan kalkti)
    if g[0] == 0x64 and g[1] == 0xFF9B:
        return v4(g[6], g[7]) if sifir(2, 3, 4, 5) else True  # NAT64 64:ff9b::/96
    if g[0] == 0x2002:
        return v4(g[1], g[2])                                # 6to4
    if g[0] == 0x2001 and g[1] in (0, 0xDB8):
        return True                                          # Teredo, dokumantasyon
    if g[0] == 0x100 and sifir(1, 2, 3):
        return True                                          # 100::/64 (discard)
    return (g[0] & 0xFE00) == 0xFC00 or (g[0] & 0xFFC0) == 0xFE80 or (g[0] & 0xFFC0) == 0xFEC0 or (g[0] & 0xFF00) == 0xFF00


def _ipv4_kisaltmali(metin: str) -> int | None:
    """inet_aton semantigi: a, a.b, a.b.c, a.b.c.d; her parca ondalik / 0ile baslayan sekizlik / 0x onaltilik.
    Isletim sistemine bagli olmasin diye elle yazildi. Gecersizse None."""
    parcalar = metin.split(".")
    if not 1 <= len(parcalar) <= 4:
        return None
    sayilar: list[int] = []
    for p in parcalar:
        if not p:
            return None
        try:
            if p[:2].lower() == "0x":
                n = int(p[2:], 16)
            elif len(p) > 1 and p[0] == "0":
                n = int(p, 8)
            elif p.isdigit():
                n = int(p, 10)
            else:
                return None
        except ValueError:
            return None
        sayilar.append(n)
    *bas, son = sayilar
    if any(b > 255 for b in bas) or son >= 256 ** (4 - len(bas)):
        return None
    deger = son
    for i, b in enumerate(bas):
        deger += b << (8 * (3 - i))
    return deger


def yerel_adres_mi(host: object) -> bool:
    """host: URL.hostname (IPv6 koseli olabilir) ya da ham ad/IP. Gecersiz/bos -> True (guvenli taraf)."""
    a = str(host if host is not None else "").strip()
    if a.startswith("["):
        a = a[1:]
    if a.endswith("]"):
        a = a[:-1]
    a = a.lower()
    if "%" in a:
        return True                                          # IPv6 bolge kimligi (fe80::1%eth0)
    a = a.rstrip(".")                                        # 'localhost.' ve '127.0.0.1.' mutlak alan adlari ayni adrestir
    if not a:
        return True
    if ":" in a:
        try:
            return _ipv6_yerel(ipaddress.IPv6Address(a))
        except ValueError:
            return True
    if re.fullmatch(r"[0-9a-fx.]+", a):
        sayi = _ipv4_kisaltmali(a)
        if sayi is not None:
            return ipv4_yerel(sayi)
    if a == "localhost" or any(a.endswith(s) for s in YEREL_SONEKLER):
        return True
    return "." not in a                                      # tek etiketli ad (http://router/): arama son ekiyle yerel makineye cozulur


def fake_ip_mi(adres: str) -> bool:
    """Proxy/VPN "fake-ip" DNS modu (Clash, Surge ...): her ad 198.18.0.0/15'e cozulur ve baglanti proxy uzerinden gercek
    sunucuya gider. DNS'ten cozulen adreste bu aralik yerel aga gidildigi anlamina gelmez; yoksa bu uygulamalari kullanan
    herkesin TUM istekleri reddedilirdi. Dogrudan IP olarak yazilan 198.18.x.x hala engellidir (yerel_adres_mi)."""
    return re.fullmatch(r"198\.(18|19)\.\d{1,3}\.\d{1,3}", str(adres or "").strip()) is not None


_dns_onbellek: dict[str, tuple[float, bool]] = {}
_dns_kilit = threading.Lock()


def _sistem_cozucu(ad: str) -> list[str]:
    return [bilgi[4][0] for bilgi in socket.getaddrinfo(ad, None)]


def ad_yerel_cozuluyor_mu(
    host: str, *, cozucu: Callable[[str], Iterable[str]] | None = None, saat: Callable[[], float] = time.monotonic,
) -> bool:
    """Alan adi yerel bir adrese cozuluyor mu? Yalnizca POZITIF sonuc engeller (cozumleme basarisizsa istegi yapan taraf
    kendi hatasini verir). 60 sn onbellekli (cevrimdisiyken her istek beklemesin). IP degerleri DNS'e gitmez."""
    a = str(host or "").strip().strip("[]").rstrip(".").lower()
    if not a or ":" in a or re.fullmatch(r"[\d.]+", a):
        return False
    enjekte = cozucu is not None
    if not enjekte:
        with _dns_kilit:
            kayit = _dns_onbellek.get(a)
        if kayit and saat() - kayit[0] < DNS_ONBELLEK_SN:
            return kayit[1]
    yerel = False
    havuz = ThreadPoolExecutor(max_workers=1)
    try:
        adresler = havuz.submit(cozucu or _sistem_cozucu, a).result(timeout=DNS_ZAMAN_ASIMI_SN)
        yerel = any(not fake_ip_mi(x) and yerel_adres_mi(x) for x in adresler)
    except (ZamanAsimiHatasi, OSError, ValueError):
        yerel = False
    finally:
        havuz.shutdown(wait=False)
    if not enjekte:
        with _dns_kilit:
            if len(_dns_onbellek) > 500:
                _dns_onbellek.clear()
            _dns_onbellek[a] = (saat(), yerel)
    return yerel


def dns_denetimi_kapali_mi() -> bool:
    """VPN/kurumsal DNS yuzunden her istek reddediliyorsa IC_YEREL_AG_IZIN=1 YALNIZ ad cozumleme denetimini kapatir.
    Dogrudan IP olarak yazilan yerel adresler ve .local/.internal adlari yine reddedilir."""
    return os.environ.get(YEREL_AG_IZIN_ORTAM, "").strip().lower() in ("1", "true", "evet", "yes")


def adresi_denetle(host: str, *, cozucu: Callable[[str], Iterable[str]] | None = None) -> None:
    """Yerel/ozel agdaysa AgGuvenlikHatasi firlatir."""
    if yerel_adres_mi(host):
        raise AgGuvenlikHatasi(f"Yerel ağ adreslerine gidilmez: {host}")
    if not dns_denetimi_kapali_mi() and ad_yerel_cozuluyor_mu(host, cozucu=cozucu):
        raise AgGuvenlikHatasi(
            f"Yerel ağ adresine çözülen ad engellendi: {host} "
            f"(VPN/proxy kullanıyorsanız {YEREL_AG_IZIN_ORTAM}=1 ortam değişkeni DNS denetimini kapatır)."
        )


def guvenli_istemci(*, cozucu: Callable[[str], Iterable[str]] | None = None, **ayar) -> httpx.Client:
    """Her istekte (yonlendirmeler dahil) hedef adresi denetleyen httpx istemcisi."""
    def denetle(istek: httpx.Request) -> None:
        adresi_denetle(istek.url.host, cozucu=cozucu)

    kancalar = dict(ayar.pop("event_hooks", {}) or {})
    kancalar["request"] = [denetle, *kancalar.get("request", [])]
    return httpx.Client(event_hooks=kancalar, **ayar)
