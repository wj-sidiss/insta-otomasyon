# -*- coding: utf-8 -*-
"""SSRF savunmasi: Otopilot'un test tablosu (test/ag-guvenlik.test.js) Python'a tasindi + istemci davranisi."""
from __future__ import annotations

import time

import httpx
import pytest

from src.icerik import ag_guvenlik as ag
from src.icerik.ag_guvenlik import AgGuvenlikHatasi, ad_yerel_cozuluyor_mu, guvenli_istemci, yerel_adres_mi

YEREL = [
    "localhost", "LOCALHOST", "localhost.", "foo.localhost", "foo.localhost.", "127.0.0.1", "127.0.0.1.", "127.1",
    "0x7f000001", "0177.0.0.1", "2130706433", "0.0.0.0", "0",
    "10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.169.254", "100.64.0.1", "100.127.255.255",
    "198.18.0.1", "192.0.0.1", "192.0.2.1", "224.0.0.1", "255.255.255.255",
    "::", "::1", "[::1]", "::ffff:127.0.0.1", "[::ffff:7f00:1]", "[::ffff:a9fe:a9fe]", "[::ffff:10.0.0.1]", "::127.0.0.1",
    "::7f00:1", "[64:ff9b::7f00:1]", "[2002:7f00:1::]", "fe80::1", "fe80::1%eth0", "fd00::1", "fc00::1", "ff02::1", "2001:db8::1",
    "foo.local", "foo.local.", "db.internal", "metadata.google.internal.", "nas.lan", "x.home.arpa",
    "router", "intranet", "", "   ", "bozuk host adi",
]
GENEL = [
    "pexels.com", "commons.wikimedia.org", "upload.wikimedia.org", "8.8.8.8", "93.184.216.34", "172.15.0.1", "172.32.0.1",
    "100.63.255.255", "100.128.0.1", "198.17.0.1", "198.20.0.1", "192.0.1.1", "[2606:4700:4700::1111]",
    "2a00:1450:4001::200e", "::ffff:8.8.8.8", "xn--bcher-kva.example",
]


@pytest.mark.parametrize("host", YEREL)
def test_yerel_ozel_ve_ayrilmis_her_bicim_yakalanir(host):
    assert yerel_adres_mi(host) is True, repr(host)


@pytest.mark.parametrize("host", GENEL)
def test_herkese_acik_adresler_gecer(host):
    assert yerel_adres_mi(host) is False, host


def test_none_guvenli_tarafta_yerel_sayilir():
    assert yerel_adres_mi(None) is True


@pytest.mark.parametrize("host", ["cafe.be", "add.dead.example", "deadbeef.com", "0xg.example", "1.2.3.4.5.example"])
def test_onaltilik_harf_iceren_alan_adlari_ip_sanilmaz(host):
    assert yerel_adres_mi(host) is False, host


@pytest.mark.parametrize("metin,beklenen", [
    ("127.1", 0x7F000001), ("0x7f000001", 0x7F000001), ("0177.0.0.1", 0x7F000001), ("2130706433", 0x7F000001),
    ("10.1", 0x0A000001), ("1.2.3", 0x01020003), ("0", 0), ("255.255.255.255", 0xFFFFFFFF),
    ("256.1.1.1", None), ("1.2.3.4.5", None), ("1..2", None), ("0x", None), ("08", None), ("4294967296", None), ("1.2.3.65536", None),
])
def test_kisaltmali_ipv4_cozumleme(metin, beklenen):
    assert ag._ipv4_kisaltmali(metin) == beklenen


# ---------------------------------------------------------------- DNS

def _cozucu(tablo):
    def coz(ad):
        if ad not in tablo:
            raise OSError("ENOTFOUND")
        return tablo[ad]
    return coz


def test_yerel_adrese_cozulen_ad_engellenir_cozulemeyen_ya_da_acik_olan_engellenmez():
    c = _cozucu({"yerel.ornek": ["127.0.0.1"], "karisik.ornek": ["8.8.8.8", "::ffff:10.0.0.1"], "acik.ornek": ["93.184.216.34"]})
    assert ad_yerel_cozuluyor_mu("yerel.ornek", cozucu=c) is True
    assert ad_yerel_cozuluyor_mu("karisik.ornek", cozucu=c) is True          # biri yerelse hepsi reddedilir
    assert ad_yerel_cozuluyor_mu("acik.ornek", cozucu=c) is False
    assert ad_yerel_cozuluyor_mu("yok.ornek", cozucu=c) is False             # cozulemezse engellenmez
    for ip in ("8.8.8.8", "[::1]", "127.0.0.1"):
        assert ad_yerel_cozuluyor_mu(ip, cozucu=lambda ad: pytest.fail("IP degeri DNS'e gitmemeli")) is False


