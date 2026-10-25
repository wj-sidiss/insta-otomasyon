# -*- coding: utf-8 -*-
"""Reels kapagi (thumbnail) uretici.

1080x1920 (9:16) uretir. Profil izgarasi kapagi ortadan 4:5 kirptigi icin tum
onemli metin ortadaki 1080x1350'lik guvenli bantta tutulur.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

GENISLIK, YUKSEKLIK = 1080, 1920

_SABLON = """<!DOCTYPE html><html lang="tr"><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{width:1080px;height:1920px;background:#07070A;color:#fff;overflow:hidden;position:relative;
  font-family:'Segoe UI Variable','Segoe UI',system-ui,-apple-system,Arial,sans-serif}
.g1{position:absolute;width:1000px;height:1000px;border-radius:50%;top:-320px;right:-380px;
  background:radial-gradient(circle,rgba(99,102,241,.30),transparent 68%)}
.g2{position:absolute;width:800px;height:800px;border-radius:50%;bottom:-300px;left:-260px;
  background:radial-gradient(circle,rgba(16,185,129,.20),transparent 70%)}
.bant{position:absolute;left:0;right:0;top:285px;height:1350px;padding:0 84px;
  display:flex;flex-direction:column;justify-content:center}
.ust{font-size:30px;font-weight:800;letter-spacing:5px;text-transform:uppercase;color:#10B981;margin-bottom:34px}
h1{font-size:__BOYUT__px;font-weight:900;line-height:1.04;letter-spacing:-3px;word-break:break-word}
.alt{font-size:36px;color:#A3A3B2;line-height:1.4;margin-top:40px;font-weight:500}
.cizgi{width:120px;height:8px;border-radius:4px;margin-top:54px;
  background:linear-gradient(90deg,#6366F1,#10B981)}
.imza{position:absolute;bottom:300px;left:84px;font-size:28px;color:#6A6A7A;font-weight:700}
</style></head><body><div class="g1"></div><div class="g2"></div>
<div class="bant"><div class="ust">__UST__</div><h1>__BASLIK__</h1>__ALT__<div class="cizgi"></div></div>
__IMZA__</body></html>"""


def _baslik_boyutu(baslik: str) -> int:
    uzunluk = len(baslik)
    if uzunluk <= 28:
        return 118
    if uzunluk <= 50:
        return 96
    if uzunluk <= 80:
        return 78
    return 64


def kapak_html(ust: str, baslik: str, alt: str = "", imza: str = "") -> str:
    kac = html.escape  # model ciktisi guvenilmezdir: her sey kacirilir
    degerler = {
        "__BOYUT__": str(_baslik_boyutu(baslik)),
        "__UST__": kac(ust[:40]),
        "__BASLIK__": kac(baslik[:110]),
        "__ALT__": f'<div class="alt">{kac(alt[:140])}</div>' if alt else "",
        "__IMZA__": f'<div class="imza">{kac(imza[:40])}</div>' if imza else "",
    }
    # Tek gecis: eklenen metin tekrar taranmaz (model ciktisi yer tutucu icerebilir).
    return re.sub(r"__(?:BOYUT|UST|BASLIK|ALT|IMZA)__", lambda m: degerler[m.group(0)], _SABLON)


def kapak_uret(hedef: Path, ust: str, baslik: str, alt: str = "", imza: str = "") -> Path:
    """Kapagi JPEG olarak yazar. Playwright/Chromium yoksa RuntimeError firlatir."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as hata:
        raise RuntimeError(
            "Kapak icin Playwright gerekli: `pip install playwright` ve "
            "`python -m playwright install chromium`."
        ) from hata

    hedef = Path(hedef)
    hedef.parent.mkdir(parents=True, exist_ok=True)
    icerik = kapak_html(ust, baslik, alt, imza)
    try:
        with sync_playwright() as oynatici:
            tarayici = oynatici.chromium.launch()
            try:
                sayfa = tarayici.new_page(viewport={"width": GENISLIK, "height": YUKSEKLIK})
                sayfa.set_content(icerik, wait_until="load")
                sayfa.screenshot(path=str(hedef), type="jpeg", quality=95)
            finally:
                tarayici.close()
    except Exception as hata:  # tarayici kurulu degil vb.
        raise RuntimeError(
            f"Kapak cizilemedi: {hata}. `python -m playwright install chromium` deneyin."
        ) from hata
    return hedef
