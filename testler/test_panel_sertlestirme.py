# -*- coding: utf-8 -*-
"""Panel sertlestirmesi (Otopilot'un panel-saldiri / panel-guvenlik derslerinden): katı CSP, satir ici betik yok,
Sec-Fetch-Site denetimi, varlik dosyalari."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.icerik.anahtarlar import AnahtarDeposu
from src.icerik.panel import uygulama_olustur
from src.icerik.saglayici import SaglayiciHatasi
from src.icerik.stok import Onbellek

ICERIK = Path(__file__).resolve().parent.parent / "src" / "icerik"
CAPRAZ = ["cross-site", "same-site"]
IZINLI = ["same-origin", "none"]


def _motor_yok(ad):
    raise SaglayiciHatasi("Kullanılabilir yazı motoru yok.")


@pytest.fixture
def panel(tmp_path):
    klasor = tmp_path / "paketler"
    klasor.mkdir()
    app = uygulama_olustur(
        paket_klasoru=klasor, saglayicilar=lambda: [], saglayici_bulucu=_motor_yok, izinli_hostlar=("testserver",),
        kapak_ciz=False, anahtar_depo=AnahtarDeposu(tmp_path / "k.json", ortam={}), stok_onbellek=Onbellek(),
    )
    return TestClient(app)


def _csp(panel) -> dict[str, list[str]]:
    ham = panel.get("/").headers["content-security-policy"]
    return {p.split()[0]: p.split()[1:] for p in (x.strip() for x in ham.split(";")) if p}


def test_csp_betik_ve_stil_yalnizca_kendi_sayfamizdan(panel):
    csp = _csp(panel)
    assert csp["script-src"] == ["'self'"] and csp["style-src"] == ["'self'"] and csp["default-src"] == ["'self'"]
    for yonerge, degerler in csp.items():
        assert "'unsafe-inline'" not in degerler and "'unsafe-eval'" not in degerler, yonerge
    for yonerge in ("object-src", "form-action", "frame-ancestors", "base-uri"):
        assert csp[yonerge] == ["'none'"], yonerge
    assert csp["connect-src"] == ["'self'"]
    assert "http:" not in " ".join(sum(csp.values(), []))                       # dis kaynaklar yalnizca https


def test_csp_her_yanitta_ve_hatalarda_da_vardir(panel):
    for yol in ("/", "/panel.js", "/panel.css", "/api/durum", "/yok-boyle-bir-yol"):
        assert "script-src 'self'" in panel.get(yol).headers.get("content-security-policy", ""), yol


def test_html_satir_ici_betik_stil_ya_da_olay_isleyicisi_icermez():
    html = (ICERIK / "panel.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\ssrc=)", html, re.I), "satir ici <script> var"
    assert "<style" not in html.lower() and not re.search(r"\sstyle\s*=", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I) and "javascript:" not in html.lower()
    assert re.findall(r'<script[^>]*src="([^"]+)"', html) == ["/panel.js"] and 'href="/panel.css"' in html


def test_varlik_dosyalari_dogru_turde_ve_guvenli_basliklarla_sunulur(panel):
    js, css = panel.get("/panel.js"), panel.get("/panel.css")
    assert js.status_code == css.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript") and css.headers["content-type"].startswith("text/css")
    for r in (js, css):
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "function sahneMedya" in js.text and ".medya" in css.text
    assert panel.post("/panel.js", json={}).status_code == 405                          # varliklar salt okunur


def test_js_dosyasi_guvenli_dom_kullanir_tehlikeli_arayuzlere_dokunmaz():
    js = (ICERIK / "panel.js").read_text(encoding="utf-8")
    for yasak in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute('style'",
                  'setAttribute("style"', "cssText", "localStorage", "sessionStorage", "javascript:", "document.cookie"):
        assert yasak not in js, yasak
    assert not re.search(r"set(Timeout|Interval)\(\s*['\"`]", js)                       # metinle betik calistirma yok
    assert "textContent" in js and "noopener noreferrer" in js and "protocol === 'https:'" in js


@pytest.mark.parametrize("sfs", CAPRAZ)
@pytest.mark.parametrize("yol", ["/api/durum", "/api/paketler", "/api/video-siteleri", "/api/anahtarlar", "/api/kaynaklar",
                                 "/paketler/20261025-120000-x/kapak.jpg", "/paketler/20261025-120000-x/medya/01-pexels-aaaa.mp4"])
def test_baska_siteden_gelen_api_ve_medya_istekleri_reddedilir(panel, sfs, yol):
    r = panel.get(yol, headers={"Sec-Fetch-Site": sfs})
    assert r.status_code == 403 and "başka bir siteden" in r.json()["hata"]


@pytest.mark.parametrize("sfs", IZINLI)
def test_ayni_kaynak_ve_kullanici_eylemi_serbest(panel, sfs):
    assert panel.get("/api/durum", headers={"Sec-Fetch-Site": sfs}).status_code == 200
    assert panel.get("/api/kaynaklar", headers={"Sec-Fetch-Site": sfs}).status_code == 200
    assert panel.get("/paketler/20261025-120000-x/kapak.jpg", headers={"Sec-Fetch-Site": sfs}).status_code == 404   # yetki degil, dosya yok


def test_tarayici_basligi_gondermeyen_istemciler_calisir(panel):
    assert panel.get("/api/durum").status_code == 200                                   # curl, betikler, testler


@pytest.mark.parametrize("sfs", CAPRAZ)
def test_sayfa_ve_statik_varliklar_baska_siteden_gezinilebilir(panel, sfs):
    for yol in ("/", "/panel.js", "/panel.css"):
        assert panel.get(yol, headers={"Sec-Fetch-Site": sfs, "Sec-Fetch-Mode": "navigate"}).status_code == 200, yol


@pytest.mark.parametrize("sfs", CAPRAZ)
@pytest.mark.parametrize("istek", [
    ("post", "/api/uret", {"konu": "saldiri konusu"}), ("put", "/api/anahtar/pexels", {"deger": "SIRRI-ANAHTAR-0123456789"}),
    ("delete", "/api/anahtar/pexels", None), ("delete", "/api/paket/20261025-120000-x", None),
    ("post", "/api/paket/20261025-120000-x/stok", {"sahne": 1, "tur": "gorsel", "kaynak": "wikimedia", "sorgu": "ab"}),
])
def test_baska_siteden_gelen_degistirici_istekler_Origin_olmasa_bile_reddedilir(panel, sfs, istek):
    yontem, yol, govde = istek
    r = getattr(panel, yontem)(yol, headers={"Sec-Fetch-Site": sfs}, **({"json": govde} if govde is not None else {}))
    assert r.status_code == 403
    assert panel.get("/api/anahtarlar").json()[0]["tanimli"] is False                  # hicbir yan etki olusmadi


def test_ayni_kaynaktan_degistirici_istekler_calisir(panel):
    govde = {"deger": "SIRRI-ANAHTAR-0123456789"}
    assert panel.put("/api/anahtar/pexels", json=govde, headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://testserver"}).status_code == 200
    assert panel.put("/api/anahtar/pexels", json=govde, headers={"Sec-Fetch-Site": "none"}).status_code == 200
    assert panel.delete("/api/anahtar/pexels", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_bilinmeyen_sec_fetch_degeri_guvenli_tarafta_reddedilir(panel):
    for deger in ("", "baska", "CROSS-SITE", "none;x", "same-origin,none"):
        assert panel.get("/api/durum", headers={"Sec-Fetch-Site": deger}).status_code == 403, repr(deger)


def test_degistirici_istek_yolu_ne_olursa_olsun_sec_fetch_ile_korunur(panel):
    """Gelecekte /api/ disina eklenecek bir POST/PUT/DELETE yolu da baska siteden gelen istege kapali olmali."""
    panel.app.add_api_route("/ek-uc", lambda: {"ok": True}, methods=["POST", "PUT", "DELETE"])
    for yontem in ("post", "put", "delete"):
        cagri = getattr(panel, yontem)
        kw = {"json": {}} if yontem != "delete" else {}
        assert cagri("/ek-uc", headers={"Sec-Fetch-Site": "cross-site"}, **kw).status_code == 403, yontem
        assert cagri("/ek-uc", headers={"Sec-Fetch-Site": "same-origin"}, **kw).status_code == 200, yontem
