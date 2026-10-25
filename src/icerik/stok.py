# -*- coding: utf-8 -*-
"""Stok video/gorsel arama ve indirme.

Anahtarli kaynaklar (KULLANICININ KENDI anahtariyla): Pexels, Pixabay  -> video + gorsel
Anahtarsiz acik kaynaklar (yalniz gorsel): Wikimedia Commons, NASA, The Met, Art Institute of Chicago, Cleveland

Guvenlik ve kurallar:
  - Anahtar hicbir hata mesajina/loga girmez (Pixabay anahtari URL'dedir; httpx hata metni onu sizdirabilir).
  - Indirme YALNIZCA bu modulun kendi aramasinin dondurdugu adaylardan yapilir (istemciden keyfi URL alinmaz); adres https
    olmali ve O KAYNAGIN sunucu izin listesinde olmali (yonlendirmeden sonra da). Ayrica varsayilan istemci yerel/ozel
    aga (SSRF) gitmez (ag_guvenlik.py). Boyut sinirlidir.
  - Pixabay kurali: sonuclar 24 saat onbellege alinir. Kaynak, kredi ve LISANS bilgisi adayla birlikte tasinir.
  - 429 alinan kaynak bir sure "sogutulur": kullanici ust uste tiklayip servisi dovmez.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

import httpx

from . import acik_kaynaklar as acik
from .acik_kaynaklar import ACIK_KAYNAKLAR, SOGUTUCU, Sogutucu
from .ag_guvenlik import AgGuvenlikHatasi, guvenli_istemci
from .aday import Aday, StokHatasi, aday_kimligi
from .lisans import atif_metni, lisans_coz

PEXELS_VIDEO = "https://api.pexels.com/videos/search"
PEXELS_FOTO = "https://api.pexels.com/v1/search"
PIXABAY_FOTO = "https://pixabay.com/api/"
PIXABAY_VIDEO = "https://pixabay.com/api/videos/"

ANAHTARLI = ("pexels", "pixabay")
VIDEO_KAYNAKLARI = ("pexels", "pixabay")
KAYNAKLAR = ANAHTARLI + ACIK_KAYNAKLAR
TURLER = ("video", "gorsel")

KAYNAK_BILGI: dict[str, dict] = {
    "pexels": {"baslik": "Pexels", "anahtar_gerekli": True, "video": True, "adres": "https://www.pexels.com",
               "aciklama": "Stok video ve fotoğraf (ücretsiz API anahtarı gerekir)"},
    "pixabay": {"baslik": "Pixabay", "anahtar_gerekli": True, "video": True, "adres": "https://pixabay.com",
                "aciklama": "Stok video ve fotoğraf (ücretsiz API anahtarı gerekir)"},
    "wikimedia": {"baslik": "Wikimedia Commons", "anahtar_gerekli": False, "video": False, "adres": "https://commons.wikimedia.org",
                  "aciklama": "Anahtarsız görsel arşivi. Lisans dosyaya göre değişir: CC BY / CC BY-SA ATIF ister, NC/ND olanlar elenir."},
    "nasa": {"baslik": "NASA", "anahtar_gerekli": False, "video": False, "adres": "https://images.nasa.gov",
             "aciklama": "Anahtarsız uzay/bilim/havacılık görselleri (kamu malı). Üçüncü taraf telifli olanlar elenir."},
    "met": {"baslik": "The Met", "anahtar_gerekli": False, "video": False, "adres": "https://www.metmuseum.org",
            "aciklama": "Anahtarsız sanat ve tarih görselleri (yalnızca CC0 / kamu malı)."},
    "aic": {"baslik": "Art Institute of Chicago", "anahtar_gerekli": False, "video": False, "adres": "https://www.artic.edu",
            "aciklama": "Anahtarsız sanat görselleri (yalnızca kamu malı)."},
    "cleveland": {"baslik": "Cleveland Museum of Art", "anahtar_gerekli": False, "video": False, "adres": "https://www.clevelandart.org",
                  "aciklama": "Anahtarsız sanat görselleri (yalnızca CC0)."},
}
KAYNAK_ADI = {k: v["baslik"] for k, v in KAYNAK_BILGI.items()}
IZINLI_ALAN_ADLARI = {
    "pexels": ("pexels.com",), "pixabay": ("pixabay.com",), "wikimedia": ("wikimedia.org",), "nasa": ("nasa.gov",),
    "met": ("metmuseum.org",), "aic": ("artic.edu",), "cleveland": ("clevelandart.org",),
}

SORGU_AZAMI = 100            # Pixabay belgesi: q en fazla 100 karakter
VIDEO_AZAMI_BAYT = 150 * 1024 * 1024
GORSEL_AZAMI_BAYT = 25 * 1024 * 1024
ONBELLEK_SURESI_SN = 24 * 3600
VARSAYILAN_SOGUMA_SN = 60.0
PROJE_ADRESI = "https://github.com/wj-sidiss/insta-otomasyon"
# Wikimedia'nin User-Agent politikasi: istemci adi/surumu + ILETISIM BILGISI (URL veya e-posta) sart; yoksa 403 verir.
# AIC'nin bot korumasi ise UA'da 'python-httpx' gibi kutuphane adi gecince 403 verir: iletisim VAR, kutuphane adi YOK.
# Her kurulum kendi iletisimini IC_ILETISIM ile verebilir (ornegin e-posta); varsayilan proje adresidir.
# Indirmede ekstra baslik isteyen kaynaklar: AIC gorsel sunucusu da AIC-User-Agent olmadan 403 verir.
INDIRME_BASLIKLARI = {"aic": acik.AIC_BASLIGI}


def kullanici_ajani() -> str:
    iletisim = os.environ.get("IC_ILETISIM", "").strip() or PROJE_ADRESI
    iletisim = re.sub(r"[^\w@.:/+\-~ ]", "", iletisim)[:120]            # baslik enjeksiyonu (CR/LF vb.) olmasin
    return f"IcerikStudyosu/1.0 ({iletisim}; acik kaynak masaustu araci)"


class Onbellek:
    """Arama sonuclari ve aday kayitlari icin 24 saatlik bellek onbellegi."""

    def __init__(self, sure_sn: float = ONBELLEK_SURESI_SN, saat=time.monotonic) -> None:
        self._sure, self._saat = sure_sn, saat
        self._aramalar: dict[tuple, tuple[float, list[Aday]]] = {}
        self._adaylar: dict[str, tuple[float, Aday]] = {}
        self._kilit = threading.Lock()

    def arama_getir(self, anahtar: tuple) -> list[Aday] | None:
        with self._kilit:
            kayit = self._aramalar.get(anahtar)
            if kayit and self._saat() - kayit[0] < self._sure:
                return kayit[1]
            self._aramalar.pop(anahtar, None)
            return None

    def arama_koy(self, anahtar: tuple, adaylar: list[Aday]) -> None:
        with self._kilit:
            simdi = self._saat()
            self._aramalar[anahtar] = (simdi, adaylar)
            for a in adaylar:
                self._adaylar[a.kimlik] = (simdi, a)

    def aday_getir(self, kimlik: str) -> Aday | None:
        with self._kilit:
            kayit = self._adaylar.get(kimlik)
            if kayit and self._saat() - kayit[0] < self._sure:
                return kayit[1]
            self._adaylar.pop(kimlik, None)
            return None


ONBELLEK = Onbellek()


# ------------------------------------------------------------------ yardimcilar

def _alan_adi_izinli(adres: str, kaynak: str) -> bool:
    ayrik = urlparse(adres)
    host = (ayrik.hostname or "").lower()
    izinli = IZINLI_ALAN_ADLARI.get(kaynak, ())
    return (
        ayrik.scheme == "https" and not ayrik.username
        and any(host == a or host.endswith("." + a) for a in izinli)
    )


def _int(deger) -> int | None:
    return int(deger) if isinstance(deger, (int, float)) and not isinstance(deger, bool) and deger > 0 else None


def _slug(adres: str) -> str:
    """https://www.pexels.com/photo/brown-bear-on-grass-2014422/ -> 'brown bear on grass'"""
    son = [p for p in urlparse(adres or "").path.split("/") if p][-1:] or [""]
    return re.sub(r"[-_]+", " ", re.sub(r"-\d+$", "", son[0])).strip()


