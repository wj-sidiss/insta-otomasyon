# -*- coding: utf-8 -*-
"""localhost paneli (FastAPI). Yalnizca 127.0.0.1'e baglanir.

Guvenlik notu: localhost'ta calisan bir servise tarayicidaki HERHANGI bir site istek
atabilir. Bu yuzden:
  - Host basligi izinli listede olmali (DNS rebinding'e karsi)
  - Veri degistiren isteklerde Origin yerel olmali (siteler arasi istege karsi)
  - POST govdesi JSON olmali (form ile gonderilemez)
  - Dosya yollari yalnizca dogrulanmis paket kimligiyle olusturulur
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import stok as stok_modulu
from .anahtarlar import AnahtarDeposu, AnahtarHatasi
from .paket import (
    MEDYA_DOSYA_DESENI, gecerli_id_mi, medya_kaldir, medya_kaydet, paket_olustur, paketi_oku, paketi_sil,
    paketleri_listele,
)
from .saglayici import Saglayici, SaglayiciHatasi, saglayici_sec, tum_saglayicilar
from .stok import ANAHTARLI, KAYNAK_ADI, KAYNAK_BILGI, Onbellek, StokHatasi
from .trend import ifade_trendleri, konu_trendleri
from .video_siteleri import katalog

PANEL_HTML = Path(__file__).with_name("panel.html")
PANEL_JS = Path(__file__).with_name("panel.js")
PANEL_CSS = Path(__file__).with_name("panel.css")
IS_SAKLAMA_SINIRI = 50


class UretIstegi(BaseModel):
    konu: str = Field(min_length=3, max_length=300)
    dil: Literal["tr", "en"] = "tr"
    ton: Literal["samimi", "uzman", "enerjik", "ciddi"] = "samimi"
    saglayici: str = Field(default="auto", max_length=40)


class StokIstegi(BaseModel):
    sahne: int = Field(ge=1, le=12)
    tur: Literal["video", "gorsel"]
    kaynak: Literal["pexels", "pixabay", "wikimedia", "nasa", "met", "aic", "cleveland"]
    sorgu: str = Field(min_length=2, max_length=100)


class MedyaIstegi(BaseModel):
    sahne: int = Field(ge=1, le=12)
    aday_kimligi: str = Field(pattern=r"^[0-9a-f]{16}$")


class AnahtarIstegi(BaseModel):
    deger: str = Field(min_length=8, max_length=300)


MEDYA_TURU = {".mp4": "video/mp4", ".webm": "video/webm", ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
# Betik ve stil YALNIZCA kendi sayfamizdan ('unsafe-inline' YOK): bir yerde HTML enjeksiyonu olsa bile satir ici betik / olay
# isleyicisi calismaz (savunma derinligi; arayuz zaten innerHTML kullanmaz). Onizleme gorselleri/videolari dis kaynaklardan
# (https) gelir, baska hicbir sey gelmez.
GUVENLIK_POLITIKASI = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; media-src 'self' https:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'"
)
FETCH_SITE_IZINLI = ("same-origin", "none")


def uygulama_olustur(
    *,
    paket_klasoru: Path,
    saglayici_bulucu: Callable[[str], Saglayici] = saglayici_sec,
    saglayicilar: Callable[[], list[Saglayici]] = tum_saglayicilar,
    izinli_hostlar: tuple[str, ...] = ("127.0.0.1", "localhost"),
    kapak_ciz: bool = True,
    trend_getir=konu_trendleri,
    ifade_getir=ifade_trendleri,
    imza: str = "",
    anahtar_depo: AnahtarDeposu | None = None,
    stok_istemci: httpx.Client | None = None,
    stok_onbellek: Onbellek | None = None,
) -> FastAPI:
    klasor = Path(paket_klasoru)
    uygulama = FastAPI(title="Icerik Studyosu", docs_url=None, redoc_url=None, openapi_url=None)
    uygulama.add_middleware(TrustedHostMiddleware, allowed_hosts=list(izinli_hostlar))

    isler: dict[str, dict] = {}
    kilit = threading.Lock()
    depo = anahtar_depo or AnahtarDeposu()
    onbellek = stok_onbellek or stok_modulu.ONBELLEK

    @uygulama.exception_handler(RequestValidationError)
    async def dogrulama_hatasi(istek: Request, hata: RequestValidationError):
        # Varsayilan yanit girdiyi (ornegin API anahtarini) geri yansitir: genel mesaj donuyoruz.
        return JSONResponse({"detail": "Geçersiz girdi."}, status_code=422)

    @uygulama.middleware("http")
    async def kaynak_denetimi(istek: Request, devam):
        # Tarayici Sec-Fetch-Site gonderiyorsa: API/medya GET'leri ve tum degistirici istekler yalnizca ayni kaynaktan ya da
        # dogrudan kullanici eyleminden ('none') gelebilir. Sayfanin kendisi (ust duzey gezinti) serbesttir.
        fetch_site = istek.headers.get("sec-fetch-site")
        hassas = istek.method not in ("GET", "HEAD", "OPTIONS") or istek.url.path.startswith(("/api/", "/paketler/"))
        if fetch_site is not None and fetch_site not in FETCH_SITE_IZINLI and hassas:
            return JSONResponse({"hata": "İstek başka bir siteden geldi; reddedildi."}, status_code=403)
        if istek.method not in ("GET", "HEAD", "OPTIONS"):
            kaynak = istek.headers.get("origin")
            if kaynak:
                host = (urlparse(kaynak).hostname or "").lower()
                if host not in izinli_hostlar:
                    return JSONResponse({"hata": "Bu kaynaktan istek kabul edilmez."}, status_code=403)
            tur = istek.headers.get("content-type", "")
            if istek.method in ("POST", "PUT", "PATCH") and not tur.startswith("application/json"):
                return JSONResponse({"hata": "Icerik turu application/json olmali."}, status_code=415)
        yanit = await devam(istek)
        yanit.headers["X-Content-Type-Options"] = "nosniff"
        yanit.headers["Referrer-Policy"] = "no-referrer"
        yanit.headers["Cache-Control"] = "no-store"
        yanit.headers["Content-Security-Policy"] = GUVENLIK_POLITIKASI
        return yanit

    @uygulama.get("/")
    def ana_sayfa():
        return FileResponse(PANEL_HTML, media_type="text/html; charset=utf-8")

    @uygulama.get("/panel.js")
    def panel_js():
        return FileResponse(PANEL_JS, media_type="text/javascript; charset=utf-8")

    @uygulama.get("/panel.css")
    def panel_css():
        return FileResponse(PANEL_CSS, media_type="text/css; charset=utf-8")

    @uygulama.get("/api/durum")
    def durum():
        liste = []
        for s in saglayicilar():
            hazir, neden = s.hazir_mi()
            liste.append({"ad": s.ad, "aciklama": s.aciklama, "hazir": hazir, "neden": neden})
        try:
            varsayilan = saglayici_bulucu("auto").ad
        except SaglayiciHatasi:
            varsayilan = None
        return {"saglayicilar": liste, "varsayilan": varsayilan, "instagram": {"bagli": False}}

    def _calistir(is_id: str, istek: UretIstegi) -> None:
        durum_kaydi = isler[is_id]

        def ilerleme(adim: str, mesaj: str) -> None:
            durum_kaydi["adimlar"].append({"adim": adim, "mesaj": mesaj})

        try:
            saglayici = saglayici_bulucu(istek.saglayici)
            paket = paket_olustur(
                istek.konu, saglayici=saglayici, klasor=klasor, dil=istek.dil, ton=istek.ton,
                imza=imza, kapak_ciz=kapak_ciz, ilerleme=ilerleme, trend_getir=trend_getir,
                ifade_getir=ifade_getir,
            )
            durum_kaydi["paket_id"] = paket["id"]
            durum_kaydi["durum"] = "bitti"
        except (SaglayiciHatasi, ValueError) as hata:
            durum_kaydi["hata"] = str(hata)
            durum_kaydi["durum"] = "hata"
        except Exception as hata:  # beklenmeyen: kullaniciya sade mesaj
            durum_kaydi["hata"] = f"Beklenmeyen hata: {type(hata).__name__}: {hata}"
            durum_kaydi["durum"] = "hata"

    @uygulama.post("/api/uret", status_code=202)
    def uret(istek: UretIstegi):
        with kilit:
            if any(i["durum"] == "calisiyor" for i in isler.values()):
                raise HTTPException(409, "Bir üretim zaten sürüyor, bitmesini bekleyin.")
            is_id = uuid.uuid4().hex[:12]
            isler[is_id] = {"durum": "calisiyor", "adimlar": [], "paket_id": None, "hata": None}
            while len(isler) > IS_SAKLAMA_SINIRI:
                isler.pop(next(iter(isler)))
        threading.Thread(target=_calistir, args=(is_id, istek), daemon=True).start()
        return {"is_id": is_id}

    @uygulama.get("/api/is/{is_id}")
    def is_durumu(is_id: str):
        kayit = isler.get(is_id)
        if kayit is None:
            raise HTTPException(404, "İş bulunamadı.")
        return kayit

    @uygulama.get("/api/paketler")
    def paketler():
        return paketleri_listele(klasor)

    @uygulama.get("/api/paket/{paket_id}")
    def paket_getir(paket_id: str):
        paket = paketi_oku(klasor, paket_id)
        if paket is None:
            raise HTTPException(404, "Paket bulunamadı.")
        return paket

    @uygulama.delete("/api/paket/{paket_id}")
    def paket_sil(paket_id: str):
        if not paketi_sil(klasor, paket_id):
            raise HTTPException(404, "Paket bulunamadı.")
        return {"silindi": paket_id}

    def _dosya(paket_id: str, ad: str, tur: str, indir: bool = False):
        if not gecerli_id_mi(paket_id):
            raise HTTPException(404, "Dosya bulunamadı.")
        yol = klasor / paket_id / ad
        if not yol.is_file():
            raise HTTPException(404, "Dosya bulunamadı.")
        return FileResponse(yol, media_type=tur, filename=ad if indir else None,
                            content_disposition_type="attachment" if indir else "inline")

    @uygulama.get("/paketler/{paket_id}/kapak.jpg")
    def kapak(paket_id: str):
        return _dosya(paket_id, "kapak.jpg", "image/jpeg")

    @uygulama.get("/paketler/{paket_id}/kopyala.txt")
    def kopyala(paket_id: str):
        return _dosya(paket_id, "kopyala.txt", "text/plain; charset=utf-8", indir=True)

    # ------------------------------------------------------------------ video siteleri / API anahtarlari

    @uygulama.get("/api/video-siteleri")
    def video_siteleri():
        return katalog()

    @uygulama.get("/api/kaynaklar")
    def kaynaklar():
        """Stok/gorsel kaynaklari: arayuz secimleri bu listeden kurulur (anahtar degeri asla donmez)."""
        return [
            {"ad": ad, **bilgi, "anahtar_tanimli": bool(depo.getir(ad)) if bilgi["anahtar_gerekli"] else None}
            for ad, bilgi in KAYNAK_BILGI.items()
        ]

    @uygulama.get("/api/anahtarlar")
    def anahtarlar():
        return depo.durum()           # anahtarin kendisi asla donmez, yalnizca maske

    @uygulama.put("/api/anahtar/{ad}")
    def anahtar_kaydet(ad: str, govde: AnahtarIstegi):
        try:
            depo.kaydet(ad, govde.deger)
        except AnahtarHatasi as hata:
            raise HTTPException(400, str(hata)) from None
        return {"ad": ad, "tanimli": True}      # deger YANKILANMAZ

    @uygulama.delete("/api/anahtar/{ad}")
    def anahtar_sil(ad: str):
        try:
            silindi = depo.sil(ad)
        except AnahtarHatasi as hata:
            raise HTTPException(404, str(hata)) from None
        if not silindi:
            raise HTTPException(404, "Dosyada kayıtlı bir anahtar yok (ortam değişkeninden geliyorsa oradan kaldırın).")
        return {"silindi": ad}

    # ------------------------------------------------------------------ stok video / gorsel

    @uygulama.post("/api/paket/{paket_id}/stok")
    def stok_ara(paket_id: str, istek: StokIstegi):
        if paketi_oku(klasor, paket_id) is None:
            raise HTTPException(404, "Paket bulunamadı.")
        anahtar = depo.getir(istek.kaynak) if istek.kaynak in ANAHTARLI else None
        if istek.kaynak in ANAHTARLI and not anahtar:
            raise HTTPException(
                400, f"{KAYNAK_ADI[istek.kaynak]} için API anahtarı tanımlı değil. 'API anahtarları' bölümünden ekleyin."
            )
        try:
            adaylar = stok_modulu.ara(istek.kaynak, istek.tur, istek.sorgu, anahtar, istemci=stok_istemci, onbellek=onbellek)
        except StokHatasi as hata:
            raise HTTPException(400, str(hata)) from None
        return {
            "kaynak": istek.kaynak, "kaynak_adi": KAYNAK_ADI[istek.kaynak], "kaynak_adresi": KAYNAK_BILGI[istek.kaynak]["adres"],
            "adaylar": [a.sozluk() for a in adaylar],
        }

    @uygulama.post("/api/paket/{paket_id}/medya")
    def medya_sec(paket_id: str, istek: MedyaIstegi):
        paket = paketi_oku(klasor, paket_id)
        if paket is None:
            raise HTTPException(404, "Paket bulunamadı.")
        if istek.sahne > len(paket.get("senaryo", [])):
            raise HTTPException(400, "Böyle bir sahne yok.")
        # Yalnizca bizim aramamizin dondurdugu adaylar indirilir: istemci URL gonderemez.
        aday = onbellek.aday_getir(istek.aday_kimligi)
        if aday is None:
            raise HTTPException(404, "Aday bulunamadı veya süresi doldu; aramayı tekrarlayın.")
        onceki = (paket.get("medya") or {}).get(str(istek.sahne))
        try:
            yol = stok_modulu.indir(aday, klasor / paket_id / "medya", istek.sahne, istemci=stok_istemci)
        except StokHatasi as hata:
            raise HTTPException(400, str(hata)) from None
        kayit = {
            "kaynak": aday.kaynak, "tur": aday.tur, "aday_kimligi": aday.kimlik, "dosya": yol.name,
            "sayfa": aday.sayfa, "kredi": aday.kredi, "kredi_adresi": aday.kredi_adresi,
            "kredi_metni": stok_modulu.kredi_metni(aday), "atif": stok_modulu.atif_satiri(aday), "baslik": aday.baslik,
            "lisans": aday.lisans, "lisans_kodu": aday.lisans_kodu, "atif_gerekli": aday.atif_gerekli, "uyari": aday.uyari,
            "genislik": aday.genislik, "yukseklik": aday.yukseklik, "sure_sn": aday.sure_sn,
        }
        medya_kaydet(klasor, paket_id, istek.sahne, kayit)
        eski_dosya = (onceki or {}).get("dosya", "")
        if eski_dosya and eski_dosya != yol.name and MEDYA_DOSYA_DESENI.fullmatch(eski_dosya):
            (klasor / paket_id / "medya" / eski_dosya).unlink(missing_ok=True)
        return kayit

    @uygulama.delete("/api/paket/{paket_id}/medya/{sahne}")
    def medya_kaldir_ucu(paket_id: str, sahne: int):
        if not medya_kaldir(klasor, paket_id, sahne):
            raise HTTPException(404, "Bu sahnede seçili medya yok.")
        return {"silindi": sahne}

    @uygulama.get("/paketler/{paket_id}/medya/{dosya}")
    def medya_dosyasi(paket_id: str, dosya: str):
        if not gecerli_id_mi(paket_id) or not MEDYA_DOSYA_DESENI.fullmatch(dosya):
            raise HTTPException(404, "Dosya bulunamadı.")
        yol = klasor / paket_id / "medya" / dosya
        if not yol.is_file():
            raise HTTPException(404, "Dosya bulunamadı.")
        return FileResponse(yol, media_type=MEDYA_TURU[yol.suffix.lower()], content_disposition_type="inline")

    return uygulama
