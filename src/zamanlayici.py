# -*- coding: ascii -*-
"""Zamanlayici: kuyruk taramasi, slot dagitimi ve yayin orkestrasyonu.

Sorumluluklar:
  1) APScheduler ile dakikada bir (ayarlar.yaml: tarama_araligi_sn) kuyrugu tarar.
  2) Zamansiz gorevleri gunluk yayin slotlarina (ornek 12:00, 19:00) sirayla yerlestirir.
  3) Her yayindan ONCE yayin limitini kontrol eder; limit doluysa yayinlamaz,
     Turkce uyari loglar ve gorevi kuyrukta BEKLIYOR olarak birakir.
  4) Yayin akisini yurutur:
        konteyner_olustur -> dosya_yukle -> durum_bekle -> yayinla -> arsivle
     Her adimdan sonra veritabani durumu guncellenir; program cokse bile
     gorev kaldigi yerden devam eder.
  5) Haftalik token kontrolu yapar; token gecersizse buyuk Turkce uyari basar.

Hata politikasi (modeller.py ile ayni):
  KaliciHata          -> dosya hata/ klasorune tasinir, gorev HATA olur.
  GeciciHata          -> deneme_sayisi artirilir, gorev BEKLIYOR'a doner,
                         ustel geri cekilme ile ertelenir; azami denemede HATA olur.
  KotaDoluHatasi      -> hata DEGIL erteleme; gorev bir sonraki pencereye kayar.
  TokenGecersizHatasi -> kuyruk durdurulur, insan mudahalesi beklenir.

ONEMLI: media_publish (yayinla) adimi ASLA kor korune tekrar denenmez;
ag hatasi alindiginda once konteyner durumu sorgulanir, PUBLISHED ise islem
basarili sayilir. Aksi halde ayni icerik iki kez yayinlanabilir.

Turkce karakter kullanilmaz (ASCII), konsol encoding sorunu cikmasin diye.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Final, Protocol, runtime_checkable

from src import db
from src.ayarlar import Ayarlar, ayarlari_yukle
from src.modeller import (
    Durum,
    GeciciHata,
    Gorev,
    KaliciHata,
    KotaDoluHatasi,
    TokenGecersizHatasi,
    YayinHatasi,
    simdi,
)

KAYIT: Final[logging.Logger] = logging.getLogger("insta.zamanlayici")

# --- Meta konteyner durum kodlari (birebir API degerleri) -------------------
DURUM_ISLENIYOR: Final[str] = "IN_PROGRESS"
DURUM_HAZIR: Final[str] = "FINISHED"
DURUM_YAYINDA: Final[str] = "PUBLISHED"
DURUM_HATALI: Final[str] = "ERROR"
DURUM_SURESI_DOLDU: Final[str] = "EXPIRED"

# --- Is (job) kimlikleri ----------------------------------------------------
IS_KUYRUK: Final[str] = "kuyruk_taramasi"
IS_TOKEN: Final[str] = "haftalik_token_kontrolu"
IS_ILK_TOKEN: Final[str] = "acilis_token_kontrolu"

#: Konteyneri kullanilamaz hale getiren alt kodlar: bu durumda ayni konteynere
#: tekrar yuklemek anlamsizdir, yeni konteyner acilmalidir.
KONTEYNER_SIFIRLAMA_ALT_KODLARI: Final[frozenset[int]] = frozenset(
    {2207008, 2207020, 2207032, 2207053}
)

#: Bir tarama turunda en fazla kac gorev yayinlansin (kota ve cagri limiti icin).
VARSAYILAN_TUR_BASINA_GOREV: Final[int] = 3

#: Gecici hatada bir sonraki denemeye kadar beklenecek azami dakika.
AZAMI_GERI_CEKILME_DK: Final[int] = 30


class KopruHatasi(RuntimeError):
    """Modul baglama (wiring) hatasi: instagram/arsiv modulu beklenen arayuzu sunmuyor.

    Bu bir MEDYA hatasi degildir; dosya hata/ klasorune TASINMAZ. Kuyruk
    durdurulur ve kullanicidan kodu duzeltmesi beklenir.
    """


# ===========================================================================
# 1) Kopru katmani - instagram.py ve arsiv.py modullerine baglanma
# ===========================================================================
#
# Bu modul, kardes modullerin fonksiyon adlarini ve imzalarini esnek cozer:
# fonksiyon adi icin esanlamlilar denenir, parametreler ise ADLARINA gore
# eslestirilir. Boylece instagram.py'nin imzasi biraz farkli yazilmis olsa da
# zamanlayici calisir; hicbir kardes dosyaya dokunmak gerekmez.

#: Mantiksal adim -> instagram.py icinde aranacak fonksiyon adlari (sirayla)
_FONKSIYON_ADLARI: Final[dict[str, tuple[str, ...]]] = {
    "yayin_limiti_kontrol": (
        "yayin_limiti_kontrol",
        "kota_kontrol",
        "kota_sorgula",
        "yayin_limitini_sorgula",
        "content_publishing_limit",
    ),
    "konteyner_olustur": (
        "konteyner_olustur",
        "kapsayici_olustur",
        "konteyner_ac",
        "medya_konteyneri_olustur",
    ),
    "dosya_yukle": (
        "dosya_yukle",
        "medya_yukle",
        "video_yukle",
        "videoyu_yukle",
        "konteynere_yukle",
    ),
    "durum_bekle": (
        "durum_bekle",
        "durumu_bekle",
        "kapsayici_hazir_bekle",
        "konteyner_hazir_bekle",
    ),
    # DIKKAT: 'yayinla_ayrintili' once denenir. instagram.py'deki 'yayinla'
    # sadece permalink dondurur; ayrintili surum medya kimligini de verir ve
    # medya kimligi olmadan veritabanina dogru kayit yazilamaz.
    "yayinla": (
        "yayinla_ayrintili",
        "yayinla",
        "medya_yayinla",
        "yayina_al",
        "media_publish",
    ),
    "token_gecerli_mi": (
        "token_gecerli_mi",
        "anahtar_gecerli_mi",
        "anahtari_dogrula",
        "token_dogrula",
    ),
    "durum_sorgula": (
        "durum_sorgula",
        "konteyner_durumu",
        "kapsayici_durumu",
        "yayin_dogrula",
    ),
    "permalink_getir": ("permalink_getir", "kalici_bag_getir", "permalink_al"),
}

#: arsiv.py icinde aranacak fonksiyon adlari
#: (ilk siradakiler arsiv.py'de gercekten tanimli olan adlardir)
_ARSIV_ADLARI: Final[dict[str, tuple[str, ...]]] = {
    "arsivle": (
        "basarili_tasi",
        "arsivle",
        "yayinlandiya_tasi",
        "basariliyi_tasi",
        "arsive_tasi",
    ),
    "hataya_tasi": (
        "hatali_tasi",
        "hataya_tasi",
        "hataya_al",
        "hata_klasorune_tasi",
        "hatayi_kaydet",
    ),
}

#: Deger anahtari -> kardes fonksiyonlarda karsilasilabilecek parametre adlari
_DEGER_ADLARI: Final[dict[str, tuple[str, ...]]] = {
    "ayar": ("ayar", "ayarlar", "yapilandirma"),
    "gorev": ("gorev", "is", "kayit"),
    "dosya_yolu": (
        "dosya_yolu",
        "dosya",
        "video_yolu",
        "medya_yolu",
        "yerel_dosya",
        "yol",
    ),
    "medya_tipi": ("medya_tipi", "media_type", "tip"),
    "aciklama": ("aciklama", "caption", "metin"),
    "konteyner_id": ("konteyner_id", "kapsayici_id", "container_id", "creation_id"),
    "yukleme_uri": ("yukleme_uri", "yukleme_adresi", "yukleme_url", "uri"),
    "token": ("token", "sayfa_tokeni", "erisim_tokeni", "access_token", "anahtar"),
    "ig_hesap_id": ("ig_hesap_id", "ig_kullanici_id", "ig_id", "instagram_id"),
    "medya_id": ("medya_id", "media_id"),
    "sebep": ("sebep", "hata_mesaji", "mesaj", "aciklama_metni"),
    # arsiv.hatali_tasi(gorev, hata, ...) gercek istisna nesnesi bekler; sadece
    # metin verilirse traceback okunamaz. Bu yuzden ayri bir anahtar tutulur.
    "hata": ("hata", "istisna", "hata_nesnesi"),
}

#: instagram.py sinif tabanli yazilmis olabilir; su adlar denenir.
_ISTEMCI_SINIF_ADLARI: Final[tuple[str, ...]] = (
    "InstagramIstemci",
    "InstagramIstemcisi",
    "InstagramAPI",
    "Instagram",
    "Yayinci",
)


def _modulu_yukle(ad: str) -> Any | None:
    """Kardes modulu ice aktarir; yoksa None doner (hata firlatmaz)."""
    try:
        return importlib.import_module(ad)
    except ModuleNotFoundError:
        return None


def _fonksiyon_coz(
    kaynaklar: tuple[Any, ...], adaylar: tuple[str, ...]
) -> Callable[..., Any] | None:
    """Verilen nesne/modullerde aday adlardan ilk bulunani dondurur."""
    for kaynak in kaynaklar:
        if kaynak is None:
            continue
        for ad in adaylar:
            islev = getattr(kaynak, ad, None)
            if callable(islev):
                return islev
    return None


def _deger_bul(parametre_adi: str, mevcut: dict[str, Any]) -> tuple[bool, Any]:
    """Parametre adina karsilik gelen degeri bulur. (bulundu_mu, deger)"""
    if parametre_adi in mevcut:
        return True, mevcut[parametre_adi]
    for anahtar, esanlamlilar in _DEGER_ADLARI.items():
        if parametre_adi in esanlamlilar and anahtar in mevcut:
            return True, mevcut[anahtar]
    return False, None


def _esleyerek_cagir(islev: Callable[..., Any], mevcut: dict[str, Any]) -> Any:
    """Fonksiyonu, imzasindaki parametre ADLARINA gore eslestirip cagirir.

    Kardes modulun imzasi tam olarak bilinmedigi icin parametreler ada gore
    baglanir. Eslesmeyen ve varsayilani da olmayan bir parametre varsa
    KopruHatasi firlatilir (bu bir kod baglama hatasidir, medya hatasi degil).
    """
    try:
        imza = inspect.signature(islev)
    except (TypeError, ValueError):  # C tarafinda tanimli fonksiyonlar
        return islev()

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for parametre in imza.parameters.values():
        if parametre.name == "self":
            continue
        if parametre.kind in (parametre.VAR_POSITIONAL, parametre.VAR_KEYWORD):
            continue
        bulundu, deger = _deger_bul(parametre.name, mevcut)
        if not bulundu:
            if parametre.default is not inspect.Parameter.empty:
                if parametre.kind is parametre.POSITIONAL_ONLY:
                    break  # konumsal zincir kirilir, kalani gonderilemez
                continue
            raise KopruHatasi(
                f"'{getattr(islev, '__qualname__', islev)}' fonksiyonunun "
                f"'{parametre.name}' parametresi zamanlayici tarafindan "
                "doldurulamadi. Kardes modulun imzasini kontrol edin "
                "(beklenen adlar: " + ", ".join(sorted(_DEGER_ADLARI)) + ")."
            )
        if parametre.kind is parametre.POSITIONAL_ONLY:
            args.append(deger)
        else:
            kwargs[parametre.name] = deger
    return islev(*args, **kwargs)


# --- Donen degerleri normallestirme ----------------------------------------


def _metin_veya_none(deger: Any) -> str | None:
    if deger is None:
        return None
    metin = str(deger).strip()
    return metin or None


def _nitelik_ara(sonuc: Any, adlar: tuple[str, ...]) -> Any:
    """Nesnedeki ilk dolu niteligi dondurur (dataclass yanitlari icin).

    instagram.py dataclass dondurur (Konteyner, KonteynerDurumu, YayinSonucu);
    sozluk/metin varsayan cozuculer bu yardimciyla nesneleri de anlar.
    """
    for ad in adlar:
        deger = getattr(sonuc, ad, None)
        if deger not in (None, ""):
            return deger
    return None


def _konteyner_sonucu_coz(sonuc: Any) -> tuple[str, str | None]:
    """konteyner_olustur ciktisini (konteyner_id, yukleme_uri) bicimine cevirir."""
    if isinstance(sonuc, str):
        return sonuc, None
    if isinstance(sonuc, dict):
        kimlik = None
        for anahtar in ("konteyner_id", "kapsayici_id", "container_id", "id"):
            if sonuc.get(anahtar):
                kimlik = str(sonuc[anahtar])
                break
        adres = None
        for anahtar in ("yukleme_uri", "yukleme_adresi", "uri", "yukleme_url"):
            if sonuc.get(anahtar):
                adres = str(sonuc[anahtar])
                break
        if not kimlik:
            raise KopruHatasi(
                "Konteyner olusturma yaniti bir kimlik icermiyor: " + repr(sonuc)
            )
        return kimlik, adres
    if isinstance(sonuc, (tuple, list)) and sonuc:
        kimlik = str(sonuc[0])
        adres = str(sonuc[1]) if len(sonuc) > 1 and sonuc[1] else None
        return kimlik, adres

    # Nesne (ornek: instagram.Konteyner dataclass'i)
    kimlik_nesne = _nitelik_ara(sonuc, ("id", "konteyner_id", "kapsayici_id", "container_id"))
    if kimlik_nesne is not None:
        adres_nesne = _nitelik_ara(
            sonuc, ("yukleme_adresi", "yukleme_uri", "uri", "yukleme_url")
        )
        return str(kimlik_nesne), (str(adres_nesne) if adres_nesne else None)

    raise KopruHatasi(
        "Konteyner olusturma yaniti anlasilamadi (metin, sozluk, ikili veya "
        "kimlik alani olan bir nesne bekleniyordu): " + repr(sonuc)
    )


def _durum_kodu_coz(sonuc: Any) -> str:
    """durum_bekle / durum_sorgula ciktisini status_code metnine cevirir.

    None donmesi 'basariyla tamamlandi' kabul edilir: bu tarz fonksiyonlar
    hata halinde istisna firlatir, basarida bir sey dondurmez.
    """
    if sonuc is None:
        return DURUM_HAZIR
    if isinstance(sonuc, bool):
        return DURUM_HAZIR if sonuc else DURUM_ISLENIYOR
    if isinstance(sonuc, str):
        return sonuc.strip().upper()
    if isinstance(sonuc, dict):
        for anahtar in ("status_code", "durum_kodu", "durum"):
            if sonuc.get(anahtar):
                return str(sonuc[anahtar]).strip().upper()
        return DURUM_ISLENIYOR
    if isinstance(sonuc, (tuple, list)) and sonuc:
        return str(sonuc[0]).strip().upper()

    # Nesne (ornek: instagram.KonteynerDurumu dataclass'i)
    nitelik = _nitelik_ara(sonuc, ("durum_kodu", "status_code", "durum"))
    if nitelik is not None:
        return str(nitelik).strip().upper()
    return str(sonuc).strip().upper()


def _medya_id_coz(sonuc: Any) -> str | None:
    """yayinla ciktisindan IG medya kimligini cikarir."""
    if sonuc is None:
        return None
    if isinstance(sonuc, str):
        # DIKKAT: eski 'yayinla' surumu permalink dondurur. Baglanti adresi
        # medya kimligi DEGILDIR; veritabanina yanlis deger yazilmasin.
        metin = _metin_veya_none(sonuc)
        if metin and metin.lower().startswith(("http://", "https://")):
            return None
        return metin
    if isinstance(sonuc, dict):
        for anahtar in ("medya_id", "media_id", "id"):
            if sonuc.get(anahtar):
                return str(sonuc[anahtar])
        return None
    if isinstance(sonuc, (tuple, list)) and sonuc:
        return _medya_id_coz(sonuc[0])

    # Nesne (ornek: instagram.YayinSonucu dataclass'i)
    nitelik = _nitelik_ara(sonuc, ("medya_id", "media_id", "id"))
    if nitelik is not None:
        return _metin_veya_none(nitelik)
    return _metin_veya_none(sonuc)


def _permalink_coz(sonuc: Any) -> str | None:
    """yayinla ciktisindan gonderi baglantisini (permalink) cikarir."""
    if sonuc is None:
        return None
    if isinstance(sonuc, str):
        metin = _metin_veya_none(sonuc)
        if metin and metin.lower().startswith(("http://", "https://")):
            return metin
        return None
    if isinstance(sonuc, dict):
        for anahtar in ("permalink", "baglanti", "kalici_bag", "url"):
            if sonuc.get(anahtar):
                return _metin_veya_none(sonuc[anahtar])
        return None
    if isinstance(sonuc, (tuple, list)) and sonuc:
        for oge in sonuc:
            bulunan = _permalink_coz(oge)
            if bulunan:
                return bulunan
        return None
    nitelik = _nitelik_ara(sonuc, ("permalink", "baglanti", "kalici_bag", "url"))
    return _metin_veya_none(nitelik) if nitelik is not None else None


@dataclass(frozen=True)
class KotaDurumu:
    """Meta yayin kotasinin anlik durumu (content_publishing_limit sonucu)."""

    kullanilan: int | None = None
    toplam: int | None = None
    uygun: bool | None = None   # API acikca 'yayin yapilabilir' dediyse
    kaynak: str = "meta"

    def dolu_mu(self) -> bool:
        if self.uygun is not None:
            return not self.uygun
        if self.kullanilan is None or self.toplam is None:
            return False  # bilinmiyorsa engelleme; yerel sayac zaten koruyor
        return self.kullanilan >= self.toplam

    def ozet(self) -> str:
        if self.kullanilan is None and self.toplam is None:
            return "Meta kotasi bilinmiyor"
        toplam = self.toplam if self.toplam is not None else "?"
        return f"Meta kotasi: {self.kullanilan}/{toplam}"


def _kota_coz(sonuc: Any) -> KotaDurumu:
    """yayin_limiti_kontrol ciktisini KotaDurumu nesnesine cevirir."""
    if sonuc is None:
        return KotaDurumu()
    if isinstance(sonuc, KotaDurumu):
        return sonuc
    if isinstance(sonuc, bool):
        return KotaDurumu(uygun=sonuc)
    if isinstance(sonuc, int):
        # Tek sayi donduyse 'kullanilan' kabul edilir; toplam bilinmez.
        return KotaDurumu(kullanilan=int(sonuc))
    if isinstance(sonuc, dict):
        kullanilan = sonuc.get("quota_usage")
        if kullanilan is None:
            kullanilan = sonuc.get("kullanilan")
        toplam = sonuc.get("quota_total")
        if toplam is None:
            toplam = sonuc.get("toplam")
        if toplam is None:
            yapilandirma = sonuc.get("config") or {}
            if isinstance(yapilandirma, dict):
                toplam = yapilandirma.get("quota_total")
        uygun = sonuc.get("uygun")
        return KotaDurumu(
            kullanilan=int(kullanilan) if kullanilan is not None else None,
            toplam=int(toplam) if toplam is not None else None,
            uygun=bool(uygun) if uygun is not None else None,
        )
    if isinstance(sonuc, (tuple, list)) and sonuc:
        kullanilan = int(sonuc[0]) if sonuc[0] is not None else None
        toplam = (
            int(sonuc[1]) if len(sonuc) > 1 and sonuc[1] is not None else None
        )
        return KotaDurumu(kullanilan=kullanilan, toplam=toplam)
    return KotaDurumu()


# --- Yayinci arayuzu --------------------------------------------------------


@runtime_checkable
class Yayinci(Protocol):
    """Zamanlayicinin ihtiyac duydugu yayin arayuzu (instagram.py bunu saglar)."""

    def yayin_limiti_kontrol(self) -> KotaDurumu: ...

    def konteyner_olustur(self, gorev: Gorev) -> tuple[str, str | None]: ...

    def dosya_yukle(
        self, gorev: Gorev, konteyner_id: str, yukleme_uri: str | None
    ) -> None: ...

    def durum_bekle(self, gorev: Gorev, konteyner_id: str) -> str: ...

    def yayinla(self, gorev: Gorev, konteyner_id: str) -> str | None: ...

    def durum_sorgula(self, gorev: Gorev, konteyner_id: str) -> str: ...

    def permalink_getir(self, medya_id: str) -> str | None: ...

    def token_gecerli_mi(self) -> bool: ...


class ModulYayinci:
    """src.instagram modulunu Yayinci arayuzune baglayan kopru sinifi."""

    def __init__(self, ayar: Ayarlar, modul: Any | None = None) -> None:
        self.ayar = ayar
        self._modul = modul if modul is not None else _modulu_yukle("src.instagram")
        if self._modul is None:
            raise KopruHatasi(
                "src/instagram.py bulunamadi. Yayin yapabilmek icin bu modul "
                "gereklidir (konteyner_olustur, dosya_yukle, durum_bekle, "
                "yayinla, yayin_limiti_kontrol, token_gecerli_mi)."
            )
        self._nesne = self._istemci_uret()
        #: yayinla() cagrisindan okunan son gonderi baglantisi (permalink)
        self.son_permalink: str | None = None

    # -- ic yardimcilar ------------------------------------------------------

    def _istemci_uret(self) -> Any | None:
        """Modul sinif tabanliysa istemci nesnesini uretir, degilse None.

        Sirayla denenir:
          1) Sinif.ayardan(ayar)  -- instagram.InstagramIstemcisi bunu sunar
          2) Sinif(ayar)
          3) Sinif()
        Hicbiri tutmazsa None doner ve modul duzeyindeki fonksiyonlar aranir.
        """
        son_hata: Exception | None = None
        for ad in _ISTEMCI_SINIF_ADLARI:
            sinif = getattr(self._modul, ad, None)
            if not inspect.isclass(sinif):
                continue
            fabrika = getattr(sinif, "ayardan", None)
            if callable(fabrika):
                try:
                    return fabrika(self.ayar)
                except Exception as hata:
                    # Ayarlar eksikse (token/hesap id bos) anlasilir hata verilir;
                    # sessizce modul fonksiyonu aramak kafa karistirir.
                    son_hata = hata
            try:
                return sinif(self.ayar)
            except TypeError:
                try:
                    return sinif()
                except Exception as hata:  # pragma: no cover - savunmaci
                    son_hata = hata
            except Exception as hata:  # pragma: no cover - savunmaci
                son_hata = hata
        if son_hata is not None:
            KAYIT.debug("Instagram istemcisi uretilemedi: %s", son_hata)
            self._istemci_hatasi = son_hata
        return None

    #: _istemci_uret() basarisiz olursa sebebini saklar (hata mesajinda gosterilir)
    _istemci_hatasi: Exception | None = None

    def _hazirlanan_yol(self, gorev: Gorev) -> Path:
        """Yuklenecek dosyayi uretir (gorsel -> video, moov duzeltmesi vb.).

        src/medya.py varsa yayina_hazirla() cagrilir; sonuc gorev.ekstra icinde
        onbelleklenir, boylece ayni gorev icin iki kez cevrim yapilmaz.
        medya modulu yoksa ozgun dosya yolu oldugu gibi kullanilir.
        """
        onbellek = gorev.ekstra.get("yuklenecek_yol")
        if onbellek is not None and Path(onbellek).is_file():
            return Path(onbellek)

        medya = _modulu_yukle("src.medya")
        hazirla = getattr(medya, "yayina_hazirla", None) if medya else None
        if hazirla is None:
            return Path(gorev.dosya_yolu)

        sonuc = hazirla(gorev.dosya_yolu, gorev.medya_tipi)
        yol = Path(str(sonuc[0])) if isinstance(sonuc, tuple) else Path(str(sonuc))
        gorev.ekstra["yuklenecek_yol"] = yol
        if yol != Path(gorev.dosya_yolu):
            KAYIT.info(
                "Dosya yayina hazirlandi: %s -> %s", gorev.dosya_adi, yol.name
            )
        return yol

    def temizle(self, gorev: Gorev) -> None:
        """Gorev icin uretilmis gecici dosyalari siler (yayin bitince cagrilir)."""
        yol = gorev.ekstra.pop("yuklenecek_yol", None)
        if yol is None:
            return
        medya = _modulu_yukle("src.medya")
        if medya is None:
            return
        try:
            if medya.gecici_dosya_mi(yol):
                medya.gecici_dosyayi_sil(yol)
        except Exception as hata:  # pragma: no cover - savunmaci
            KAYIT.debug("Gecici dosya temizlenemedi: %s (%s)", yol, hata)

    def _islev(self, anahtar: str, zorunlu: bool = True) -> Callable[..., Any] | None:
        islev = _fonksiyon_coz(
            (self._nesne, self._modul), _FONKSIYON_ADLARI[anahtar]
        )
        if islev is None and zorunlu:
            ek = ""
            if self._nesne is None and self._istemci_hatasi is not None:
                ek = (
                    " | Instagram istemcisi olusturulamadi: "
                    + str(self._istemci_hatasi)
                )
            raise KopruHatasi(
                f"src/instagram.py icinde '{anahtar}' adimi bulunamadi. "
                "Beklenen adlardan biri tanimlanmali: "
                + ", ".join(_FONKSIYON_ADLARI[anahtar])
                + ek
            )
        return islev

    def _ortak_degerler(self, gorev: Gorev | None = None) -> dict[str, Any]:
        degerler: dict[str, Any] = {
            "ayar": self.ayar,
            "token": self.ayar.token,
            "ig_hesap_id": self.ayar.ig_hesap_id,
        }
        if gorev is not None:
            degerler.update(
                {
                    "gorev": gorev,
                    "dosya_yolu": gorev.dosya_yolu,
                    "medya_tipi": gorev.medya_tipi,
                    "aciklama": gorev.aciklama,
                }
            )
        return degerler

    # -- Yayinci arayuzu -----------------------------------------------------

    def yayin_limiti_kontrol(self) -> KotaDurumu:
        islev = self._islev("yayin_limiti_kontrol")
        return _kota_coz(_esleyerek_cagir(islev, self._ortak_degerler()))

    def konteyner_olustur(self, gorev: Gorev) -> tuple[str, str | None]:
        islev = self._islev("konteyner_olustur")
        degerler = self._ortak_degerler(gorev)
        # Yuklenecek dosya ozgun dosyadan farkli olabilir (gorsel -> video,
        # moov atom duzeltmesi). Yerel dogrulama da bu yol uzerinden yapilmali.
        degerler["dosya_yolu"] = self._hazirlanan_yol(gorev)
        return _konteyner_sonucu_coz(_esleyerek_cagir(islev, degerler))

    def dosya_yukle(
        self, gorev: Gorev, konteyner_id: str, yukleme_uri: str | None
    ) -> None:
        islev = self._islev("dosya_yukle")
        degerler = self._ortak_degerler(gorev)
        degerler["dosya_yolu"] = self._hazirlanan_yol(gorev)
        degerler["konteyner_id"] = konteyner_id
        degerler["yukleme_uri"] = yukleme_uri or (
            f"{self.ayar.rupload_kok}/{konteyner_id}"
        )
        _esleyerek_cagir(islev, degerler)

    def durum_bekle(self, gorev: Gorev, konteyner_id: str) -> str:
        islev = self._islev("durum_bekle")
        degerler = self._ortak_degerler(gorev)
        degerler["konteyner_id"] = konteyner_id
        return _durum_kodu_coz(_esleyerek_cagir(islev, degerler))

    def yayinla(self, gorev: Gorev, konteyner_id: str) -> str | None:
        islev = self._islev("yayinla")
        degerler = self._ortak_degerler(gorev)
        degerler["konteyner_id"] = konteyner_id
        sonuc = _esleyerek_cagir(islev, degerler)
        # Baglanti ayni yanitta geliyorsa ikinci bir API cagrisi yapilmaz.
        self.son_permalink = _permalink_coz(sonuc)
        return _medya_id_coz(sonuc)

    def durum_sorgula(self, gorev: Gorev, konteyner_id: str) -> str:
        """Tek seferlik durum sorgusu; ayri fonksiyon yoksa durum_bekle kullanilir."""
        islev = self._islev("durum_sorgula", zorunlu=False)
        if islev is None:
            islev = self._islev("durum_bekle")
        degerler = self._ortak_degerler(gorev)
        degerler["konteyner_id"] = konteyner_id
        return _durum_kodu_coz(_esleyerek_cagir(islev, degerler))

    def permalink_getir(self, medya_id: str) -> str | None:
        islev = self._islev("permalink_getir", zorunlu=False)
        if islev is None:
            # Ayri bir permalink fonksiyonu yok: yayin yanitindan okunan
            # baglanti kullanilir (instagram.py bunu birlikte donduruyor).
            if self.son_permalink:
                return self.son_permalink
            return _permalink_coz(getattr(self._nesne, "son_yayin", None))
        degerler = self._ortak_degerler()
        degerler["medya_id"] = medya_id
        return _metin_veya_none(_esleyerek_cagir(islev, degerler))

    def token_gecerli_mi(self) -> bool:
        islev = self._islev("token_gecerli_mi")
        sonuc = _esleyerek_cagir(islev, self._ortak_degerler())
        if sonuc is None:
            return True  # istisna firlatmadiysa gecerli kabul edilir
        if isinstance(sonuc, dict):
            return bool(sonuc.get("is_valid", True))
        return bool(sonuc)


# --- Arsivci arayuzu --------------------------------------------------------


@runtime_checkable
class Arsivci(Protocol):
    """Yayinlanan/basarisiz dosyalarin tasinmasi (arsiv.py bunu saglar)."""

    def arsivle(self, gorev: Gorev) -> Path | None: ...

    def hataya_tasi(
        self, gorev: Gorev, sebep: str, hata: BaseException | None = None
    ) -> Path | None: ...


def _benzersiz_hedef(hedef_klasor: Path, dosya_adi: str) -> Path:
    """Ayni adda dosya varsa sonuna sayi ekleyerek benzersiz yol uretir."""
    hedef = hedef_klasor / dosya_adi
    if not hedef.exists():
        return hedef
    govde = Path(dosya_adi).stem
    uzanti = Path(dosya_adi).suffix
    sayac = 2
    while True:
        aday = hedef_klasor / f"{govde}_{sayac}{uzanti}"
        if not aday.exists():
            return aday
        sayac += 1


def _dosyayi_tasi(kaynak: Path, hedef_klasor: Path) -> Path | None:
    """Dosyayi (ve varsa yanindaki .txt aciklamasini) hedef klasore tasir."""
    if not kaynak.exists():
        KAYIT.warning("Tasinacak dosya bulunamadi: %s", kaynak)
        return None
    hedef_klasor.mkdir(parents=True, exist_ok=True)
    hedef = _benzersiz_hedef(hedef_klasor, kaynak.name)
    shutil.move(str(kaynak), str(hedef))
    aciklama_dosyasi = kaynak.with_suffix(".txt")
    if aciklama_dosyasi.exists():
        try:
            shutil.move(
                str(aciklama_dosyasi),
                str(hedef.with_suffix(".txt")),
            )
        except OSError as hata:  # aciklama tasinamazsa yayin yine de basarilidir
            KAYIT.warning("Aciklama dosyasi tasinamadi: %s (%s)", aciklama_dosyasi, hata)
    return hedef


class ModulArsivci:
    """src.arsiv modulunu kullanir; modul yoksa dosyayi kendisi tasir."""

    def __init__(self, ayar: Ayarlar, modul: Any | None = None) -> None:
        self.ayar = ayar
        self._modul = modul if modul is not None else _modulu_yukle("src.arsiv")

    def _islev(self, anahtar: str) -> Callable[..., Any] | None:
        if self._modul is None:
            return None
        return _fonksiyon_coz((self._modul,), _ARSIV_ADLARI[anahtar])

    def arsivle(self, gorev: Gorev) -> Path | None:
        islev = self._islev("arsivle")
        if islev is not None:
            sonuc = _esleyerek_cagir(
                islev,
                {
                    "ayar": self.ayar,
                    "gorev": gorev,
                    "dosya_yolu": gorev.dosya_yolu,
                    "medya_tipi": gorev.medya_tipi,
                },
            )
            return Path(str(sonuc)) if sonuc else None
        # Yedek yol: yayinlandi/YYYY-MM/ altina tasi
        yerel = simdi().astimezone(self.ayar.zaman_dilimi)
        return _dosyayi_tasi(
            gorev.dosya_yolu, self.ayar.klasorler.yayinlandi / yerel.strftime("%Y-%m")
        )

    def hataya_tasi(
        self, gorev: Gorev, sebep: str, hata: BaseException | None = None
    ) -> Path | None:
        islev = self._islev("hataya_tasi")
        if islev is not None:
            # arsiv.hatali_tasi() gercek bir istisna nesnesi bekler (traceback ve
            # Meta alt kodu icin). Elde sadece metin varsa sarmalanir.
            istisna: BaseException = hata if hata is not None else KaliciHata(str(sebep))
            sonuc = _esleyerek_cagir(
                islev,
                {
                    "ayar": self.ayar,
                    "gorev": gorev,
                    "dosya_yolu": gorev.dosya_yolu,
                    "sebep": sebep,
                    "hata": istisna,
                    "db_yolu": self.ayar.db_yolu,
                },
            )
            return Path(str(sonuc)) if sonuc else None
        hedef = _dosyayi_tasi(gorev.dosya_yolu, self.ayar.klasorler.hata)
        if hedef is not None:
            not_dosyasi = hedef.with_suffix(hedef.suffix + ".log")
            try:
                not_dosyasi.write_text(
                    f"Gorev: {gorev.ozet()}\nZaman: {simdi().isoformat()}\n"
                    f"Sebep: {sebep}\n",
                    encoding="utf-8",
                )
            except OSError as hata:  # pragma: no cover - savunmaci
                KAYIT.warning("Hata notu yazilamadi: %s (%s)", not_dosyasi, hata)
        return hedef


# ===========================================================================
# 2) Zamanlayici
# ===========================================================================


def _buyuk_uyari(baslik: str, satirlar: list[str]) -> str:
    """Loglarda gozden kacmayacak cerceveli uyari metni uretir (ASCII)."""
    cizgi = "=" * 72
    govde = "\n".join("  " + satir for satir in satirlar)
    return f"\n{cizgi}\n  {baslik}\n{cizgi}\n{govde}\n{cizgi}"


class Zamanlayici:
    """Kuyrugu tarayan, slot dagitan ve yayin akisini yoneten orkestrator."""

    def __init__(
        self,
        ayar: Ayarlar | None = None,
        *,
        yayinci: Yayinci | None = None,
        arsivci: Arsivci | None = None,
        tur_basina_gorev: int = VARSAYILAN_TUR_BASINA_GOREV,
        ilk_token_kontrolu: bool = True,
    ) -> None:
        self.ayar: Ayarlar = ayar if ayar is not None else ayarlari_yukle()
        self.db_yolu: Path = self.ayar.db_yolu
        self.tur_basina_gorev: int = max(1, int(tur_basina_gorev))
        self.ilk_token_kontrolu: bool = ilk_token_kontrolu

        self._yayinci: Yayinci | None = yayinci
        self._arsivci: Arsivci | None = arsivci
        self._planlayici: Any | None = None
        self._kilit: threading.Lock = threading.Lock()

        #: Kuyruk durduruldu mu (token gecersiz / baglama hatasi)
        self.kuyruk_durduruldu: bool = False
        self.durdurma_sebebi: str | None = None
        #: Kota dolu oldugunda bu zamana kadar yeni yayin denenmez
        self.kota_bekleme_zamani: datetime | None = None

    # -- tembel (lazy) bagimliliklar -----------------------------------------

    @property
    def yayinci(self) -> Yayinci:
        """instagram koprusu; ilk kullanimda kurulur (test icin disaridan verilebilir)."""
        if self._yayinci is None:
            self._yayinci = ModulYayinci(self.ayar)
        return self._yayinci

    @property
    def arsivci(self) -> Arsivci:
        if self._arsivci is None:
            self._arsivci = ModulArsivci(self.ayar)
        return self._arsivci

    # -- kucuk yardimcilar ---------------------------------------------------

    def _yerel_metin(self, an: datetime | None) -> str:
        """Zamani kullaniciya gosterilecek yerel bicime cevirir."""
        if an is None:
            return "-"
        return an.astimezone(self.ayar.zaman_dilimi).strftime("%Y-%m-%d %H:%M")

    def _kuyrugu_durdur(self, sebep: str) -> None:
        self.kuyruk_durduruldu = True
        self.durdurma_sebebi = sebep

    def kuyrugu_devam_ettir(self) -> None:
        """Sorun giderildikten sonra kuyrugu yeniden calistirir."""
        self.kuyruk_durduruldu = False
        self.durdurma_sebebi = None
        KAYIT.info("Kuyruk yeniden calistirildi.")

    # -- APScheduler kurulumu ------------------------------------------------

    def _planlayici_olustur(self, engelleyici: bool) -> Any:
        """APScheduler nesnesini uretir (paket ice aktarimi tembel yapilir)."""
        try:
            if engelleyici:
                from apscheduler.schedulers.blocking import BlockingScheduler as Sinif
            else:
                from apscheduler.schedulers.background import (
                    BackgroundScheduler as Sinif,
                )
        except ImportError as hata:  # pragma: no cover - kurulum sorunu
            raise KopruHatasi(
                "APScheduler paketi bulunamadi. Kurulum: "
                "python -m pip install -r requirements.txt"
            ) from hata
        return Sinif(timezone=self.ayar.zamanlama.zaman_dilimi)

    def isleri_ekle(self, planlayici: Any) -> None:
        """Kuyruk taramasi ve haftalik token kontrolu islerini ekler."""
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.date import DateTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        aralik = int(self.ayar.zamanlama.tarama_araligi_sn)
        planlayici.add_job(
            self.kuyrugu_isle,
            trigger=IntervalTrigger(seconds=aralik),
            id=IS_KUYRUK,
            name="Kuyruk taramasi",
            max_instances=1,      # ust uste calismasin
            coalesce=True,        # kacirilan turlar birlestirilsin
            misfire_grace_time=max(30, aralik),
            replace_existing=True,
        )
        planlayici.add_job(
            self.token_kontrol_isi,
            trigger=CronTrigger(
                day_of_week="mon",
                hour=9,
                minute=0,
                timezone=self.ayar.zamanlama.zaman_dilimi,
            ),
            id=IS_TOKEN,
            name="Haftalik erisim anahtari kontrolu",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
            replace_existing=True,
        )
        if self.ilk_token_kontrolu:
            # Acilista da bir kez bakilir; token olu ise gece yarisi degil
            # program acilirken ogrenilmesi daha iyidir.
            planlayici.add_job(
                self.token_kontrol_isi,
                trigger=DateTrigger(
                    run_date=datetime.now(self.ayar.zaman_dilimi) + timedelta(seconds=10)
                ),
                id=IS_ILK_TOKEN,
                name="Acilis erisim anahtari kontrolu",
                max_instances=1,
                replace_existing=True,
            )

    def basla(self, *, engelleyici: bool = False) -> Any:
        """Veritabanini hazirlar, yarim isleri toparlar ve zamanlayiciyi baslatir."""
        self.ayar.klasorleri_hazirla()
        db.veritabanini_hazirla(self.db_yolu)
        self.baslangic_toparlamasi()

        KAYIT.info(
            "Zamanlayici basliyor | tarama: %s sn | slotlar: %s | gunluk limit: %s",
            self.ayar.zamanlama.tarama_araligi_sn,
            ", ".join(self.ayar.zamanlama.yayin_slotlari),
            self.ayar.yayin.gunluk_limit,
        )
        hazir, eksik = self.ayar.yayina_hazir_mi()
        if not hazir:
            KAYIT.warning(
                _buyuk_uyari(
                    "AYARLAR EKSIK - YAYIN YAPILAMAZ",
                    [
                        "config/.env dosyasinda su alanlar bos: " + ", ".join(eksik),
                        "Ornek dosya: config/.env.ornek",
                        "Kuyruk calisiyor ama yayin denemeleri hata verecek.",
                    ],
                )
            )

        self._planlayici = self._planlayici_olustur(engelleyici)
        self.isleri_ekle(self._planlayici)
        self._planlayici.start()
        return self._planlayici

    def durdur(self, *, bekle: bool = True) -> None:
        """Zamanlayiciyi durdurur (calisan is varsa bitmesini bekler)."""
        if self._planlayici is None:
            return
        try:
            self._planlayici.shutdown(wait=bekle)
        finally:
            self._planlayici = None
            KAYIT.info("Zamanlayici durduruldu.")

    # -- acilista yarim kalan isler -----------------------------------------

    def baslangic_toparlamasi(self) -> int:
        """Program cokmus olabilir: yarim kalan gorevleri kullanilabilir hale getirir.

        Konteyneri 24 saati doldurmus (EXPIRED) gorevler bastan alinir; digerleri
        kaldigi adimdan devam eder.
        """
        yarim = db.bekleyenleri_getir(
            self.db_yolu, durumlar=[Durum.YUKLENIYOR, Durum.HAZIRLANIYOR]
        )
        toparlanan = 0
        for gorev in yarim:
            if gorev.kapsayici_suresi_doldu_mu():
                KAYIT.warning(
                    "Konteyner suresi dolmus (24 saat), gorev bastan alinacak: %s",
                    gorev.ozet(),
                )
                self._konteyneri_sifirla(gorev, deneme_artir=False)
                toparlanan += 1
            else:
                KAYIT.info("Yarim kalan gorev kaldigi yerden surdurulecek: %s", gorev.ozet())
        if toparlanan:
            KAYIT.info("Acilista %d gorev bastan alinmak uzere kuyruga dondu.", toparlanan)
        return toparlanan

    def _konteyneri_sifirla(self, gorev: Gorev, *, deneme_artir: bool) -> None:
        """Konteyner bilgisini temizler ve gorevi BEKLIYOR durumuna dondurur."""
        db.durum_guncelle(
            self.db_yolu,
            int(gorev.id or 0),
            Durum.BEKLIYOR,
            container_id=None,
            container_zamani=None,
            deneme_artir=deneme_artir,
        )
        gorev.container_id = None
        gorev.container_zamani = None
        gorev.durum = Durum.BEKLIYOR

    # -- slot dagitimi -------------------------------------------------------

    def slotlari_dagit(self, an: datetime | None = None) -> int:
        """Zamansiz gorevleri gunluk yayin slotlarina sirayla yerlestirir.

        Dosya adinda tarih/saat olmayan gorevlerin planlanan_zaman alani bostur.
        Bunlar ayarlar.yaml'daki slotlarin (ornek 12:00, 19:00) ilk bos olanina,
        eklenme sirasina gore konur. Ayni slota iki gorev yazilmaz.

        Doner: yerlestirilen gorev sayisi.
        """
        su_an = an or simdi()
        bekleyenler = db.bekleyenleri_getir(self.db_yolu)
        zamansizlar = [
            gorev
            for gorev in bekleyenler
            if gorev.planlanan_zaman is None and gorev.durum is Durum.BEKLIYOR
        ]
        if not zamansizlar:
            return 0

        dolu_slotlar: set[datetime] = {
            gorev.planlanan_zaman
            for gorev in bekleyenler
            if gorev.planlanan_zaman is not None
        }
        aday = self.ayar.sonraki_slot(su_an)
        yerlestirilen = 0
        for gorev in zamansizlar:
            while aday in dolu_slotlar:
                aday = self.ayar.sonraki_slot(aday)
            db.durum_guncelle(
                self.db_yolu, int(gorev.id or 0), planlanan_zaman=aday
            )
            gorev.planlanan_zaman = aday
            dolu_slotlar.add(aday)
            yerlestirilen += 1
            KAYIT.info(
                "Gorev yayin slotuna yerlestirildi: %s -> %s",
                gorev.dosya_adi,
                self._yerel_metin(aday),
            )
            aday = self.ayar.sonraki_slot(aday)
        return yerlestirilen

    # -- yayin limiti --------------------------------------------------------

    def yayin_limiti_kontrol(self, an: datetime | None = None) -> tuple[bool, str]:
        """Yayin yapilabilir mi? (uygun_mu, Turkce aciklama)

        Once YEREL sayac (son 24 saatte yayinlanan gorev sayisi) bakilir, sonra
        ayarlar izin veriyorsa Meta'nin content_publishing_limit ucu sorulur.
        Meta sorgusu basarisiz olursa yayin engellenmez; yerel sayac koruma saglar.
        """
        su_an = an or simdi()
        yerel = db.son_24_saatte_yayinlanan_sayisi(self.db_yolu, su_an)
        sinir = int(self.ayar.yayin.gunluk_limit)
        if yerel >= sinir:
            return False, (
                f"Gunluk yayin limiti dolu: son 24 saatte {yerel}/{sinir} yayin "
                "yapildi (ayarlar.yaml -> yayin.gunluk_limit)."
            )

        if not self.ayar.yayin.kota_sorgula:
            return True, f"Yerel sayac: {yerel}/{sinir}, Meta kotasi sorgulanmadi."

        try:
            kota = _kota_coz(self.yayinci.yayin_limiti_kontrol())
        except TokenGecersizHatasi as hata:
            self._token_hatasi_isle(hata)
            return False, "Erisim anahtari gecersiz oldugu icin yayin durduruldu."
        except KopruHatasi as hata:
            self._kopru_hatasi_isle(hata)
            return False, "Modul baglama hatasi nedeniyle yayin durduruldu."
        except Exception as hata:
            KAYIT.warning(
                "Meta yayin kotasi sorgulanamadi, yerel sayac ile devam ediliyor: %s",
                hata,
            )
            return True, f"Yerel sayac: {yerel}/{sinir} (Meta kotasi okunamadi)."

        if kota.dolu_mu():
            return False, (
                "Meta yayin kotasi dolu (" + kota.ozet() + "). Yayin ertelendi; "
                "kota 24 saatlik kayan penceredir, gece yarisi sifirlanmaz."
            )
        return True, f"Yerel sayac: {yerel}/{sinir} | {kota.ozet()}"

    # -- ana tarama turu -----------------------------------------------------

    def kuyrugu_isle(self) -> None:
        """APScheduler isi: kuyrugu tarar ve sirasi gelen gorevleri yayinlar."""
        if self.kuyruk_durduruldu:
            KAYIT.error(
                "Kuyruk durduruldu, yayin denenmiyor. Sebep: %s "
                "(sorun giderildikten sonra program yeniden baslatilmali)",
                self.durdurma_sebebi,
            )
            return
        if not self._kilit.acquire(blocking=False):
            KAYIT.debug("Onceki kuyruk taramasi hala suruyor, bu tur atlandi.")
            return
        try:
            self._turu_calistir()
        except Exception as hata:  # zamanlayici isi asla cokmemeli
            KAYIT.exception("Kuyruk taramasinda beklenmeyen hata: %s", hata)
        finally:
            self._kilit.release()

    def _turu_calistir(self) -> None:
        su_an = simdi()
        self.slotlari_dagit(su_an)

        if self.kota_bekleme_zamani is not None:
            if su_an < self.kota_bekleme_zamani:
                KAYIT.info(
                    "Yayin kotasi dolu oldugu icin bekleniyor. Yeniden deneme: %s",
                    self._yerel_metin(self.kota_bekleme_zamani),
                )
                return
            self.kota_bekleme_zamani = None

        uygun, aciklama = self.yayin_limiti_kontrol(su_an)
        if not uygun:
            KAYIT.warning("Yayin yapilmadi: %s", aciklama)
            return
        KAYIT.debug("Yayin limiti uygun: %s", aciklama)

        islenen = 0
        onceki_id: int | None = None
        while islenen < self.tur_basina_gorev:
            gorev = db.siradaki_gorevi_al(
                self.db_yolu,
                azami_deneme=int(self.ayar.yayin.azami_deneme),
                sahiplen=True,
            )
            if gorev is None:
                break
            if gorev.id is not None and gorev.id == onceki_id:
                # Ayni gorev tekrar geldi (gecici hata sonrasi): bu turda birak.
                KAYIT.debug("Ayni gorev tekrar sirada, tur sonlandiriliyor: %s", gorev.ozet())
                break
            onceki_id = gorev.id

            yayinlandi = self._gorevi_yayinla(gorev)
            islenen += 1

            if self.kuyruk_durduruldu or self.kota_bekleme_zamani is not None:
                break
            if yayinlandi:
                uygun, aciklama = self.yayin_limiti_kontrol()
                if not uygun:
                    KAYIT.warning("Sonraki yayin yapilmadi: %s", aciklama)
                    break

    # -- tek gorevin yayin akisi --------------------------------------------

    def _gorevi_yayinla(self, gorev: Gorev) -> bool:
        """Bir gorevi bastan sona yayinlar. Doner: yayin gerceklesti mi.

        Akis: konteyner_olustur -> dosya_yukle -> durum_bekle -> yayinla -> arsivle
        Her adimdan sonra veritabani guncellenir; cokme halinde gorev kaldigi
        adimdan devam eder.
        """
        gorev_id = int(gorev.id or 0)
        KAYIT.info("Yayin akisi basliyor: %s", gorev.ozet())
        self._gecikmeyi_bildir(gorev)
        try:
            # --- 0) dosya duruyor mu, konteyner hala gecerli mi ---
            if not gorev.dosya_yolu.exists():
                raise KaliciHata(
                    "Dosya bulunamadi, gorev iptal edildi: " + str(gorev.dosya_yolu)
                )
            if gorev.container_id and gorev.kapsayici_suresi_doldu_mu():
                KAYIT.warning(
                    "Konteynerin 24 saatlik suresi dolmus, yeniden olusturulacak: %s",
                    gorev.ozet(),
                )
                self._konteyneri_sifirla(gorev, deneme_artir=False)

            # --- 1) konteyner olustur (yerel dosya: video_url GONDERILMEZ) ---
            yukleme_uri: str | None = gorev.ekstra.get("yukleme_uri")
            if not gorev.container_id:
                # Donen deger burada da normallestirilir: disaridan verilen
                # (test/sahte) yayincilar farkli bicimde donebilir.
                konteyner_id, yukleme_uri = _konteyner_sonucu_coz(
                    self.yayinci.konteyner_olustur(gorev)
                )
                gorev.container_id = konteyner_id
                gorev.container_zamani = simdi()
                gorev.ekstra["yukleme_uri"] = yukleme_uri
                db.durum_guncelle(
                    self.db_yolu,
                    gorev_id,
                    Durum.YUKLENIYOR,
                    container_id=konteyner_id,
                    container_zamani=gorev.container_zamani,
                    hata_mesaji=None,
                )
                gorev.durum = Durum.YUKLENIYOR
                KAYIT.info("Konteyner olusturuldu: %s | %s", konteyner_id, gorev.dosya_adi)
            else:
                KAYIT.info(
                    "Mevcut konteyner kullaniliyor: %s | %s",
                    gorev.container_id,
                    gorev.dosya_adi,
                )

            konteyner_id = str(gorev.container_id)

            # --- 2) dosyayi rupload'a yukle (zaten yuklendiyse atlanir) ---
            if gorev.durum is not Durum.HAZIRLANIYOR:
                KAYIT.info("Dosya yukleniyor: %s", gorev.dosya_adi)
                self.yayinci.dosya_yukle(gorev, konteyner_id, yukleme_uri)
                db.durum_guncelle(self.db_yolu, gorev_id, Durum.HAZIRLANIYOR)
                gorev.durum = Durum.HAZIRLANIYOR
                KAYIT.info("Dosya yuklendi, Meta isleme aliyor: %s", gorev.dosya_adi)
            else:
                KAYIT.info("Dosya zaten yuklenmis, isleme durumu sorgulanacak: %s", gorev.dosya_adi)

            # --- 3) status_code FINISHED olana kadar bekle ---
            durum_kodu = _durum_kodu_coz(self.yayinci.durum_bekle(gorev, konteyner_id))
            if durum_kodu == DURUM_YAYINDA:
                # Onceki calismada yayinlanmis ama kayit guncellenememis olabilir.
                KAYIT.warning(
                    "Konteyner zaten yayinlanmis (PUBLISHED), tekrar yayinlanmayacak: %s",
                    gorev.dosya_adi,
                )
                self._yayini_tamamla(gorev, medya_id=None)
                return True
            if durum_kodu == DURUM_SURESI_DOLDU:
                raise GeciciHata(
                    "Konteynerin suresi doldu (EXPIRED), yeni konteyner ile "
                    "tekrar denenecek.",
                    alt_kod=2207020,
                )
            if durum_kodu == DURUM_HATALI:
                raise GeciciHata(
                    "Meta videoyu isleyemedi (status_code=ERROR). Dosya "
                    "ozelliklerini kontrol edin: MP4/MOV, H264/HEVC, AAC, "
                    "moov atom dosya basinda (ffmpeg -movflags +faststart).",
                    alt_kod=2207032,
                )
            if durum_kodu != DURUM_HAZIR:
                # Hala IN_PROGRESS: gorev HAZIRLANIYOR durumunda kalir, bir
                # sonraki turda tekrar sorgulanir (konteyner 24 saat gecerli).
                KAYIT.info(
                    "Video hala isleniyor (%s), bir sonraki turda tekrar bakilacak: %s",
                    durum_kodu,
                    gorev.dosya_adi,
                )
                return False

            # --- 4) yayinla (ASLA kor korune tekrar denenmez) ---
            medya_id = self._guvenli_yayinla(gorev, konteyner_id)
            self._yayini_tamamla(gorev, medya_id=medya_id)
            return True

        except KotaDoluHatasi as hata:
            self._kota_hatasi_isle(gorev, hata)
        except TokenGecersizHatasi as hata:
            self._token_hatasi_isle(hata, gorev)
        except KopruHatasi as hata:
            self._kopru_hatasi_isle(hata, gorev)
        except KaliciHata as hata:
            self._kalici_hata_isle(gorev, str(hata), hata)
        except GeciciHata as hata:
            self._gecici_hata_isle(gorev, hata)
        except YayinHatasi as hata:  # taban sinif: kalici bayragina bak
            if hata.kalici:
                self._kalici_hata_isle(gorev, str(hata), hata)
            else:
                self._gecici_hata_isle(gorev, hata)
        except Exception as hata:  # ag/dosya/bilinmeyen: gecici kabul edilir
            KAYIT.exception("Beklenmeyen hata: %s | %s", gorev.ozet(), hata)
            self._gecici_hata_isle(gorev, GeciciHata(str(hata)))
        finally:
            # Gorsel -> video cevriminden kalan gecici dosyalar temizlenir.
            # Gorev hala HAZIRLANIYOR ise dosya zaten yuklenmistir, gecici
            # kopyaya ihtiyac kalmaz.
            # DIKKAT: self.yayinci degil self._yayinci; kopru kurulamadiysa
            # finally icinde ikinci bir istisna firlatmamali.
            temizle = getattr(self._yayinci, "temizle", None)
            if callable(temizle):
                try:
                    temizle(gorev)
                except Exception as hata:  # pragma: no cover - savunmaci
                    KAYIT.debug("Gecici dosya temizligi basarisiz: %s", hata)
        return False

    def _gecikmeyi_bildir(self, gorev: Gorev) -> None:
        """Planlanan saatten cok sonra yayinlaniyorsa Turkce uyari loglar."""
        if gorev.planlanan_zaman is None:
            return
        tolerans = int(self.ayar.zamanlama.slot_tolerans_dk)
        gecikme = simdi() - gorev.planlanan_zaman
        if gecikme > timedelta(minutes=tolerans):
            KAYIT.warning(
                "Gorev planlanan saatten %d dakika sonra yayinlaniyor "
                "(planlanan: %s): %s",
                int(gecikme.total_seconds() // 60),
                self._yerel_metin(gorev.planlanan_zaman),
                gorev.dosya_adi,
            )

    def _guvenli_yayinla(self, gorev: Gorev, konteyner_id: str) -> str | None:
        """media_publish cagrisi; ag hatasinda TEKRAR DENEMEZ, durumu dogrular.

        Meta bu cagri icin idempotency anahtari sunmaz: istek zaman asimina
        ugrasa bile sunucuda basarili olmus olabilir. Bu yuzden once konteyner
        durumu sorgulanir; PUBLISHED ise islem basarili sayilir.
        """
        try:
            return _medya_id_coz(self.yayinci.yayinla(gorev, konteyner_id))
        except (KaliciHata, KotaDoluHatasi, TokenGecersizHatasi, KopruHatasi):
            raise
        except Exception as hata:
            KAYIT.warning(
                "Yayin cagrisindan yanit alinamadi, cift yayin riskine karsi "
                "konteyner durumu sorgulanacak: %s",
                hata,
            )
            try:
                durum_kodu = _durum_kodu_coz(
                    self.yayinci.durum_sorgula(gorev, konteyner_id)
                )
            except Exception as sorgu_hatasi:
                KAYIT.error(
                    "Konteyner durumu da sorgulanamadi. Gorev beklemede birakiliyor, "
                    "cift yayin olmamasi icin otomatik tekrar YAPILMAYACAK: %s",
                    sorgu_hatasi,
                )
                raise KaliciHata(
                    "Yayin sonucu dogrulanamadi. Instagram hesabini elle kontrol "
                    "edin: icerik yayinlanmis olabilir. Dosya: " + gorev.dosya_adi
                ) from hata
            if durum_kodu == DURUM_YAYINDA:
                KAYIT.warning(
                    "Yayin aslinda gerceklesmis (PUBLISHED). Tekrar yayinlanmayacak: %s",
                    gorev.dosya_adi,
                )
                return None
            raise

    def _yayini_tamamla(self, gorev: Gorev, *, medya_id: str | None) -> None:
        """Yayin basarili: veritabanini gunceller ve dosyayi arsive tasir."""
        gorev_id = int(gorev.id or 0)
        permalink: str | None = None
        if medya_id:
            try:
                permalink = _metin_veya_none(self.yayinci.permalink_getir(medya_id))
            except Exception as hata:  # permalink zorunlu degil
                KAYIT.debug("Permalink alinamadi: %s", hata)
        if not permalink:
            # Yayin yanitinda baglanti gelmis olabilir (ek API cagrisi gerekmez).
            permalink = _metin_veya_none(
                getattr(self._yayinci, "son_permalink", None)
            )

        guncellemeler: dict[str, Any] = {"hata_mesaji": None, "deneme_sifirla": True}
        if medya_id:
            guncellemeler["medya_id"] = medya_id
        if permalink:
            guncellemeler["permalink"] = permalink
        db.durum_guncelle(self.db_yolu, gorev_id, Durum.YAYINLANDI, **guncellemeler)
        gorev.durum = Durum.YAYINLANDI
        gorev.yayin_zamani = simdi()
        if medya_id:
            gorev.medya_id = medya_id
        if permalink:
            gorev.permalink = permalink
        KAYIT.info(
            "YAYINLANDI: %s (%s) | medya_id=%s",
            gorev.dosya_adi,
            gorev.medya_tipi,
            medya_id or "bilinmiyor",
        )

        # Arsivleme basarisiz olsa bile yayin gerceklesmistir; gorev HATA olmaz.
        try:
            yeni_yol = self.arsivci.arsivle(gorev)
        except Exception as hata:
            KAYIT.error(
                "Dosya arsive tasinamadi (yayin basarili): %s | %s",
                gorev.dosya_adi,
                hata,
            )
            return
        if yeni_yol is not None:
            db.dosya_yolunu_guncelle(self.db_yolu, gorev_id, yeni_yol)
            gorev.dosya_yolu = Path(yeni_yol)
            KAYIT.info("Dosya arsive tasindi: %s", yeni_yol)

    # -- hata isleyicileri ---------------------------------------------------

    def _kalici_hata_isle(
        self, gorev: Gorev, mesaj: str, hata_nesnesi: BaseException | None = None
    ) -> None:
        """Kalici hata: dosya hata/ klasorune tasinir, gorev HATA olur."""
        gorev_id = int(gorev.id or 0)
        KAYIT.error("KALICI HATA | %s | %s", gorev.ozet(), mesaj)
        yeni_yol: Path | None = None
        if self.ayar.yayin.hatali_dosyayi_tasi:
            try:
                yeni_yol = self._hataya_tasi(gorev, mesaj, hata_nesnesi)
            except Exception as hata:
                KAYIT.error("Dosya hata klasorune tasinamadi: %s | %s", gorev.dosya_adi, hata)
        db.durum_guncelle(
            self.db_yolu, gorev_id, Durum.HATA, hata_mesaji=mesaj[:1000]
        )
        gorev.durum = Durum.HATA
        if yeni_yol is not None:
            db.dosya_yolunu_guncelle(self.db_yolu, gorev_id, yeni_yol)
            KAYIT.info("Dosya hata klasorune tasindi: %s", yeni_yol)

    def _hataya_tasi(
        self, gorev: Gorev, mesaj: str, hata_nesnesi: BaseException | None
    ) -> Path | None:
        """Arsivciyi cagirir; istisna nesnesini kabul etmeyen surumlerle de calisir."""
        try:
            return self.arsivci.hataya_tasi(gorev, mesaj, hata_nesnesi)
        except TypeError:
            # Eski/sade arsivci: sadece (gorev, sebep) kabul ediyor.
            return self.arsivci.hataya_tasi(gorev, mesaj)

    def _gecici_hata_isle(self, gorev: Gorev, hata: YayinHatasi) -> None:
        """Gecici hata: deneme sayisi artirilir, gorev kuyruga geri konur."""
        gorev_id = int(gorev.id or 0)
        mesaj = str(hata)
        deneme = int(gorev.deneme_sayisi) + 1
        azami = int(self.ayar.yayin.azami_deneme)

        if deneme >= azami:
            self._kalici_hata_isle(
                gorev,
                f"Azami deneme sayisina ulasildi ({deneme}/{azami}). Son hata: {mesaj}",
                hata,
            )
            return

        # Konteyner artik kullanilamaz durumdaysa sifirlanir; aksi halde ayni
        # konteynere kaldigi yerden devam edilir (rupload offset ile surer).
        alt_kod = getattr(hata, "alt_kod", None)
        konteyner_sifirla = bool(alt_kod) and int(alt_kod) in KONTEYNER_SIFIRLAMA_ALT_KODLARI

        gecikme_dk = min(2 ** deneme, AZAMI_GERI_CEKILME_DK)
        yeniden_deneme = simdi() + timedelta(minutes=gecikme_dk)

        guncellemeler: dict[str, Any] = {
            "hata_mesaji": mesaj[:1000],
            "deneme_artir": True,
            "planlanan_zaman": yeniden_deneme,
        }
        if konteyner_sifirla:
            guncellemeler["container_id"] = None
            guncellemeler["container_zamani"] = None

        db.durum_guncelle(self.db_yolu, gorev_id, Durum.BEKLIYOR, **guncellemeler)
        gorev.durum = Durum.BEKLIYOR
        KAYIT.warning(
            "GECICI HATA (%d/%d), %d dakika sonra tekrar denenecek%s | %s | %s",
            deneme,
            azami,
            gecikme_dk,
            " (yeni konteyner ile)" if konteyner_sifirla else "",
            gorev.dosya_adi,
            mesaj,
        )

    def _kota_hatasi_isle(self, gorev: Gorev, hata: KotaDoluHatasi) -> None:
        """Kota dolu: bu bir hata degil ERTELEMEdir; deneme sayisi artirilmaz."""
        gorev_id = int(gorev.id or 0)
        yeniden = hata.yeniden_deneme_zamani or (simdi() + timedelta(hours=1))
        self.kota_bekleme_zamani = yeniden
        db.durum_guncelle(
            self.db_yolu,
            gorev_id,
            Durum.BEKLIYOR,
            planlanan_zaman=yeniden,
            hata_mesaji=str(hata)[:1000],
        )
        gorev.durum = Durum.BEKLIYOR
        KAYIT.warning(
            "Yayin kotasi dolu, gorev ertelendi -> %s | %s",
            self._yerel_metin(yeniden),
            gorev.dosya_adi,
        )

    def _token_hatasi_isle(
        self, hata: TokenGecersizHatasi, gorev: Gorev | None = None
    ) -> None:
        """Token gecersiz: kuyruk durdurulur, gorev kaybolmadan beklemede kalir."""
        self._kuyrugu_durdur(str(hata))
        if gorev is not None and gorev.id is not None:
            db.durum_guncelle(
                self.db_yolu,
                int(gorev.id),
                Durum.BEKLIYOR,
                hata_mesaji=str(hata)[:1000],
            )
            gorev.durum = Durum.BEKLIYOR
        KAYIT.critical(
            _buyuk_uyari(
                "ERISIM ANAHTARI (TOKEN) GECERSIZ - KUYRUK DURDURULDU",
                [
                    "Meta yaniti: " + str(hata),
                    "",
                    "Yapilmasi gerekenler:",
                    "  1) Facebook ile yeniden giris yapip yeni bir kullanici",
                    "     anahtari alin, uzun omurlu surumune cevirin.",
                    "  2) /me/accounts ile Sayfa anahtarini (Page access token)",
                    "     yeniden alin.",
                    "  3) config/.env icindeki FB_SAYFA_TOKEN degerini guncelleyin.",
                    "  4) Programi yeniden baslatin.",
                    "",
                    "Kuyruktaki gorevler silinmedi, beklemede duruyor.",
                ],
            )
        )

    def _kopru_hatasi_isle(self, hata: KopruHatasi, gorev: Gorev | None = None) -> None:
        """Modul baglama hatasi: dosya TASINMAZ, kuyruk durdurulur."""
        self._kuyrugu_durdur(str(hata))
        if gorev is not None and gorev.id is not None:
            db.durum_guncelle(
                self.db_yolu,
                int(gorev.id),
                Durum.BEKLIYOR,
                hata_mesaji=str(hata)[:1000],
            )
            gorev.durum = Durum.BEKLIYOR
        KAYIT.critical(
            _buyuk_uyari(
                "MODUL BAGLAMA HATASI - KUYRUK DURDURULDU",
                [
                    str(hata),
                    "",
                    "Bu bir medya hatasi degildir; hicbir dosya tasinmadi.",
                    "Kod duzeltildikten sonra programi yeniden baslatin.",
                ],
            )
        )

    # -- haftalik token kontrolu --------------------------------------------

    def token_kontrol_isi(self) -> None:
        """Haftalik is: erisim anahtari hala gecerli mi diye bakar."""
        KAYIT.info("Erisim anahtari (token) kontrolu yapiliyor...")
        hazir, eksik = self.ayar.yayina_hazir_mi()
        if not hazir:
            KAYIT.warning(
                "Token kontrolu atlandi, config/.env eksik: %s", ", ".join(eksik)
            )
            return
        ayrinti = ""
        try:
            gecerli = bool(self.yayinci.token_gecerli_mi())
        except TokenGecersizHatasi as hata:
            gecerli = False
            ayrinti = str(hata)
        except KopruHatasi as hata:
            KAYIT.error("Token kontrolu yapilamadi (modul baglama): %s", hata)
            return
        except Exception as hata:
            KAYIT.warning(
                "Token kontrolu yapilamadi (gecici sorun olabilir, sonraki hafta "
                "tekrar denenecek): %s",
                hata,
            )
            return

        if gecerli:
            KAYIT.info("Erisim anahtari gecerli, yayin akisi saglikli.")
            return

        KAYIT.critical(
            _buyuk_uyari(
                "DIKKAT: ERISIM ANAHTARI (TOKEN) ARTIK GECERLI DEGIL",
                [
                    ayrinti or "debug_token kontrolu 'gecersiz' dondu.",
                    "",
                    "Otomasyon yeni gonderi YAYINLAYAMAZ.",
                    "",
                    "Sik gorulen sebepler:",
                    "  - Facebook sifresi degistirildi veya uygulama yetkisi kaldirildi",
                    "  - Kullanicinin Sayfa rolu degisti / kaldirildi",
                    "  - 90 gunluk veri erisim suresi doldu (yeniden yetkilendirme sart)",
                    "",
                    "Cozum: Facebook ile yeniden giris yapin, uzun omurlu Sayfa",
                    "anahtarini yeniden uretip config/.env dosyasina yazin.",
                ],
            )
        )

    # -- durum ozeti ---------------------------------------------------------

    def ozet(self) -> dict[str, Any]:
        """CLI/log icin kisa durum ozeti (sir icermez)."""
        return {
            "kuyruk": db.kuyruk_ozeti(self.db_yolu),
            "son_24_saat_yayin": db.son_24_saatte_yayinlanan_sayisi(self.db_yolu),
            "gunluk_limit": self.ayar.yayin.gunluk_limit,
            "kuyruk_durduruldu": self.kuyruk_durduruldu,
            "durdurma_sebebi": self.durdurma_sebebi,
            "kota_bekleme_zamani": self._yerel_metin(self.kota_bekleme_zamani),
            "yayin_slotlari": self.ayar.zamanlama.yayin_slotlari,
        }


# ===========================================================================
# 3) Tek basina calistirma
# ===========================================================================


def _kayit_kur(ayar: Ayarlar) -> None:
    """Sadece bu modul dogrudan calistirildiginda kullanilan basit log kurulumu."""
    from logging.handlers import RotatingFileHandler

    kok = logging.getLogger()
    if kok.handlers:  # main.py zaten kurmussa dokunma
        return
    kok.setLevel(getattr(logging, ayar.kayit.seviye, logging.INFO))
    bicim = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    konsol = logging.StreamHandler()
    konsol.setFormatter(bicim)
    kok.addHandler(konsol)
    ayar.kayit.dosya.parent.mkdir(parents=True, exist_ok=True)
    dosya = RotatingFileHandler(
        str(ayar.kayit.dosya),
        maxBytes=int(ayar.kayit.azami_bayt),
        backupCount=int(ayar.kayit.yedek_sayisi),
        encoding="utf-8",
    )
    dosya.setFormatter(bicim)
    kok.addHandler(dosya)


def calistir(ayar: Ayarlar | None = None) -> None:
    """Zamanlayiciyi engelleyici kipte baslatir (Ctrl+C ile durur)."""
    yapilandirma = ayar if ayar is not None else ayarlari_yukle()
    _kayit_kur(yapilandirma)
    zamanlayici = Zamanlayici(yapilandirma)
    try:
        zamanlayici.basla(engelleyici=True)
    except (KeyboardInterrupt, SystemExit):
        KAYIT.info("Kapatma istegi alindi, zamanlayici durduruluyor...")
        zamanlayici.durdur()


__all__ = [
    "AZAMI_GERI_CEKILME_DK",
    "Arsivci",
    "DURUM_HATALI",
    "DURUM_HAZIR",
    "DURUM_ISLENIYOR",
    "DURUM_SURESI_DOLDU",
    "DURUM_YAYINDA",
    "IS_ILK_TOKEN",
    "IS_KUYRUK",
    "IS_TOKEN",
    "KopruHatasi",
    "KotaDurumu",
    "ModulArsivci",
    "ModulYayinci",
    "VARSAYILAN_TUR_BASINA_GOREV",
    "Yayinci",
    "Zamanlayici",
    "calistir",
]


if __name__ == "__main__":  # pragma: no cover
    calistir()