def bekleme_suresi(basliklar: Mapping[str, str], simdi: float | None = None) -> float | None:
    """429 yanitinda beklenecek sure (sn); basliklardan okunamazsa None. Retry-After (sayi ya da HTTP tarihi) ve
    Pexels'in X-Ratelimit-Reset (UNIX saniyesi) basligini okur."""
    simdi = time.time() if simdi is None else simdi
    ra = basliklar.get("retry-after")
    if ra is not None:
        try:
            return max(0.0, float(ra))
        except ValueError:
            try:
                tarih = parsedate_to_datetime(ra)
                if tarih.tzinfo is None:
                    tarih = tarih.replace(tzinfo=timezone.utc)
                return max(0.0, tarih.timestamp() - simdi)
            except (TypeError, ValueError):
                pass
    sifirlama = basliklar.get("x-ratelimit-reset")
    if sifirlama is not None:
        try:
            s = float(sifirlama)
            return max(0.0, s - simdi) if s > 1e9 else max(0.0, s)
        except ValueError:
            pass
    return None


def _en_iyi_pexels_dosyasi(dosyalar: list[dict]) -> dict | None:
    """9:16 icin: genisligi 1080'e en yakin ama asmayan mp4; yoksa en kucuk buyuk olan."""
    mp4 = [d for d in dosyalar if d.get("file_type") == "video/mp4" and isinstance(d.get("link"), str) and _int(d.get("width"))]
    kucukler = [d for d in mp4 if d["width"] <= 1080]
    if kucukler:
        return max(kucukler, key=lambda d: d["width"])
    return min(mp4, key=lambda d: d["width"]) if mp4 else None