def test_fake_ip_modundaki_proxy_vpn_kullanicilari_reddedilmez_ama_dogrudan_ip_hala_engelli():
    assert ag.fake_ip_mi("198.18.0.5") and ag.fake_ip_mi("198.19.255.1") and not ag.fake_ip_mi("198.20.0.1")
    assert ad_yerel_cozuluyor_mu("ornek.example", cozucu=_cozucu({"ornek.example": ["198.18.0.5"]})) is False
    assert yerel_adres_mi("198.18.0.5") is True


def test_yavas_dns_beklemez(monkeypatch):
    monkeypatch.setattr(ag, "DNS_ZAMAN_ASIMI_SN", 0.1)
    t0 = time.monotonic()
    assert ad_yerel_cozuluyor_mu("yavas.ornek", cozucu=lambda ad: (time.sleep(2), ["127.0.0.1"])[1]) is False
    assert time.monotonic() - t0 < 1.0


def test_sistem_dns_sonucu_onbelleklenir(monkeypatch):
    sayac = []
    monkeypatch.setattr(ag, "_sistem_cozucu", lambda ad: sayac.append(ad) or ["93.184.216.34"])
    ag._dns_onbellek.clear()
    assert [ad_yerel_cozuluyor_mu("onbellek.ornek") for _ in range(3)] == [False] * 3
    assert sayac == ["onbellek.ornek"]
    ag._dns_onbellek.clear()


# ---------------------------------------------------------------- istemci

def _istemci(yanit=None, **kw):
    cagrilar = []

    def isleyici(istek):
        cagrilar.append(str(istek.url))
        return yanit(istek) if yanit else httpx.Response(200, text="ok")

    return guvenli_istemci(transport=httpx.MockTransport(isleyici), **kw), cagrilar


@pytest.mark.parametrize("adres", [
    "http://127.0.0.1/x", "http://[::ffff:127.0.0.1]:4399/x", "http://[::7f00:1]/x", "http://localhost./x", "http://router/x",
    "http://100.64.0.1/x", "http://[64:ff9b::7f00:1]/x", "http://127.1/x", "http://169.254.169.254/latest/meta-data/",
    "http://0x7f000001/x", "http://2130706433/x", "https://metadata.google.internal./x",
])
def test_istemci_yerel_adreslere_ag_cagrisi_yapmadan_gitmez(adres):
    istemci, cagrilar = _istemci()
    with pytest.raises(AgGuvenlikHatasi):
        istemci.get(adres)
    assert cagrilar == []


def test_istemci_yerele_cozulen_adi_engeller_acik_olani_gecirir():
    istemci, cagrilar = _istemci(cozucu=_cozucu({"localtest.example": ["127.0.0.1"], "guvenli.example": ["93.184.216.34"]}))
    with pytest.raises(AgGuvenlikHatasi, match="çözülen ad"):
        istemci.get("https://localtest.example/x")
    assert istemci.get("https://guvenli.example/x").status_code == 200 and cagrilar == ["https://guvenli.example/x"]


def test_acik_adresten_yerel_adrese_yonlendirme_durdurulur():
    def yanit(istek):
        if istek.url.host == "guvenli.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError("yerel adrese istek atildi")
    istemci, cagrilar = _istemci(yanit, cozucu=_cozucu({"guvenli.example": ["93.184.216.34"]}), follow_redirects=True)
    with pytest.raises(AgGuvenlikHatasi):
        istemci.get("https://guvenli.example/x")
    assert cagrilar == ["https://guvenli.example/x"]


def test_dns_denetimi_ortam_degiskeniyle_kapatilabilir_ama_ip_denetimi_kapanmaz(monkeypatch):
    monkeypatch.setenv("IC_YEREL_AG_IZIN", "1")
    istemci, cagrilar = _istemci(cozucu=_cozucu({"vpn.example": ["10.0.0.5"]}))
    assert istemci.get("https://vpn.example/x").status_code == 200           # DNS denetimi kapali
    for adres in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://foo.internal/x"):
        with pytest.raises(AgGuvenlikHatasi):
            istemci.get(adres)
    monkeypatch.delenv("IC_YEREL_AG_IZIN")
    with pytest.raises(AgGuvenlikHatasi):
        istemci.get("https://vpn.example/x")


def test_istemci_ek_olay_kancalarini_korur():
    gorulen = []
    istemci, _ = _istemci(cozucu=_cozucu({"a.example": ["93.184.216.34"]}), event_hooks={"request": [lambda i: gorulen.append(i.url.host)]})
    istemci.get("https://a.example/x")
    assert gorulen == ["a.example"]
