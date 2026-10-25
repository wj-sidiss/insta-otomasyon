# -*- coding: ascii -*-
"""Meta Graph API istemcisi: Instagram Reels / Story yayin akisi.

Yol: "Instagram API with Facebook Login".
  - Graph cagrilari : https://graph.facebook.com/<API_SURUM>
  - Video yukleme   : https://rupload.facebook.com/ig-api-upload/<API_SURUM>
  - Token           : Facebook Page access token (uzun omurlu)
  - Izinler         : instagram_basic, instagram_content_publish,
                      pages_read_engagement (+ Sayfa rolu Business Manager
                      uzerinden verildiyse ads_management / ads_read)

Yayin akisi (4 adim, hicbir bulut depolama katmani YOK):
  1) konteyner_olustur()  POST /<IG_ID>/media  (media_type + upload_type=resumable)
  2) dosya_yukle()        POST rupload.../<KONTEYNER_ID>  (ham binary, offset ile devam)
  3) durum_bekle()        GET  /<KONTEYNER_ID>?fields=status_code,status -> FINISHED
  4) yayinla()            POST /<IG_ID>/media_publish?creation_id=<KONTEYNER_ID>

KRITIK KURALLAR (bozulursa sistem sessizce yanlis calisir):
  - rupload tarafinda Authorization semasi "OAuth <token>"; "Bearer" DEGIL.
  - rupload baslik adlari birebir kucuk harf: "offset" ve "file_size".
    file_size HER ZAMAN dosyanin TOPLAM boyutudur, kalan bayt sayisi degildir.
  - Govde ham binary gonderilir (multipart/form-data veya files= YANLIS).
  - upload_type=resumable ile YEREL dosya yuklenirken video_url GONDERILMEZ.
  - HTTP 200 yayinlandi demek DEGILDIR; status_code alani mutlaka okunmalidir.
  - media_publish ASLA otomatik tekrar denenmez (ayni icerigi iki kez yayinlama
    riski). Cevapsiz kalirsa once konteyner durumu / son medya izi kontrol edilir.
  - Sadece VIDEO yayinlanir. JPEG gorseller rupload ile yuklenemez; gorsel icin
    Meta public bir image_url ister. Gorseller medya katmaninda ffmpeg ile kisa
    MP4'e cevrilip STORIES videosu olarak gonderilir.
  - Token hicbir log satirina yazilmaz (bkz. _gizle).

Turkce karakter kullanilmaz (ASCII), konsol encoding sorunu cikmasin diye.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from src.modeller import (
    CAPTION_MAKS_ETIKET,
    CAPTION_MAKS_HASHTAG,
    CAPTION_MAKS_KARAKTER,
    GeciciHata,
    KaliciHata,
    KotaDoluHatasi,
    MedyaDogrulamaHatasi,
    MedyaTipi,
    TokenGecersizHatasi,
    YayinHatasi,
    hata_sinifla,
    simdi,
    sinirlari_getir,
)

if TYPE_CHECKING:  # yalnizca tip kontrolu; calisma aninda pydantic yuklenmez
    from src.ayarlar import Ayarlar

#: Proje gunlukcu agaci "insta.*" altindadir (bkz. arsiv.gunlugu_yapilandir).
#: __name__ kullanilirsa satirlar kok gunlukcuye duser ve log dosyasina yazilmaz.
KAYIT: Final[logging.Logger] = logging.getLogger("insta.instagram")

GRAFIK_HOST: Final[str] = "https://graph.facebook.com"
YUKLEME_HOST: Final[str] = "https://rupload.facebook.com"
VARSAYILAN_API_SURUMU: Final[str] = "v23.0"

#: Meta dokumanlari celisiyor (bir yerde 100, bir yerde 50). Kota her zaman
#: content_publishing_limit ucundan okunur; okunamazsa bu dusuk deger kullanilir.
VARSAYILAN_KOTA: Final[int] = 50

#: Kapsayici durum kodlari (GET /<KONTEYNER_ID>?fields=status_code)
DURUM_ISLENIYOR: Final[str] = "IN_PROGRESS"
DURUM_HAZIR: Final[str] = "FINISHED"
DURUM_YAYINLANDI: Final[str] = "PUBLISHED"
DURUM_HATA: Final[str] = "ERROR"
DURUM_SURE_DOLDU: Final[str] = "EXPIRED"

#: status alanindaki hata alt kodunu (ornek 2207026) yakalayan desen
_ALT_KOD_DESENI: Final[re.Pattern[str]] = re.compile(r"\b(\d{6,7})\b")

#: Yeniden denemede kullanilan geri cekilme (backoff) sinirlari
_GERI_CEKILME_TABAN_SN: Final[float] = 2.0
_GERI_CEKILME_TAVAN_SN: Final[float] = 60.0

#: Yayin cagrisi cevapsiz kaldiginda "acaba yayinlandi mi" kontrolu icin pencere
_YAYIN_IZI_PENCERESI_DK: Final[int] = 5

#: Yayin icin dokumante edilmis zorunlu izinler (debug_token ile kontrol edilir)
GEREKLI_IZINLER: Final[frozenset[str]] = frozenset(
    {"instagram_basic", "instagram_content_publish", "pages_read_engagement"}
)


# ---------------------------------------------------------------------------
# Bu modulun kendi istisnalari (taban siniflar modeller.py'den gelir)
# ---------------------------------------------------------------------------


class KonteynerSuresiDoldu(GeciciHata):
    """Kapsayici 24 saat icinde yayinlanmadi (EXPIRED).

    Tekrar denenebilir AMA ayni kapsayici ID'si ile DEGIL: yayinci yeni bir
    kapsayici acip dosyayi bastan yuklemelidir.
    """


class YayinDogrulanamadi(GeciciHata):
    """media_publish cevapsiz kaldi ve yayinin gerceklesip gerceklesmedigi
    kesin olarak belirlenemedi.

    Yayinci bu hatayi gorunce KOR TEKRAR YAPMAMALI; gorevi beklemeye alip bir
    sonraki turda once konteyner durumunu sorgulamalidir (duplicate riski).
    """


# ---------------------------------------------------------------------------
# Donen veri yapilari
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Konteyner:
    """Acilan medya kapsayicisi."""

    id: str
    yukleme_adresi: str          # kapsayici yanitindaki 'uri' (yoksa kurulan adres)
    olusturma_zamani: datetime   # 24 saatlik omur takibi icin (UTC)

    def __str__(self) -> str:
        return self.id


@dataclass(slots=True)
class KonteynerDurumu:
    """GET /<KONTEYNER_ID>?fields=id,status,status_code yaniti.

    NOT: IG Container dugumunde video_status alani YOKTUR (o Facebook Video
    dugumune aittir). Istenirse Graph API (#100) hatasi doner.
    """

    durum_kodu: str
    durum: str | None = None           # status_code ERROR ise burada alt kod olur
    yuklenen_bayt: int | None = None   # video_status.uploading_phase.bytes_transferred
    ham: dict[str, Any] | None = None

    @property
    def hazir_mi(self) -> bool:
        return self.durum_kodu in (DURUM_HAZIR, DURUM_YAYINLANDI)


@dataclass(slots=True)
class YayinSonucu:
    """media_publish sonrasi elde edilen bilgiler."""

    medya_id: str
    permalink: str | None = None
    urun_tipi: str | None = None       # media_product_type: REELS / STORY / FEED
    zaman_damgasi: str | None = None
    kurtarildi: bool = False           # cevapsiz cagri sonrasi dogrulanarak bulundu


# ---------------------------------------------------------------------------
# Yardimcilar
# ---------------------------------------------------------------------------


def _gizle(deger: str | None, gorunur: int = 4) -> str:
    """Log satirlari icin token maskesi. Acik token ASLA loga yazilmaz."""
    if not deger:
        return "<bos>"
    if len(deger) <= gorunur:
        return "*" * len(deger)
    return "*" * 8 + deger[-gorunur:]


def _sayiya_cevir(deger: Any) -> int | None:
    try:
        return int(deger)
    except (TypeError, ValueError):
        return None


def _alt_kodu_ayikla(metin: str | None) -> int | None:
    """status alanindaki metinden Meta alt kodunu (2207xxx) cikarir."""
    if not metin:
        return None
    eslesme = _ALT_KOD_DESENI.search(str(metin))
    return int(eslesme.group(1)) if eslesme else None


def _zaman_damgasini_coz(metin: str | None) -> datetime | None:
    """Meta zaman damgasi ('2026-09-22T12:00:00+0000') -> datetime."""
    if not metin:
        return None
    try:
        return datetime.fromisoformat(str(metin))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Istemci
# ---------------------------------------------------------------------------


class InstagramIstemcisi:
    """Instagram icerik yayini icin Graph API istemcisi.

    Kullanim:
        istemci = InstagramIstemcisi(token, ig_hesap_id, "v23.0")
        with istemci:
            konteyner = istemci.konteyner_olustur(MedyaTipi.REELS, "merhaba", yol)
            istemci.dosya_yukle(konteyner.id, yol)
            istemci.durum_bekle(konteyner.id)
            permalink = istemci.yayinla(konteyner.id)

    Butun metotlar hata durumunda modeller.py'deki istisnalari firlatir:
        KaliciHata / MedyaDogrulamaHatasi / TokenGecersizHatasi -> tekrar deneme
        GeciciHata / KotaDoluHatasi / KonteynerSuresiDoldu      -> tekrar denenebilir
    """

    def __init__(
        self,
        token: str,
        ig_hesap_id: str,
        api_surumu: str = VARSAYILAN_API_SURUMU,
        *,
        uygulama_id: str = "",
        uygulama_sirri: str = "",
        istek_zaman_asimi_sn: float = 60.0,
        baglanti_zaman_asimi_sn: float = 30.0,
        yukleme_zaman_asimi_sn: float = 900.0,
        durum_sorgu_araligi_sn: float = 20.0,
        durum_azami_bekleme_sn: float = 900.0,
        azami_deneme: int = 5,
        akisa_paylas: bool = True,
        istemci: httpx.Client | None = None,
    ) -> None:
        self._token = (token or "").strip()
        self.ig_hesap_id = (ig_hesap_id or "").strip()
        self.api_surumu = (api_surumu or VARSAYILAN_API_SURUMU).strip()
        self._uygulama_id = (uygulama_id or "").strip()
        self._uygulama_sirri = (uygulama_sirri or "").strip()

        if not self._token:
            raise ValueError(
                "Erisim anahtari bos. config/.env icindeki FB_SAYFA_TOKEN alanini "
                "doldurun (ornek dosya: config/.env.ornek)."
            )
        if not self.ig_hesap_id:
            raise ValueError(
                "Instagram hesap kimligi bos. config/.env icindeki IG_HESAP_ID "
                "alanini doldurun (ornek dosya: config/.env.ornek)."
            )

        self.istek_zaman_asimi_sn = float(istek_zaman_asimi_sn)
        self.baglanti_zaman_asimi_sn = float(baglanti_zaman_asimi_sn)
        self.yukleme_zaman_asimi_sn = float(yukleme_zaman_asimi_sn)
        self.durum_sorgu_araligi_sn = float(durum_sorgu_araligi_sn)
        self.durum_azami_bekleme_sn = float(durum_azami_bekleme_sn)
        self.azami_deneme = max(1, int(azami_deneme))
        self.akisa_paylas = bool(akisa_paylas)

        self._kendi_istemcisi = istemci is None
        self._istemci: httpx.Client = istemci or httpx.Client(
            timeout=httpx.Timeout(
                self.istek_zaman_asimi_sn,
                connect=self.baglanti_zaman_asimi_sn,
            ),
            follow_redirects=True,
        )
        #: kapsayici ID -> yukleme adresi (yanitindaki 'uri'); kesintide lazim
        self._yukleme_adresleri: dict[str, str] = {}
        #: en son basarili yayin (yayinla() ile birlikte guncellenir)
        self.son_yayin: YayinSonucu | None = None

    # -- kurulum / kapanis ---------------------------------------------------

    @classmethod
    def ayardan(cls, ayar: Ayarlar, *, istemci: httpx.Client | None = None) -> InstagramIstemcisi:
        """Ayarlar nesnesinden istemci uretir (yayina hazir olup olmadigini dogrular)."""
        ayar.yayina_hazir_degilse_hata()
        return cls(
            ayar.token,
            ayar.ig_hesap_id,
            ayar.api_surumu,
            uygulama_id=ayar.gizli.fb_uygulama_id,
            uygulama_sirri=ayar.gizli.uygulama_sirri,
            istek_zaman_asimi_sn=ayar.yayin.istek_zaman_asimi_sn,
            baglanti_zaman_asimi_sn=ayar.yayin.baglanti_zaman_asimi_sn,
            yukleme_zaman_asimi_sn=ayar.yayin.yukleme_zaman_asimi_sn,
            durum_sorgu_araligi_sn=ayar.yayin.durum_sorgu_araligi_sn,
            durum_azami_bekleme_sn=ayar.yayin.durum_azami_bekleme_sn,
            azami_deneme=ayar.yayin.azami_deneme,
            akisa_paylas=ayar.yayin.akisa_paylas,
            istemci=istemci,
        )

    def kapat(self) -> None:
        """Kendi actigi HTTP istemcisini kapatir (disaridan verilene dokunmaz)."""
        if self._kendi_istemcisi:
            self._istemci.close()

    def __enter__(self) -> InstagramIstemcisi:
        return self

    def __exit__(self, *_ayrinti: object) -> None:
        self.kapat()

    def __repr__(self) -> str:  # token sizdirmayan gosterim
        return (
            f"InstagramIstemcisi(ig_hesap_id={self.ig_hesap_id!r}, "
            f"api_surumu={self.api_surumu!r}, token={_gizle(self._token)})"
        )

    # -- adresler ------------------------------------------------------------

    @property
    def grafik_kok(self) -> str:
        """Ornek: https://graph.facebook.com/v23.0"""
        return f"{GRAFIK_HOST}/{self.api_surumu}"

    @property
    def rupload_kok(self) -> str:
        """Ornek: https://rupload.facebook.com/ig-api-upload/v23.0

        NOT: kapsayici yanitindaki 'uri' alani varsa ONCE o kullanilir; bu adres
        yalnizca yedek (fallback) olarak kurulur.
        """
        return f"{YUKLEME_HOST}/ig-api-upload/{self.api_surumu}"

    # ------------------------------------------------------------------
    # Dusuk seviye HTTP katmani
    # ------------------------------------------------------------------

    def _istek(
        self,
        yontem: str,
        adres: str,
        *,
        baglam: str,
        parametreler: dict[str, Any] | None = None,
        veri: dict[str, Any] | None = None,
        zaman_asimi: float | httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        """Tek graph cagrisi yapar, hata govdesini istisnaya cevirir.

        Ag hatalari GeciciHata'ya cevrilir; cagiran taraf isterse _tekrarla()
        icinde calistirarak ustel geri cekilme ile yeniden dener.
        """
        try:
            cevap = self._istemci.request(
                yontem,
                adres,
                params=parametreler,
                data=veri,
                timeout=zaman_asimi if zaman_asimi is not None else self._varsayilan_zaman_asimi(),
            )
        except httpx.TimeoutException as hata:
            raise GeciciHata(f"{baglam} zaman asimina ugradi: {hata}") from hata
        except httpx.TransportError as hata:
            raise GeciciHata(f"{baglam} sirasinda ag hatasi: {hata}") from hata

        return self._cevabi_coz(cevap, baglam)

    def _varsayilan_zaman_asimi(self) -> httpx.Timeout:
        return httpx.Timeout(
            self.istek_zaman_asimi_sn, connect=self.baglanti_zaman_asimi_sn
        )

    def _cevabi_coz(self, cevap: httpx.Response, baglam: str) -> dict[str, Any]:
        """HTTP yanitini sozluge cevirir; hata varsa uygun istisnayi firlatir."""
        try:
            govde: Any = cevap.json()
        except ValueError:
            govde = None

        if cevap.status_code >= 400 or (isinstance(govde, dict) and "error" in govde):
            self._hata_firlat(cevap, govde, baglam)

        if not isinstance(govde, dict):
            raise GeciciHata(
                f"{baglam}: beklenmeyen yanit bicimi (HTTP {cevap.status_code}). "
                f"Ilk 200 karakter: {cevap.text[:200]!r}"
            )
        return govde

    def _hata_firlat(self, cevap: httpx.Response, govde: Any, baglam: str) -> None:
        """Meta hata govdesini ayristirip dogru istisnayi firlatir (her zaman firlatir)."""
        hata: dict[str, Any] = {}
        if isinstance(govde, dict) and isinstance(govde.get("error"), dict):
            hata = govde["error"]

        if hata:
            kod = _sayiya_cevir(hata.get("code"))
            alt_kod = _sayiya_cevir(hata.get("error_subcode"))
            mesaj = str(hata.get("message") or "ayrinti yok")
            kullanici_mesaji = hata.get("error_user_msg")
            if kullanici_mesaji:
                mesaj = f"{mesaj} | {kullanici_mesaji}"
            iz_no = hata.get("fbtrace_id")
            KAYIT.error(
                "Meta hatasi | %s | HTTP %s | kod=%s alt_kod=%s iz=%s | %s",
                baglam,
                cevap.status_code,
                kod,
                alt_kod,
                iz_no or "-",
                mesaj,
            )
            raise hata_sinifla(
                kod,
                alt_kod,
                f"{baglam} basarisiz (HTTP {cevap.status_code}): {mesaj}",
                iz_no=str(iz_no) if iz_no else None,
            )

        # rupload tarafi: yapisal hata 'debug_info' icinde gelir
        ayiklama = govde.get("debug_info") if isinstance(govde, dict) else None
        if isinstance(ayiklama, dict):
            self._rupload_hatasi_firlat(ayiklama, cevap.status_code, baglam)

        ozet = cevap.text[:300] if cevap.text else "<bos govde>"
        if cevap.status_code >= 500 or cevap.status_code in (408, 429):
            raise GeciciHata(
                f"{baglam}: Meta sunucu tarafinda gecici hata (HTTP "
                f"{cevap.status_code}). Yanit: {ozet}"
            )
        raise KaliciHata(
            f"{baglam}: beklenmeyen hata (HTTP {cevap.status_code}). Yanit: {ozet}"
        )

    def _rupload_hatasi_firlat(
        self, ayiklama: dict[str, Any], http_kodu: int, baglam: str
    ) -> None:
        """rupload debug_info nesnesini istisnaya cevirir (her zaman firlatir)."""
        tekrar_denenebilir = ayiklama.get("retriable")
        tip = str(ayiklama.get("type") or "bilinmiyor")
        mesaj = str(ayiklama.get("message") or "ayrinti yok")
        metin = (
            f"{baglam} basarisiz (HTTP {http_kodu}, tip={tip}): {mesaj}"
        )
        KAYIT.error(
            "Yukleme hatasi | %s | tip=%s | tekrar_denenebilir=%s | %s",
            baglam,
            tip,
            tekrar_denenebilir,
            mesaj,
        )
        if tekrar_denenebilir is False:
            raise KaliciHata(
                metin + " | Ayni kapsayici ile tekrar denemek anlamsiz, "
                "yeni kapsayici acilmali."
            )
        raise GeciciHata(metin)

    # -- yeniden deneme ------------------------------------------------------

    def _geri_cekilme_suresi(self, deneme: int) -> float:
        """Ustel geri cekilme + kucuk rastgele sapma (thundering herd olmasin)."""
        sure = min(_GERI_CEKILME_TABAN_SN * (2 ** max(0, deneme - 1)), _GERI_CEKILME_TAVAN_SN)
        return sure + random.uniform(0.0, min(1.0, sure * 0.1))

    def _tekrarla(self, islem: Any, baglam: str, azami_deneme: int | None = None) -> Any:
        """Gecici hatalarda ustel geri cekilme ile tekrar dener.

        DIKKAT: media_publish bu sarmalayici ile KULLANILMAZ (duplicate riski).
        """
        sinir = azami_deneme if azami_deneme is not None else self.azami_deneme
        son_hata: YayinHatasi | None = None
        for deneme in range(1, sinir + 1):
            try:
                return islem()
            except (KaliciHata, KotaDoluHatasi):
                raise
            except GeciciHata as hata:
                son_hata = hata
                if deneme >= sinir:
                    break
                bekleme = self._geri_cekilme_suresi(deneme)
                KAYIT.warning(
                    "%s basarisiz (deneme %d/%d), %.1f sn sonra tekrar denenecek: %s",
                    baglam,
                    deneme,
                    sinir,
                    bekleme,
                    hata,
                )
                time.sleep(bekleme)
        raise son_hata or GeciciHata(f"{baglam}: deneme hakki bitti.")

    # ------------------------------------------------------------------
    # 0) Token dogrulama
    # ------------------------------------------------------------------

    def token_gecerli_mi(self) -> bool:
        """Erisim anahtari su anda kullanilabilir mi?

        Uygulama kimligi ve sirri varsa debug_token ucu kullanilir; yoksa hafif
        bir hesap sorgusu ile dolayli kontrol yapilir. Hicbir durumda istisna
        firlatmaz, sadece True/False doner (kullaniciya gorunen uyarilari loglar).
        """
        try:
            bilgi = self.token_bilgisi()
        except TokenGecersizHatasi as hata:
            KAYIT.error("Erisim anahtari gecersiz: %s", hata)
            return False
        except YayinHatasi as hata:
            KAYIT.warning("Erisim anahtari dogrulanamadi: %s", hata)
            return False
        return bool(bilgi.get("gecerli"))

    def token_bilgisi(self) -> dict[str, Any]:
        """debug_token ciktisini sadelestirilmis Turkce ozet olarak dondurur.

        Donen sozluk: gecerli, kaynak, bitis_zamani, veri_erisim_bitisi,
        izinler, eksik_izinler, uyarilar.
        """
        if not self._uygulama_id or not self._uygulama_sirri:
            KAYIT.warning(
                "FB_UYGULAMA_ID / FB_UYGULAMA_SIRRI bos oldugu icin debug_token "
                "kontrolu yapilamiyor; hesap sorgusu ile dolayli dogrulama yapiliyor."
            )
            return self._hafif_token_kontrolu()

        govde = self._tekrarla(
            lambda: self._istek(
                "GET",
                f"{self.grafik_kok}/debug_token",
                baglam="Erisim anahtari dogrulama",
                parametreler={
                    "input_token": self._token,
                    "access_token": f"{self._uygulama_id}|{self._uygulama_sirri}",
                },
            ),
            "Erisim anahtari dogrulama",
        )
        veri = govde.get("data")
        if not isinstance(veri, dict):
            raise GeciciHata(
                "Erisim anahtari dogrulama: beklenen 'data' alani gelmedi."
            )

        gecerli = bool(veri.get("is_valid"))
        if not gecerli:
            ic_hata = veri.get("error") if isinstance(veri.get("error"), dict) else {}
            raise TokenGecersizHatasi(
                "Erisim anahtari gecersiz (kod="
                f"{ic_hata.get('code')} alt_kod={ic_hata.get('subcode')}): "
                f"{ic_hata.get('message', 'ayrinti yok')}. Facebook ile yeniden "
                "giris yapip config/.env dosyasini guncelleyin.",
                kod=_sayiya_cevir(ic_hata.get("code")),
                alt_kod=_sayiya_cevir(ic_hata.get("subcode")),
            )

        izinler = [str(k) for k in (veri.get("scopes") or [])]
        eksik = sorted(GEREKLI_IZINLER - set(izinler))
        uyarilar: list[str] = []
        if eksik:
            uyarilar.append("Eksik izinler: " + ", ".join(eksik))
            KAYIT.warning(
                "Erisim anahtarinda eksik izinler var: %s. Yayin denemesi "
                "yetki hatasi verebilir.",
                ", ".join(eksik),
            )

        su_an = int(time.time())
        bitis = _sayiya_cevir(veri.get("expires_at")) or 0
        veri_bitis = _sayiya_cevir(veri.get("data_access_expires_at")) or 0

        # NOT: expires_at == 0 degerinin "suresiz" demek oldugu Meta dokumaninda
        # yazmiyor; asil kaynak is_valid alanidir. Savunmaci davraniliyor.
        if bitis and 0 < bitis - su_an < 7 * 86400:
            mesaj = (
                "Erisim anahtarinin suresi 7 gunden az kaldi, yenilemeniz gerekiyor."
            )
            uyarilar.append(mesaj)
            KAYIT.warning(mesaj)
        if veri_bitis and 0 < veri_bitis - su_an < 7 * 86400:
            mesaj = (
                "90 gunluk veri erisim suresi doluyor; Facebook ile yeniden "
                "giris yapip yetkileri tazeleyin."
            )
            uyarilar.append(mesaj)
            KAYIT.warning(mesaj)

        return {
            "gecerli": True,
            "kaynak": "debug_token",
            "bitis_zamani": bitis or None,
            "veri_erisim_bitisi": veri_bitis or None,
            "izinler": izinler,
            "eksik_izinler": eksik,
            "uyarilar": uyarilar,
        }

    def _hafif_token_kontrolu(self) -> dict[str, Any]:
        """Uygulama sirri yokken: hesap sorgusu ile dolayli token kontrolu."""
        govde = self._tekrarla(
            lambda: self._istek(
                "GET",
                f"{self.grafik_kok}/{self.ig_hesap_id}",
                baglam="Hesap sorgusu",
                parametreler={"fields": "id,username", "access_token": self._token},
            ),
            "Hesap sorgusu",
        )
        return {
            "gecerli": bool(govde.get("id")),
            "kaynak": "hesap_sorgusu",
            "bitis_zamani": None,
            "veri_erisim_bitisi": None,
            "izinler": [],
            "eksik_izinler": [],
            "uyarilar": [
                "debug_token kontrolu yapilamadi (uygulama kimligi/sirri bos); "
                "anahtarin bitis tarihi bilinmiyor."
            ],
            "kullanici_adi": govde.get("username"),
        }

    # ------------------------------------------------------------------
    # 1) Yayin kotasi
    # ------------------------------------------------------------------

    def yayin_limiti_kontrol(self) -> tuple[int, int]:
        """24 saatlik kayan penceredeki yayin kotasini okur.

        Doner: (kullanilan, kota)
          - kullanilan: pencerede yayinlanmis gonderi sayisi (quota_usage)
          - kota      : donem basina izin verilen azami yayin (config.quota_total)

        Kota degerini KODA GOMME; Meta dokumanlari 50 ve 100 arasinda celisiyor,
        dogru deger her zaman bu uctan okunur.
        """
        govde = self._tekrarla(
            lambda: self._istek(
                "GET",
                f"{self.grafik_kok}/{self.ig_hesap_id}/content_publishing_limit",
                baglam="Yayin kotasi sorgulama",
                parametreler={
                    "fields": "config,quota_usage",
                    "access_token": self._token,
                },
            ),
            "Yayin kotasi sorgulama",
        )
        kayitlar = govde.get("data")
        if not isinstance(kayitlar, list) or not kayitlar:
            KAYIT.warning(
                "Yayin kotasi yaniti bos geldi; varsayilan kota (%d) kullaniliyor.",
                VARSAYILAN_KOTA,
            )
            return 0, VARSAYILAN_KOTA

        kayit = kayitlar[0] if isinstance(kayitlar[0], dict) else {}
        kullanilan = _sayiya_cevir(kayit.get("quota_usage")) or 0
        yapilandirma = kayit.get("config") if isinstance(kayit.get("config"), dict) else {}
        kota = _sayiya_cevir(yapilandirma.get("quota_total"))
        if not kota:
            KAYIT.warning(
                "Kota degeri okunamadi; varsayilan kota (%d) kullaniliyor.",
                VARSAYILAN_KOTA,
            )
            kota = VARSAYILAN_KOTA

        KAYIT.info("Yayin kotasi: %d / %d (son 24 saat).", kullanilan, kota)
        return kullanilan, kota

    def kota_musait_mi(self) -> bool:
        """Kota doluysa False doner (gorev hata degil ERTELEME almalidir)."""
        kullanilan, kota = self.yayin_limiti_kontrol()
        return kullanilan < kota

    # ------------------------------------------------------------------
    # 2) Kapsayici olusturma
    # ------------------------------------------------------------------

    def konteyner_olustur(
        self,
        medya_tipi: MedyaTipi | str,
        aciklama: str | None = None,
        yerel_dosya: Path | str | None = None,
        *,
        akisa_paylas: bool | None = None,
        kapak_url: str | None = None,
        kucuk_resim_ofseti_ms: int | None = None,
        ses_adi: str | None = None,
        konum_id: str | None = None,
    ) -> Konteyner:
        """Adim 1: resumable yukleme kapsayicisi acar.

        Yerel dosya yuklenecegi icin video_url GONDERILMEZ; gonderilirse Meta
        dosyayi o adresten cekmeye calisir ve 9004/2207052 hatasi doner.

        yerel_dosya verilirse yukleme oncesi yerel dogrulama yapilir (uzanti ve
        dosya boyutu). Sinir asimlari yukleme sirasinda degil ISLEME sirasinda
        status_code=ERROR olarak dondugu icin burada elemek cok daha ucuzdur.
        """
        tip = MedyaTipi(str(medya_tipi))
        if yerel_dosya is not None:
            self._yerel_dogrula(Path(yerel_dosya), tip)

        gonderilecek: dict[str, str] = {
            "media_type": str(tip),
            "upload_type": "resumable",   # rupload protokolu (SADECE video)
            "access_token": self._token,
        }

        if tip is MedyaTipi.REELS:
            # Asagidaki parametrelerin tamami SADECE REELS icin dokumantedir.
            temiz_aciklama = self._aciklamayi_dogrula(aciklama)
            if temiz_aciklama:
                gonderilecek["caption"] = temiz_aciklama
            paylas = self.akisa_paylas if akisa_paylas is None else bool(akisa_paylas)
            gonderilecek["share_to_feed"] = "true" if paylas else "false"
            if kapak_url:
                gonderilecek["cover_url"] = kapak_url
            if kucuk_resim_ofseti_ms is not None:
                gonderilecek["thumb_offset"] = str(int(kucuk_resim_ofseti_ms))
            if ses_adi:
                gonderilecek["audio_name"] = ses_adi
            if konum_id:
                gonderilecek["location_id"] = str(konum_id)
        else:
            # STORIES: caption / share_to_feed / cover_url / thumb_offset /
            # audio_name / collaborators desteklenmiyor veya dokumante degil.
            if aciklama:
                KAYIT.info(
                    "Story icin aciklama gonderilmiyor (Instagram Story'de altyazi "
                    "alani yok): %s",
                    aciklama[:60],
                )

        govde = self._tekrarla(
            lambda: self._istek(
                "POST",
                f"{self.grafik_kok}/{self.ig_hesap_id}/media",
                baglam="Kapsayici olusturma",
                veri=gonderilecek,
            ),
            "Kapsayici olusturma",
        )

        konteyner_id = str(govde.get("id") or "").strip()
        if not konteyner_id:
            raise GeciciHata(
                "Kapsayici olusturuldu ancak kimlik alani yanitta yok: "
                + str(govde)[:200]
            )

        # Yanitta hazir yukleme adresi ('uri') varsa ONCE o kullanilir; API
        # surumunu elle kurmaktan daha guvenlidir.
        adres = str(govde.get("uri") or "").strip() or f"{self.rupload_kok}/{konteyner_id}"
        self._yukleme_adresleri[konteyner_id] = adres

        KAYIT.info(
            "Kapsayici acildi: %s (%s). 24 saat icinde yayinlanmazsa suresi dolar.",
            konteyner_id,
            tip,
        )
        return Konteyner(
            id=konteyner_id, yukleme_adresi=adres, olusturma_zamani=simdi()
        )

    def _yerel_dogrula(self, dosya: Path, tip: MedyaTipi) -> None:
        """Yukleme oncesi ucuz yerel kontroller (uzanti, boyut)."""
        if not dosya.exists():
            raise MedyaDogrulamaHatasi(f"Dosya bulunamadi: {dosya}")
        if not dosya.is_file():
            raise MedyaDogrulamaHatasi(f"Yol bir dosya degil: {dosya}")

        sinirlar = sinirlari_getir(tip)
        uzanti = dosya.suffix.lower()
        if uzanti not in tuple(sinirlar["uzantilar"]):
            raise MedyaDogrulamaHatasi(
                f"Desteklenmeyen dosya turu '{uzanti}'. Bu akis SADECE video "
                f"yukler; kabul edilenler: {', '.join(sinirlar['uzantilar'])}. "
                "Gorseller once kisa bir MP4'e cevrilmelidir."
            )

        boyut = dosya.stat().st_size
        if boyut <= 0:
            raise MedyaDogrulamaHatasi(f"Dosya bos (0 bayt): {dosya.name}")
        azami = int(sinirlar["maks_boyut_bayt"])
        if boyut > azami:
            raise MedyaDogrulamaHatasi(
                f"Dosya cok buyuk: {dosya.name} "
                f"({boyut / 1048576:.1f} MB). {tip} icin azami sinir "
                f"{azami / 1048576:.0f} MB."
            )

    def _aciklamayi_dogrula(self, aciklama: str | None) -> str | None:
        """Caption sinirlarini yerel olarak kontrol eder (Reels icin)."""
        if not aciklama:
            return None
        metin = str(aciklama)
        if len(metin) > CAPTION_MAKS_KARAKTER:
            raise MedyaDogrulamaHatasi(
                f"Aciklama cok uzun: {len(metin)} karakter. Azami sinir "
                f"{CAPTION_MAKS_KARAKTER} karakter."
            )
        if metin.count("#") > CAPTION_MAKS_HASHTAG:
            raise MedyaDogrulamaHatasi(
                f"Aciklamada {metin.count('#')} etiket var. Azami "
                f"{CAPTION_MAKS_HASHTAG} hashtag kullanilabilir."
            )
        if metin.count("@") > CAPTION_MAKS_ETIKET:
            raise MedyaDogrulamaHatasi(
                f"Aciklamada {metin.count('@')} kullanici etiketi var. Azami "
                f"{CAPTION_MAKS_ETIKET} etiket kullanilabilir."
            )
        return metin

    # ------------------------------------------------------------------
    # 3) Dosya yukleme (rupload)
    # ------------------------------------------------------------------

    def dosya_yukle(
        self,
        container_id: str,
        yerel_dosya: Path | str,
        *,
        yukleme_adresi: str | None = None,
    ) -> None:
        """Adim 2: yerel videoyu rupload.facebook.com adresine ham binary yukler.

        Kesinti olursa sunucudaki ofset graph ucundan ogrenilir
        (video_status.uploading_phase.bytes_transferred) ve kalan baytlar
        gonderilir; file_size her zaman dosyanin TOPLAM boyutudur.

        Basarili yukleme YAYINLANDI demek DEGILDIR; ardindan durum_bekle()
        cagrilmalidir.
        """
        dosya = Path(yerel_dosya)
        if not dosya.is_file():
            raise MedyaDogrulamaHatasi(f"Yuklenecek dosya bulunamadi: {dosya}")

        toplam_bayt = dosya.stat().st_size
        if toplam_bayt <= 0:
            raise MedyaDogrulamaHatasi(f"Dosya bos (0 bayt): {dosya.name}")

        adres = (
            yukleme_adresi
            or self._yukleme_adresleri.get(container_id)
            or f"{self.rupload_kok}/{container_id}"
        )
        zaman_asimi = httpx.Timeout(
            self.yukleme_zaman_asimi_sn,
            connect=self.baglanti_zaman_asimi_sn,
            read=self.yukleme_zaman_asimi_sn,
            write=self.yukleme_zaman_asimi_sn,
        )

        ofset = 0
        son_hata: YayinHatasi | None = None

        for deneme in range(1, self.azami_deneme + 1):
            if ofset >= toplam_bayt:
                KAYIT.info(
                    "Dosya zaten tamamen yuklenmis gorunuyor (%d bayt): %s",
                    toplam_bayt,
                    dosya.name,
                )
                return

            # DIKKAT: baytlar bellege okunur. Dosya nesnesi (content=f) verilirse
            # httpx 'Transfer-Encoding: chunked' kullanir ve Content-Length
            # gondermez; rupload'un bunu kabul ettigi dogrulanmis degildir.
            with dosya.open("rb") as akis:
                akis.seek(ofset)
                govde_baytlari = akis.read()

            basliklar = {
                # DIKKAT: sema "Bearer" DEGIL, "OAuth"
                "Authorization": f"OAuth {self._token}",
                "offset": str(ofset),               # kucuk harf, alt cizgili
                "file_size": str(toplam_bayt),      # HER ZAMAN toplam boyut
                "Content-Type": "application/octet-stream",
            }

            KAYIT.info(
                "Video yukleniyor: %s | %d/%d bayt | deneme %d/%d | anahtar=%s",
                dosya.name,
                ofset,
                toplam_bayt,
                deneme,
                self.azami_deneme,
                _gizle(self._token),
            )

            try:
                cevap = self._istemci.post(
                    adres,
                    headers=basliklar,
                    content=govde_baytlari,
                    timeout=zaman_asimi,
                )
            except httpx.TimeoutException as hata:
                son_hata = GeciciHata(f"Video yukleme zaman asimina ugradi: {hata}")
                KAYIT.warning("Yukleme zaman asimi (deneme %d): %s", deneme, hata)
            except httpx.TransportError as hata:
                son_hata = GeciciHata(f"Video yukleme sirasinda ag hatasi: {hata}")
                KAYIT.warning("Yukleme ag hatasi (deneme %d): %s", deneme, hata)
            else:
                try:
                    if self._yukleme_yaniti_basarili_mi(cevap):
                        KAYIT.info("Video yuklendi: %s (%d bayt).", dosya.name, toplam_bayt)
                        return
                    son_hata = GeciciHata(
                        "Video yukleme basarisiz: yanit basari bildirmedi "
                        f"(HTTP {cevap.status_code})."
                    )
                except GeciciHata as hata:
                    son_hata = hata
                    KAYIT.warning("Yukleme reddedildi (deneme %d): %s", deneme, hata)

            if deneme >= self.azami_deneme:
                break

            bekleme = self._geri_cekilme_suresi(deneme)
            KAYIT.info("Yukleme %.1f sn sonra kaldigi yerden denenecek.", bekleme)
            time.sleep(bekleme)
            ofset = self._sunucudaki_ofset(container_id, toplam_bayt)
            KAYIT.info(
                "Kaldigi yerden devam ediliyor, ofset: %d/%d", ofset, toplam_bayt
            )

        raise son_hata or GeciciHata(
            f"Video yuklenemedi, deneme hakki bitti: {dosya.name}"
        )

    def _yukleme_yaniti_basarili_mi(self, cevap: httpx.Response) -> bool:
        """rupload yanitini degerlendirir.

        True  -> yukleme basarili
        False -> basarisiz ama yapisal hata yok (tekrar denenebilir)
        Istisna -> KaliciHata (retriable=false) veya GeciciHata
        """
        try:
            govde: Any = cevap.json()
        except ValueError:
            govde = None

        if cevap.status_code == 200 and isinstance(govde, dict):
            if govde.get("success") is True:
                return True

        if isinstance(govde, dict) and (
            "error" in govde or "debug_info" in govde
        ):
            # Her zaman istisna firlatir
            self._hata_firlat(cevap, govde, "Video yukleme")

        if cevap.status_code >= 400:
            self._hata_firlat(cevap, govde, "Video yukleme")

        KAYIT.warning(
            "Yukleme yaniti beklenen basari bilgisini icermiyor (HTTP %s): %s",
            cevap.status_code,
            (cevap.text or "")[:200],
        )
        return False

    def _sunucudaki_ofset(self, container_id: str, toplam_bayt: int) -> int:
        """Kesinti sonrasi sunucuya ulasmis bayt sayisini ogrenir.

        Ofset graph.facebook.com uzerinden sorgulanir (rupload host'undan DEGIL).
        Sorgu basarisiz olursa 0 doner, yani dosya bastan yuklenir.
        """
        try:
            durum = self.konteyner_durumu(
                container_id, alanlar="id,status,status_code"
            )
        except YayinHatasi as hata:
            KAYIT.warning(
                "Yukleme ofseti sorgulanamadi, bastan yuklenecek: %s", hata
            )
            return 0

        ofset = durum.yuklenen_bayt or 0
        if ofset < 0:
            return 0
        if ofset > toplam_bayt:
            KAYIT.warning(
                "Sunucudaki ofset (%d) dosya boyutundan (%d) buyuk; bastan "
                "yuklenecek.",
                ofset,
                toplam_bayt,
            )
            return 0
        return ofset

    # ------------------------------------------------------------------
    # 4) Durum sorgulama ve bekleme
    # ------------------------------------------------------------------

    def konteyner_durumu(
        self, container_id: str, *, alanlar: str = "status_code,status"
    ) -> KonteynerDurumu:
        """GET /<KONTEYNER_ID> - kapsayicinin islenme durumu.

        HTTP 200 donmesi basari anlamina GELMEZ; asil bilgi status_code alanidir.
        status_code == ERROR ise gercek sebep 'status' alanindaki alt koddur.
        """
        govde = self._tekrarla(
            lambda: self._istek(
                "GET",
                f"{self.grafik_kok}/{container_id}",
                baglam="Durum sorgulama",
                parametreler={"fields": alanlar, "access_token": self._token},
            ),
            "Durum sorgulama",
            azami_deneme=3,
        )

        video_durumu = govde.get("video_status")
        yuklenen: int | None = None
        if isinstance(video_durumu, dict):
            asama = video_durumu.get("uploading_phase")
            if isinstance(asama, dict):
                yuklenen = _sayiya_cevir(asama.get("bytes_transferred"))

        return KonteynerDurumu(
            durum_kodu=str(govde.get("status_code") or ""),
            durum=str(govde.get("status")) if govde.get("status") is not None else None,
            yuklenen_bayt=yuklenen,
            ham=govde,
        )

    def durum_bekle(
        self,
        container_id: str,
        *,
        aralik_sn: float | None = None,
        azami_bekleme_sn: float | None = None,
    ) -> str:
        """Adim 3: status_code FINISHED olana kadar bekler.

        Doner: "FINISHED" veya "PUBLISHED".
        Firlatir:
          - ERROR    -> status alanindaki alt koda gore Kalici/GeciciHata
          - EXPIRED  -> KonteynerSuresiDoldu (yeni kapsayici acilmali)
          - zaman asimi -> GeciciHata (kapsayici 24 saat gecerli, sonra tekrar bak)

        Meta'nin onerisi dakikada bir sorgulamak ve 5 dakikayi asmamaktir; buyuk
        dosyalarda bu yetmediginden aralik ve ust sinir ayarlardan gelir.
        """
        aralik = float(aralik_sn if aralik_sn is not None else self.durum_sorgu_araligi_sn)
        sinir = float(
            azami_bekleme_sn if azami_bekleme_sn is not None else self.durum_azami_bekleme_sn
        )
        baslangic = time.monotonic()

        while True:
            durum = self.konteyner_durumu(container_id)
            kod = durum.durum_kodu

            if kod in (DURUM_HAZIR, DURUM_YAYINLANDI):
                KAYIT.info("Kapsayici hazir: %s (%s).", container_id, kod)
                return kod

            if kod == DURUM_HATA:
                alt_kod = _alt_kodu_ayikla(durum.durum)
                KAYIT.error(
                    "Kapsayici isleme hatasi | kapsayici=%s | status=%s",
                    container_id,
                    durum.durum,
                )
                raise hata_sinifla(
                    None,
                    alt_kod,
                    "Video islenirken hata olustu (kapsayici "
                    f"{container_id}). Meta ayrintisi: {durum.durum or 'yok'}. "
                    "En sik sebep: moov atom dosyanin basinda degil "
                    "(ffmpeg -movflags +faststart), desteklenmeyen kodek veya "
                    "sure/boyut siniri asimi.",
                )

            if kod == DURUM_SURE_DOLDU:
                raise KonteynerSuresiDoldu(
                    f"Kapsayicinin 24 saatlik suresi doldu (kapsayici {container_id}). "
                    "Yeni kapsayici acilip dosya bastan yuklenmelidir."
                )

            gecen = time.monotonic() - baslangic
            if gecen >= sinir:
                raise GeciciHata(
                    f"Video isleme zaman asimi: {int(gecen)} sn sonunda kapsayici "
                    f"hala hazir degil (durum={kod or 'bilinmiyor'}). Kapsayici 24 "
                    "saat gecerlidir, gorev beklemede birakildi."
                )

            KAYIT.info(
                "Video isleniyor... durum=%s | gecen sure %d sn | %.0f sn sonra "
                "tekrar sorgulanacak.",
                kod or DURUM_ISLENIYOR,
                int(gecen),
                aralik,
            )
            time.sleep(aralik)

    # ------------------------------------------------------------------
    # 5) Yayinlama
    # ------------------------------------------------------------------

    def yayinla(self, container_id: str, *, durumu_kontrol_et: bool = True) -> str | None:
        """Adim 4: kapsayiciyi yayinlar ve gonderi baglantisini (permalink) dondurur.

        ASLA OTOMATIK TEKRAR DENENMEZ. media_publish ag hatasi ile donse bile
        istek sunucuda basarili olmus olabilir ve Meta bu cagri icin idempotency
        anahtari sunmaz; kor tekrar ayni icerigi iki kez yayinlar.

        Cevapsiz kalirsa: once kapsayici durumu, sonra hesabin son gonderileri
        kontrol edilir. Yayin izi bulunursa islem basarili sayilir; bulunamazsa
        YayinDogrulanamadi firlatilir (yayinci kor tekrar yapmamalidir).

        Ayrintili sonuc (medya kimligi dahil) icin yayinla_ayrintili() kullanin.
        """
        sonuc = self.yayinla_ayrintili(container_id, durumu_kontrol_et=durumu_kontrol_et)
        return sonuc.permalink

    def yayinla_ayrintili(
        self, container_id: str, *, durumu_kontrol_et: bool = True
    ) -> YayinSonucu:
        """yayinla() ile ayni; medya kimligi ve urun tipini de dondurur."""
        if durumu_kontrol_et:
            # FINISHED olmadan media_publish cagirmak 9007/2207027 verir.
            durum = self.konteyner_durumu(container_id)
            if durum.durum_kodu == DURUM_YAYINLANDI:
                KAYIT.warning(
                    "Kapsayici zaten yayinlanmis (%s); tekrar yayin cagrisi "
                    "yapilmiyor.",
                    container_id,
                )
                kurtarilan = self._yayin_izini_ara()
                if kurtarilan:
                    kurtarilan.kurtarildi = True
                    self.son_yayin = kurtarilan
                    return kurtarilan
                raise YayinDogrulanamadi(
                    f"Kapsayici {container_id} yayinlanmis gorunuyor ancak gonderi "
                    "bilgisi okunamadi. Instagram hesabini elle kontrol edin."
                )
            if durum.durum_kodu == DURUM_SURE_DOLDU:
                raise KonteynerSuresiDoldu(
                    f"Kapsayicinin 24 saatlik suresi doldu (kapsayici {container_id}). "
                    "Yeni kapsayici acilip dosya bastan yuklenmelidir."
                )
            if durum.durum_kodu != DURUM_HAZIR:
                raise GeciciHata(
                    "Kapsayici yayina hazir degil (durum="
                    f"{durum.durum_kodu or 'bilinmiyor'}). Once durum_bekle() "
                    "cagrilmalidir."
                )

        adres = f"{self.grafik_kok}/{self.ig_hesap_id}/media_publish"
        veri = {"creation_id": container_id, "access_token": self._token}

        try:
            govde = self._istek(
                "POST", adres, baglam="Yayinlama", veri=veri
            )
        except GeciciHata as hata:
            # Ag/zaman asimi: istek sunucuda basarili olmus OLABILIR.
            KAYIT.error(
                "Yayin cagrisi cevapsiz kaldi (%s). Duplicate riski nedeniyle "
                "otomatik tekrar YAPILMIYOR; once yayin izi araniyor.",
                hata,
            )
            kurtarilan = self._yayini_dogrula(container_id)
            if kurtarilan is not None:
                self.son_yayin = kurtarilan
                return kurtarilan
            raise YayinDogrulanamadi(
                "Yayin cagrisinin sonucu belirlenemedi (kapsayici "
                f"{container_id}). Icerik yayinlanmis olabilir; ayni gorevi "
                "tekrar yayinlamadan once Instagram hesabini kontrol edin. "
                f"Ilk hata: {hata}",
                kod=hata.kod,
                alt_kod=hata.alt_kod,
                iz_no=hata.iz_no,
            ) from hata

        medya_id = str(govde.get("id") or "").strip()
        if not medya_id:
            raise GeciciHata(
                "Yayin yaniti beklenen medya kimligini icermiyor: " + str(govde)[:200]
            )

        sonuc = self._medya_bilgisi(medya_id)
        self.son_yayin = sonuc
        KAYIT.info(
            "Yayinlandi. Medya kimligi=%s | tip=%s | baglanti=%s",
            sonuc.medya_id,
            sonuc.urun_tipi or "bilinmiyor",
            sonuc.permalink or "yok",
        )
        return sonuc

    def _medya_bilgisi(self, medya_id: str) -> YayinSonucu:
        """Yayinlanan medyanin baglantisini ve urun tipini okur.

        NOT: media_type alani reel icin 'VIDEO' doner; gercek tip icin
        media_product_type (REELS / STORY / FEED) kullanilir.
        Bu sorgu basarisiz olursa yayin YINE DE basarilidir; sadece baglanti
        bilinmez.
        """
        try:
            govde = self._istek(
                "GET",
                f"{self.grafik_kok}/{medya_id}",
                baglam="Gonderi bilgisi sorgulama",
                parametreler={
                    "fields": "id,permalink,media_product_type,media_type,timestamp",
                    "access_token": self._token,
                },
            )
        except YayinHatasi as hata:
            KAYIT.warning(
                "Gonderi baglantisi okunamadi (yayin basarili): %s", hata
            )
            return YayinSonucu(medya_id=medya_id)

        return YayinSonucu(
            medya_id=str(govde.get("id") or medya_id),
            permalink=str(govde["permalink"]) if govde.get("permalink") else None,
            urun_tipi=(
                str(govde["media_product_type"])
                if govde.get("media_product_type")
                else None
            ),
            zaman_damgasi=str(govde["timestamp"]) if govde.get("timestamp") else None,
        )

    # -- duplicate korumasi --------------------------------------------------

    def _yayini_dogrula(self, container_id: str) -> YayinSonucu | None:
        """Cevapsiz yayin cagrisindan sonra: gercekten yayinlandi mi?

        Sirayla: (1) kapsayici durumu PUBLISHED mi, (2) hesabin son gonderileri
        arasinda birkac dakika icinde olusmus bir kayit var mi. Ikisi de yoksa
        None doner ve cagiran taraf kor tekrar YAPMAZ.
        """
        time.sleep(5.0)  # sunucunun durumu guncellemesi icin kisa nefes
        try:
            durum = self.konteyner_durumu(container_id)
        except YayinHatasi as hata:
            KAYIT.warning("Yayin dogrulama sirasinda durum okunamadi: %s", hata)
            durum = KonteynerDurumu(durum_kodu="")

        if durum.durum_kodu == DURUM_YAYINLANDI:
            KAYIT.warning(
                "Kapsayici %s aslinda YAYINLANMIS; tekrar yayin yapilmayacak.",
                container_id,
            )
            sonuc = self._yayin_izini_ara() or YayinSonucu(medya_id="")
            sonuc.kurtarildi = True
            return sonuc

        iz = self._yayin_izini_ara()
        if iz is not None:
            KAYIT.warning(
                "Son %d dakika icinde yeni bir gonderi bulundu (%s); yayin "
                "basarili kabul ediliyor, tekrar denenmeyecek.",
                _YAYIN_IZI_PENCERESI_DK,
                iz.medya_id,
            )
            iz.kurtarildi = True
            return iz

        return None

    def _yayin_izini_ara(self, *, pencere_dk: int = _YAYIN_IZI_PENCERESI_DK) -> YayinSonucu | None:
        """Hesabin son gonderileri arasinda yeni yayin izi arar."""
        try:
            govde = self._istek(
                "GET",
                f"{self.grafik_kok}/{self.ig_hesap_id}/media",
                baglam="Son gonderileri sorgulama",
                parametreler={
                    "fields": "id,permalink,media_product_type,media_type,timestamp",
                    "limit": 5,
                    "access_token": self._token,
                },
            )
        except YayinHatasi as hata:
            KAYIT.warning("Son gonderiler okunamadi: %s", hata)
            return None

        kayitlar = govde.get("data")
        if not isinstance(kayitlar, list):
            return None

        esik = simdi() - timedelta(minutes=pencere_dk)
        for kayit in kayitlar:
            if not isinstance(kayit, dict):
                continue
            an = _zaman_damgasini_coz(kayit.get("timestamp"))
            if an is None or an < esik:
                continue
            return YayinSonucu(
                medya_id=str(kayit.get("id") or ""),
                permalink=str(kayit["permalink"]) if kayit.get("permalink") else None,
                urun_tipi=(
                    str(kayit["media_product_type"])
                    if kayit.get("media_product_type")
                    else None
                ),
                zaman_damgasi=str(kayit["timestamp"]) if kayit.get("timestamp") else None,
            )
        return None

    # ------------------------------------------------------------------
    # Bastan sona akis (kolaylik metodu)
    # ------------------------------------------------------------------

    def videoyu_yayinla(
        self,
        yerel_dosya: Path | str,
        medya_tipi: MedyaTipi | str = MedyaTipi.REELS,
        aciklama: str | None = None,
        *,
        kota_kontrol: bool = True,
    ) -> YayinSonucu:
        """Kapsayici -> yukleme -> durum -> yayin akisini bastan sona calistirir.

        Kuyruk (yayinci.py) normalde adimlari tek tek cagirir ve her adimda
        veritabanini gunceller; bu metot elle test ve tek seferlik yayin icindir.
        """
        if kota_kontrol:
            kullanilan, kota = self.yayin_limiti_kontrol()
            if kullanilan >= kota:
                raise KotaDoluHatasi(
                    f"Gunluk yayin kotasi dolu ({kullanilan}/{kota}). Kota kayan "
                    "24 saatlik penceredir; gorev ertelendi."
                )

        dosya = Path(yerel_dosya)
        konteyner = self.konteyner_olustur(medya_tipi, aciklama, dosya)
        self.dosya_yukle(konteyner.id, dosya)
        self.durum_bekle(konteyner.id)
        return self.yayinla_ayrintili(konteyner.id, durumu_kontrol_et=False)


__all__ = [
    "GEREKLI_IZINLER",
    "GRAFIK_HOST",
    "InstagramIstemcisi",
    "Konteyner",
    "KonteynerDurumu",
    "KonteynerSuresiDoldu",
    "VARSAYILAN_API_SURUMU",
    "VARSAYILAN_KOTA",
    "YUKLEME_HOST",
    "YayinDogrulanamadi",
    "YayinSonucu",
    "DURUM_HATA",
    "DURUM_HAZIR",
    "DURUM_ISLENIYOR",
    "DURUM_SURE_DOLDU",
    "DURUM_YAYINLANDI",
]