def _en_iyi_pixabay_dosyasi(videolar: dict) -> dict | None:
    for ad in ("medium", "small", "large", "tiny"):          # medium ~1080p
        d = videolar.get(ad) if isinstance(videolar, dict) else None
        if isinstance(d, dict) and isinstance(d.get("url"), str) and d["url"] and _int(d.get("width")):
            return d
    return None


def _lisans_alanlari(kaynak: str) -> dict:
    l = lisans_coz(kaynak)
    return {"lisans": l.lisans, "lisans_kodu": l.kod, "atif_gerekli": l.atif_gerekli, "atif_onerilen": l.atif_onerilen}


# ------------------------------------------------------------------ ayristirma (Pexels / Pixabay)

def _pexels_video(veri: dict) -> list[Aday]:
    sonuc = []
    for v in veri.get("videos", []):
        dosya = _en_iyi_pexels_dosyasi(v.get("video_files", []) if isinstance(v, dict) else [])
        if not dosya or not isinstance(v.get("url"), str):
            continue
        kullanici = v.get("user") if isinstance(v.get("user"), dict) else {}
        sonuc.append(Aday(
            kimlik=aday_kimligi("pexels", "video", v.get("id")), kaynak="pexels", tur="video",
            onizleme=str(v.get("image") or ""), indirme=dosya["link"], sayfa=v["url"],
            kredi=str(kullanici.get("name") or ""), kredi_adresi=str(kullanici.get("url") or ""),
            genislik=_int(dosya.get("width")), yukseklik=_int(dosya.get("height")), sure_sn=_int(v.get("duration")),
            dikey=bool(_int(dosya.get("height")) and _int(dosya.get("width")) and dosya["height"] > dosya["width"]),
            baslik=_slug(v["url"]), **_lisans_alanlari("pexels"),
        ))
    return sonuc


def _pexels_foto(veri: dict) -> list[Aday]:
    sonuc = []
    for f in veri.get("photos", []):
        src = f.get("src") if isinstance(f, dict) and isinstance(f.get("src"), dict) else {}
        indirme = src.get("large2x") or src.get("large") or src.get("original")
        if not indirme or not isinstance(f.get("url"), str):
            continue
        sonuc.append(Aday(
            kimlik=aday_kimligi("pexels", "gorsel", f.get("id")), kaynak="pexels", tur="gorsel",
            onizleme=str(src.get("medium") or src.get("small") or indirme), indirme=indirme, sayfa=f["url"],
            kredi=str(f.get("photographer") or ""), kredi_adresi=str(f.get("photographer_url") or ""),
            genislik=_int(f.get("width")), yukseklik=_int(f.get("height")),
            dikey=bool(_int(f.get("height")) and _int(f.get("width")) and f["height"] > f["width"]),
            baslik=str(f.get("alt") or "") or _slug(f["url"]), **_lisans_alanlari("pexels"),
        ))
    return sonuc


def _pixabay_foto(veri: dict) -> list[Aday]:
    sonuc = []
    for h in veri.get("hits", []):
        indirme = h.get("largeImageURL") or h.get("webformatURL")
        if not indirme or not isinstance(h.get("pageURL"), str):
            continue
        sonuc.append(Aday(
            kimlik=aday_kimligi("pixabay", "gorsel", h.get("id")), kaynak="pixabay", tur="gorsel",
            onizleme=str(h.get("webformatURL") or h.get("previewURL") or indirme), indirme=indirme, sayfa=h["pageURL"],
            kredi=str(h.get("user") or ""), kredi_adresi=h["pageURL"],
            genislik=_int(h.get("imageWidth")), yukseklik=_int(h.get("imageHeight")),
            dikey=bool(_int(h.get("imageHeight")) and _int(h.get("imageWidth")) and h["imageHeight"] > h["imageWidth"]),
            baslik=str(h.get("tags") or "") or _slug(h["pageURL"]), **_lisans_alanlari("pixabay"),
        ))
    return sonuc


def _pixabay_video(veri: dict) -> list[Aday]:
    sonuc = []
    for h in veri.get("hits", []):
        dosya = _en_iyi_pixabay_dosyasi(h.get("videos"))
        if not dosya or not isinstance(h.get("pageURL"), str):
            continue
        sonuc.append(Aday(
            kimlik=aday_kimligi("pixabay", "video", h.get("id")), kaynak="pixabay", tur="video",
            onizleme=str(dosya.get("thumbnail") or ""), indirme=dosya["url"], sayfa=h["pageURL"],
            kredi=str(h.get("user") or ""), kredi_adresi=h["pageURL"],
            genislik=_int(dosya.get("width")), yukseklik=_int(dosya.get("height")), sure_sn=_int(h.get("duration")),
            dikey=bool(_int(dosya.get("height")) and _int(dosya.get("width")) and dosya["height"] > dosya["width"]),
            baslik=str(h.get("tags") or "") or _slug(h["pageURL"]), **_lisans_alanlari("pixabay"),
        ))
    return sonuc


# ------------------------------------------------------------------ HTTP

def _json_getir(istemci: httpx.Client, kaynak: str, url: str, params: dict | None, basliklar: dict | None) -> dict:
    """Tek GET + hata esleme + JSON ayristirma. Her hata kullaniciya gosterilebilir StokHatasi'dir (anahtar sizmaz)."""
    ad = KAYNAK_ADI[kaynak]
    # User-Agent HER istekte gonderilir (disaridan verilen istemcide de): Wikimedia iletisim bilgisi, AIC kutuphane adinin
    # OLMAMASINI ister; bu kural istemcinin varsayilanina birakilamayacak kadar onemlidir.
    basliklar = {"User-Agent": kullanici_ajani(), **(basliklar or {})}
    try:
        yanit = istemci.get(url, params=params, headers=basliklar)
    except AgGuvenlikHatasi as hata:
        raise StokHatasi(str(hata)) from None                      # zincir yok: URL/anahtar sizmasin
    except httpx.HTTPError:
        raise StokHatasi(f"{ad}'e ulaşılamadı (internet veya servis sorunu).") from None
    govde = ""
    try:
        govde = yanit.text[:200]
    except Exception:
        pass
    durum = yanit.status_code
    anahtarli = kaynak in ANAHTARLI
    if anahtarli and (durum in (401, 403) or (kaynak == "pixabay" and durum == 400 and "api key" in govde.lower())):
        raise StokHatasi(f"{ad} API anahtarı geçersiz ya da yetkisiz.")
    if durum == 429:
        raise StokHatasi(f"{ad} istek sınırı aşıldı; biraz bekleyip tekrar deneyin.", bekle_sn=bekleme_suresi(yanit.headers))
    if durum in (401, 403):
        raise StokHatasi(f"{ad} erişimi reddetti ({durum}).")
    if durum >= 500:
        raise StokHatasi(f"{ad} şu an yanıt vermiyor ({durum}).")
    if durum >= 400:
        raise StokHatasi(f"{ad} isteği reddetti ({durum}).")
    try:
        veri = yanit.json()
        if not isinstance(veri, dict):
            raise ValueError
    except ValueError:
        raise StokHatasi(f"{ad} beklenmeyen bir yanıt döndürdü.") from None
    return veri


def _anahtarli_ara(kaynak: str, tur: str, sorgu: str, anahtar: str, adet: int, getir) -> list[Aday]:
    if kaynak == "pexels":
        adres = PEXELS_VIDEO if tur == "video" else PEXELS_FOTO
        parametre = {"query": sorgu, "orientation": "portrait", "per_page": adet}
        basliklar = {"Authorization": anahtar}
    else:
        adres = PIXABAY_VIDEO if tur == "video" else PIXABAY_FOTO
        # Pixabay videolarinda yonelim filtresi yok: daha fazla isteyip dikeyleri one aliyoruz.
        parametre = {"key": anahtar, "q": sorgu, "safesearch": "true", "per_page": max(adet, 3) * (3 if tur == "video" else 1)}
        if tur == "gorsel":
            parametre.update({"orientation": "vertical", "image_type": "photo"})
        basliklar = None
    veri = getir(adres, parametre, basliklar)
    ayristir = {("pexels", "video"): _pexels_video, ("pexels", "gorsel"): _pexels_foto,
                ("pixabay", "video"): _pixabay_video, ("pixabay", "gorsel"): _pixabay_foto}[(kaynak, tur)]
    adaylar = ayristir(veri)
    adaylar.sort(key=lambda a: not a.dikey)            # dikey olanlar once (kararli siralama)
    return adaylar[:adet]


# ------------------------------------------------------------------ arama

def ara(
    kaynak: str, tur: str, sorgu: str, anahtar: str | None = None, *, adet: int = 6,
    istemci: httpx.Client | None = None, onbellek: Onbellek | None = None, sogutucu: Sogutucu | None = None,
) -> list[Aday]:
    if kaynak not in KAYNAKLAR or tur not in TURLER:
        raise StokHatasi("Bilinmeyen kaynak veya tür.")
    ad = KAYNAK_ADI[kaynak]
    if tur == "video" and kaynak not in VIDEO_KAYNAKLARI:
        raise StokHatasi(f"{ad} yalnızca görsel arar; video için Pexels veya Pixabay kullanın.")
    sorgu = " ".join((sorgu or "").split())
    if len(sorgu) < 2 or len(sorgu) > SORGU_AZAMI:
        raise StokHatasi(f"Arama ifadesi 2-{SORGU_AZAMI} karakter olmalı.")
    if kaynak in ANAHTARLI and not anahtar:
        raise StokHatasi(f"{ad} için API anahtarı tanımlı değil.")
    adet = max(1, min(int(adet), 20))
    onbellek = onbellek or ONBELLEK

    onbellek_anahtari = (kaynak, tur, sorgu.lower(), adet)
    hazir = onbellek.arama_getir(onbellek_anahtari)
    if hazir is not None:
        return hazir

    sogutucu = sogutucu or SOGUTUCU
    sogutucu.bekle(kaynak, ad)                         # sogutmadaysa hemen StokHatasi; degilse en kisa araligi bekler
    kendi = istemci is None
    istemci = istemci or guvenli_istemci(timeout=15.0, headers={"User-Agent": kullanici_ajani()})

    def getir(url: str, params: dict | None = None, basliklar: dict | None = None) -> dict:
        if kaynak in ACIK_KAYNAKLAR:
            sogutucu.bekle(kaynak, ad)                 # ek istekler (ornegin Met nesneleri) de nazik aralikla gider
        return _json_getir(istemci, kaynak, url, params, basliklar)

    try:
        if kaynak in ANAHTARLI:
            adaylar = _anahtarli_ara(kaynak, tur, sorgu, str(anahtar), adet, getir)
        else:
            adaylar = acik.ara(kaynak, sorgu, adet, lambda url, params=None, basliklar=None: getir(url, params, basliklar))
    except StokHatasi as hata:
        if hata.bekle_sn is not None or "istek sınırı aşıldı" in str(hata):
            sogutucu.sogut(kaynak, hata.bekle_sn if hata.bekle_sn is not None else VARSAYILAN_SOGUMA_SN)
        raise
    finally:
        if kendi:
            istemci.close()
    onbellek.arama_koy(onbellek_anahtari, adaylar)
    return adaylar


# ------------------------------------------------------------------ indirme

_UZANTI = {"video/mp4": ".mp4", "video/webm": ".webm", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _indir_tek(istemci: httpx.Client, adres: str, aday: Aday, hedef_klasor: Path, azami: int) -> tuple[Path, str]:
    """Tek adresi indirir. Doner: (gecici dosya, icerik turu). Hata: StokHatasi (yedeklenebilir olabilir)."""
    gecici = hedef_klasor / f".indiriliyor-{os.getpid()}-{threading.get_ident()}"
    try:
        with istemci.stream("GET", adres, follow_redirects=True, headers={"User-Agent": kullanici_ajani(), **INDIRME_BASLIKLARI.get(aday.kaynak, {})}) as yanit:
            if not _alan_adi_izinli(str(yanit.url), aday.kaynak):
                raise StokHatasi("İndirme beklenmeyen bir adrese yönlendirildi, durduruldu.")
            if yanit.status_code >= 400:
                raise StokHatasi(f"İndirme başarısız ({yanit.status_code}).", yedeklenebilir=yanit.status_code in (403, 404))
            tur = (yanit.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not (tur.startswith(("video/", "image/")) or tur == "application/octet-stream"):
                raise StokHatasi("İndirilen içerik video veya görsel değil.", yedeklenebilir=True)
            uzunluk = yanit.headers.get("content-length")
            if uzunluk and uzunluk.isdigit() and int(uzunluk) > azami:
                raise StokHatasi("Dosya izin verilen boyuttan büyük.", yedeklenebilir=True)
            toplam = 0
            with open(gecici, "wb") as f:
                for parca in yanit.iter_bytes(64 * 1024):
                    toplam += len(parca)
                    if toplam > azami:
                        raise StokHatasi("Dosya izin verilen boyuttan büyük, durduruldu.", yedeklenebilir=True)
                    f.write(parca)
            if toplam == 0:
                raise StokHatasi("Boş dosya indirildi.", yedeklenebilir=True)
        return gecici, tur
    except AgGuvenlikHatasi as hata:
        gecici.unlink(missing_ok=True)
        raise StokHatasi(str(hata)) from None
    except httpx.HTTPError:
        gecici.unlink(missing_ok=True)
        raise StokHatasi("İndirme sırasında bağlantı hatası oluştu.") from None
    except StokHatasi:
        gecici.unlink(missing_ok=True)
        raise


def indir(aday: Aday, hedef_klasor: Path, sahne_no: int, *, istemci: httpx.Client | None = None) -> Path:
    """Adayi hedef klasore indirir. Ana adres olmazsa (404, cok buyuk ...) yedek adresleri sirayla dener.
    Doner: dosya yolu. Hata: StokHatasi."""
    adresler = [aday.indirme, *aday.yedek]
    if not all(_alan_adi_izinli(a, aday.kaynak) for a in adresler):    # hicbiri izin disi olmamali: yedek de kapi disi acmaz
        raise StokHatasi("İndirme adresi izin verilen kaynaklardan biri değil.")
    azami = VIDEO_AZAMI_BAYT if aday.tur == "video" else GORSEL_AZAMI_BAYT
    kendi = istemci is None
    istemci = istemci or guvenli_istemci(timeout=60.0, headers={"User-Agent": kullanici_ajani()})
    hedef_klasor = Path(hedef_klasor)
    hedef_klasor.mkdir(parents=True, exist_ok=True)
    try:
        son_hata: StokHatasi | None = None
        for adres in adresler:
            try:
                gecici, tur = _indir_tek(istemci, adres, aday, hedef_klasor, azami)
                break
            except StokHatasi as hata:
                son_hata = hata
                if not hata.yedeklenebilir:
                    raise
        else:
            raise son_hata or StokHatasi("İndirilemedi.")
    finally:
        if kendi:
            istemci.close()

    uzanti = _UZANTI.get(tur) or (".mp4" if aday.tur == "video" else ".jpg")
    guvenli_kimlik = re.sub(r"[^a-z0-9]", "", aday.kimlik.lower())[:16]
    hedef = hedef_klasor / f"{int(sahne_no):02d}-{aday.kaynak}-{guvenli_kimlik}{uzanti}"
    os.replace(gecici, hedef)
    return hedef


def kredi_metni(aday: Aday) -> str:
    ad = KAYNAK_ADI.get(aday.kaynak, aday.kaynak)
    tur = "Video" if aday.tur == "video" else "Fotoğraf"
    return f"{tur}: {aday.kredi or 'bilinmeyen'} / {ad}"


def atif_satiri(aday: Aday) -> str:
    """"Baslik" - Yazar - Lisans - adres (CC BY / CC BY-SA icin aciklamaya konmasi gereken satir)."""
    return atif_metni(aday.baslik or kredi_metni(aday), aday.kredi, aday.lisans or "lisans belirtilmemiş", aday.sayfa)
