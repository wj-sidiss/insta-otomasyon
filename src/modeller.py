# -*- coding: ascii -*-
"""Ortak veri modelleri, durum makinesi, medya sinirlari ve ozel istisnalar.

Bu modul SADECE standart kutuphaneyi kullanir; hicbir dis pakete bagli degildir.
Projedeki butun moduller (db, ayarlar, instagram, medya, izleyici, zamanlayici,
arsiv) buradaki tipleri paylasir.

TARIH / SAAT KURALI (butun proje icin baglayicidir):
  - Bellekte: her zaman timezone-aware datetime nesnesi. Naive datetime YASAK.
  - Diskte (SQLite): UTC'ye cevrilmis, mikrosaniyesiz ISO 8601 metin.
    Ornek: "2026-09-25T15:30:00+00:00"
    Bu bicim sabit uzunluktadir; bu sayede SQLite'ta metin karsilastirmasi ile
    kronolojik karsilastirma ayni sonucu verir (ORDER BY / <= calisir).
  - Kullaniciya gosterirken Europe/Istanbul'a cevrilir (bkz. ayarlar.zaman_dilimi).

Turkce karakter kullanilmaz (ASCII), konsol encoding sorunu cikmasin diye.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Mapping

# ---------------------------------------------------------------------------
# Durum makinesi
# ---------------------------------------------------------------------------


class Durum(StrEnum):
    """Bir gorevin kuyruktaki yasam dongusu.

    Akis: BEKLIYOR -> YUKLENIYOR -> HAZIRLANIYOR -> YAYINLANDI
    Herhangi bir adimda hata olursa -> HATA (gecici hatalarda BEKLIYOR'a doner).
    """

    BEKLIYOR = "bekliyor"            # kuyruga alindi, sirasini bekliyor
    YUKLENIYOR = "yukleniyor"        # kapsayici acildi, dosya rupload'a gidiyor
    HAZIRLANIYOR = "hazirlaniyor"    # yukleme bitti, Meta videoyu isliyor (status_code beklemesi)
    YAYINLANDI = "yayinlandi"        # media_publish basarili, arsive tasindi
    HATA = "hata"                    # kalici hata; dosya hata/ klasorune tasindi


class MedyaTipi(StrEnum):
    """Meta API'sine gonderilen media_type degeri.

    DEGERLER BIREBIR API'YE GIDER, degistirme.
    """

    REELS = "REELS"
    STORIES = "STORIES"


#: Islenmesi devam eden (kuyrukta canli) durumlar
AKTIF_DURUMLAR: Final[frozenset[Durum]] = frozenset(
    {Durum.BEKLIYOR, Durum.YUKLENIYOR, Durum.HAZIRLANIYOR}
)
#: Artik dokunulmayacak durumlar
SONLANMIS_DURUMLAR: Final[frozenset[Durum]] = frozenset({Durum.YAYINLANDI, Durum.HATA})


# ---------------------------------------------------------------------------
# Zaman yardimcilari - butun proje bunlari kullanmali
# ---------------------------------------------------------------------------


def simdi() -> datetime:
    """Su anki zaman, UTC ve timezone-aware, mikrosaniyesiz."""
    return datetime.now(UTC).replace(microsecond=0)


def zaman_metni(an: datetime) -> str:
    """datetime -> veritabaninda saklanacak ISO 8601 metin (UTC, mikrosaniyesiz).

    Naive (timezone bilgisi olmayan) datetime verilirse hata firlatir; bu
    bilincli bir karardir, sessiz yanlis saat yayini yapmaktan iyidir.
    """
    if an.tzinfo is None:
        raise ValueError(
            "Zaman dilimi bilgisi olmayan (naive) tarih kabul edilmiyor: " + repr(an)
        )
    return an.astimezone(UTC).replace(microsecond=0).isoformat()


def metinden_zaman(metin: str | None) -> datetime | None:
    """Veritabanindaki ISO 8601 metni timezone-aware datetime'a cevirir.

    Bos/None deger None doner. Eski kayitlarda zaman dilimi eki yoksa UTC kabul
    edilir (savunmaci davranis).
    """
    if not metin:
        return None
    an = datetime.fromisoformat(metin)
    if an.tzinfo is None:
        an = an.replace(tzinfo=UTC)
    return an


# ---------------------------------------------------------------------------
# Medya sinirlari - Meta dokumanindan dogrulanmis degerler
# Yukleme oncesi yerel dogrulama icin kullanilir. Sinirlari asan dosya yukleme
# sirasinda degil ISLEME sirasinda status_code=ERROR olarak geri doner; bu
# yuzden kuyruga almadan once burada elenmelidir.
# ---------------------------------------------------------------------------

REELS_SINIRLAR: Final[dict[str, Any]] = {
    "uzantilar": (".mp4", ".mov"),
    "min_sure_sn": 3,
    "maks_sure_sn": 900,                       # 15 dakika
    "maks_boyut_bayt": 300 * 1024 * 1024,      # 300 MB
    "maks_genislik_px": 1920,
    "min_fps": 23,
    "maks_fps": 60,
    "min_oran": 0.01,                          # dokuman: 0.01:1
    "maks_oran": 10.0,
    "onerilen_oran": 9 / 16,
}

STORY_SINIRLAR: Final[dict[str, Any]] = {
    "uzantilar": (".mp4", ".mov"),
    "min_sure_sn": 3,
    "maks_sure_sn": 60,                        # DIKKAT: Reels'ten farkli
    "maks_boyut_bayt": 100 * 1024 * 1024,      # DIKKAT: Reels'ten farkli
    "maks_genislik_px": 1920,
    "min_fps": 23,
    "maks_fps": 60,
    "min_oran": 0.1,                           # dokuman: 0.1:1 (Reels'te 0.01:1)
    "maks_oran": 10.0,
    "onerilen_oran": 9 / 16,
}

#: Gorsel Story. DIKKAT: rupload/resumable akisi gorsel KABUL ETMEZ; gorsel
#: yayini public image_url ister. Bu proje gorseli ffmpeg ile kisa videoya
#: cevirerek STORIES videosu olarak yayinlar (bulut depolama yok karari).
STORY_GORSEL_SINIRLAR: Final[dict[str, Any]] = {
    "uzantilar": (".jpg", ".jpeg"),            # MPO / JPS desteklenmez
    "maks_boyut_bayt": 8 * 1024 * 1024,        # 8 MB
    "renk_uzayi": "sRGB",
    "cevrilebilir_uzantilar": (".png", ".webp", ".bmp"),  # Pillow ile JPEG'e cevrilir
}

CAPTION_MAKS_KARAKTER: Final[int] = 2200
CAPTION_MAKS_HASHTAG: Final[int] = 30
CAPTION_MAKS_ETIKET: Final[int] = 20

#: Kapsayici (container) Meta tarafinda 24 saat sonra EXPIRED olur.
KAPSAYICI_OMRU_SAAT: Final[int] = 24

#: Yayin kotasi penceresi (kayan pencere, gece yarisi sifirlanmaz).
KOTA_PENCERESI_SAAT: Final[int] = 24


def sinirlari_getir(medya_tipi: MedyaTipi) -> dict[str, Any]:
    """Medya tipine gore video sinirlarini dondurur."""
    return REELS_SINIRLAR if medya_tipi is MedyaTipi.REELS else STORY_SINIRLAR


# ---------------------------------------------------------------------------
# Meta hata kodlari - kalici / gecici ayrimi
# Kor retry yapmak 400'luk kapsayici kotasini ve gunluk yayin kotasini yakar.
# ---------------------------------------------------------------------------

#: Tekrar denemenin ANLAMSIZ oldugu alt kodlar (dosya hata/ klasorune tasinir)
KALICI_ALT_KODLAR: Final[frozenset[int]] = frozenset(
    {
        2207026,  # video formati desteklenmiyor (MOV/MP4, moov atom basta olmali)
        2207005,  # gorsel formati desteklenmiyor
        2207009,  # en-boy orani gecersiz
        2207010,  # caption 2200 karakteri asti
        2207040,  # 20'den fazla @ etiketi
        2207057,  # thumb_offset video suresi disinda
        2207004,  # gorsel 8 MiB'den buyuk
        2207023,  # bilinmeyen media_type
        2207050,  # Instagram hesabi kisitli
        2207051,  # spam korumasi - retry kisiti derinlestirir
    }
)

#: Yeni kapsayici ile tekrar denenebilir alt kodlar
GECICI_ALT_KODLAR: Final[frozenset[int]] = frozenset(
    {
        2207003,  # medya indirme cok uzun surdu
        2207001,  # Instagram sunucu hatasi
        2207032,  # "Create media fail, please try to re-create media"
        2207053,  # bilinmeyen yukleme hatasi
        2207008,  # media builder yok / suresi doldu
        2207020,  # medya suresi doldu -> yeni kapsayici
        2207027,  # medya yayina hazir degil (once status_code FINISHED bekle)
        2207052,  # medya URI'den cekilemedi
    }
)

#: Token kalici olarak gecersiz - kullanicinin yeniden giris yapmasi gerekir
TOKEN_HATA_KODLARI: Final[frozenset[int]] = frozenset({190, 102, 467})
TOKEN_HATA_ALT_KODLARI: Final[frozenset[int]] = frozenset(
    {458, 459, 460, 463, 464, 467, 492}
)

#: Izin eksikligi - insan mudahalesi gerekir, retry cozmez
IZIN_HATA_KODLARI: Final[frozenset[int]] = frozenset({10, 200, 299, 9053})

#: Cagri limiti (uygulama / kullanici / sayfa) - uzun backoff ile tekrar dene
CAGRI_LIMIT_KODLARI: Final[frozenset[int]] = frozenset({4, 17, 32, 341, 368, 613})

#: Gecici sunucu hatalari
GECICI_KODLAR: Final[frozenset[int]] = frozenset({1, 2})

#: 24 saatlik yayin kotasi doldu -> hata degil ERTELEME
KOTA_KODU: Final[int] = 9
KOTA_ALT_KODU: Final[int] = 2207042


# ---------------------------------------------------------------------------
# Istisnalar
# ---------------------------------------------------------------------------


class YayinHatasi(RuntimeError):
    """Yayin akisindaki hatalarin taban sinifi.

    kalici == True  -> tekrar denenmez, gorev HATA durumuna gecer
    kalici == False -> kuyrukta azami_deneme kadar tekrar denenir
    """

    kalici: bool = False

    def __init__(
        self,
        mesaj: str,
        *,
        kod: int | None = None,
        alt_kod: int | None = None,
        iz_no: str | None = None,
    ) -> None:
        super().__init__(mesaj)
        self.mesaj = mesaj
        self.kod = kod
        self.alt_kod = alt_kod
        self.iz_no = iz_no  # fbtrace_id - Meta destek kaydi icin sart, loga yaz

    def __str__(self) -> str:  # log satirlarinda tek satirlik ozet
        parcalar = [self.mesaj]
        if self.kod is not None:
            parcalar.append(f"kod={self.kod}")
        if self.alt_kod:
            parcalar.append(f"alt_kod={self.alt_kod}")
        if self.iz_no:
            parcalar.append(f"iz={self.iz_no}")
        return " | ".join(parcalar)


class KaliciHata(YayinHatasi):
    """Tekrar denenmesi anlamsiz hata (format, oran, izin, hesap kisiti...)."""

    kalici = True


class GeciciHata(YayinHatasi):
    """Tekrar denenebilir hata (ag, sunucu, isleme gecikmesi...)."""

    kalici = False


class TokenGecersizHatasi(KaliciHata):
    """Erisim anahtari gecersiz/suresi dolmus. Kuyruk durdurulmali."""


class KotaDoluHatasi(GeciciHata):
    """24 saatlik yayin kotasi doldu. Gorev hata degil ERTELEME alir."""

    def __init__(self, mesaj: str, *, yeniden_deneme_zamani: datetime | None = None,
                 kod: int | None = None, alt_kod: int | None = None,
                 iz_no: str | None = None) -> None:
        super().__init__(mesaj, kod=kod, alt_kod=alt_kod, iz_no=iz_no)
        self.yeniden_deneme_zamani = yeniden_deneme_zamani


class MedyaDogrulamaHatasi(KaliciHata):
    """Dosya Meta spesifikasyonuna uymuyor (boyut, sure, format, oran)."""


def hata_sinifla(
    kod: int | None,
    alt_kod: int | None,
    mesaj: str,
    *,
    iz_no: str | None = None,
) -> YayinHatasi:
    """Meta hata govdesindeki kod/alt_kod ciftini dogru istisnaya cevirir.

    instagram.py bu fonksiyonu kullanmali; boylece retry mantigi tek yerde
    tanimli kalir. Bilinmeyen hatalar GECICI kabul edilir, ancak gorevin
    deneme_sayisi siniri sonsuz dongune izin vermez.
    """
    kod_sayi = int(kod) if kod is not None else None
    alt_sayi = int(alt_kod) if alt_kod else None

    # Sayfa Yayin Yetkilendirmesi (PPA) icin Meta ozel bir kod yayinlamiyor;
    # tespit mesaj metninden yapilir.
    if "page publishing authorization" in mesaj.lower():
        return KaliciHata(
            "Sayfa Yayin Yetkilendirmesi (PPA) tamamlanmamis. Facebook Sayfa "
            "ayarlarindan yayin yetkilendirmesini tamamlayin: " + mesaj,
            kod=kod_sayi,
            alt_kod=alt_sayi,
            iz_no=iz_no,
        )

    if kod_sayi == KOTA_KODU and alt_sayi == KOTA_ALT_KODU:
        return KotaDoluHatasi(
            "Gunluk yayin kotasi doldu, gorev ertelendi: " + mesaj,
            kod=kod_sayi,
            alt_kod=alt_sayi,
            iz_no=iz_no,
        )

    if (kod_sayi in TOKEN_HATA_KODLARI) or (alt_sayi in TOKEN_HATA_ALT_KODLARI):
        return TokenGecersizHatasi(
            "Erisim anahtari gecersiz veya suresi dolmus. Facebook ile yeniden "
            "giris yapip config/.env dosyasini guncelleyin: " + mesaj,
            kod=kod_sayi,
            alt_kod=alt_sayi,
            iz_no=iz_no,
        )

    if alt_sayi in KALICI_ALT_KODLAR:
        return KaliciHata(mesaj, kod=kod_sayi, alt_kod=alt_sayi, iz_no=iz_no)

    if kod_sayi in IZIN_HATA_KODLARI:
        return KaliciHata(
            "Yayin yetkisi problemi. Gerekli izinler: instagram_basic, "
            "instagram_content_publish, pages_read_engagement (Sayfa rolu "
            "Business Manager uzerinden verildiyse ads_management/ads_read de "
            "gerekir): " + mesaj,
            kod=kod_sayi,
            alt_kod=alt_sayi,
            iz_no=iz_no,
        )

    if alt_sayi in GECICI_ALT_KODLAR or kod_sayi in GECICI_KODLAR:
        return GeciciHata(mesaj, kod=kod_sayi, alt_kod=alt_sayi, iz_no=iz_no)

    if kod_sayi in CAGRI_LIMIT_KODLARI:
        return GeciciHata(
            "Meta cagri limitine takildi, bir sure beklenecek: " + mesaj,
            kod=kod_sayi,
            alt_kod=alt_sayi,
            iz_no=iz_no,
        )

    # Bilinmeyen hata: gecici kabul et, deneme sayisi sinirlar.
    return GeciciHata(mesaj, kod=kod_sayi, alt_kod=alt_sayi, iz_no=iz_no)


# ---------------------------------------------------------------------------
# Gorev: kuyruktaki tek bir yayin isi
# ---------------------------------------------------------------------------


@dataclass
class Gorev:
    """Kuyruktaki tek bir yayin isi (gorevler tablosunun bir satiri).

    Zorunlu alanlar: dosya_yolu, medya_tipi. Digerleri anahtar kelime ile
    verilir. Veritabanindan okundugunda 'id' ve zaman alanlari doludur.
    """

    dosya_yolu: Path
    medya_tipi: MedyaTipi
    id: int | None = None
    aciklama: str | None = None              # caption; STORIES icin kullanilmaz
    planlanan_zaman: datetime | None = None  # None ise siradaki uygun slot
    durum: Durum = Durum.BEKLIYOR
    container_id: str | None = None          # Meta kapsayici (creation_id)
    container_zamani: datetime | None = None # kapsayici acilis ani (24s omur takibi)
    permalink: str | None = None
    medya_id: str | None = None              # media_publish sonucu donen IG medya ID
    deneme_sayisi: int = 0
    hata_mesaji: str | None = None
    olusturma_zamani: datetime | None = None
    guncelleme_zamani: datetime | None = None
    yayin_zamani: datetime | None = None     # 24 saatlik kota sayimi buradan yapilir
    ekstra: dict[str, Any] = field(default_factory=dict)  # gecici/ic kullanim

    # -- yardimci sorgular ---------------------------------------------------

    @property
    def dosya_adi(self) -> str:
        return self.dosya_yolu.name

    @property
    def video_mu(self) -> bool:
        return self.dosya_yolu.suffix.lower() in (".mp4", ".mov")

    def zamani_geldi_mi(self, an: datetime | None = None) -> bool:
        """Planlanan zamani gecti mi? Planlanmamis gorevler her zaman hazirdir."""
        if self.planlanan_zaman is None:
            return True
        return (an or simdi()) >= self.planlanan_zaman

    def kapsayici_suresi_doldu_mu(self, an: datetime | None = None) -> bool:
        """Kapsayici 24 saati doldurduysa True (yeniden olusturulmali)."""
        if self.container_id is None or self.container_zamani is None:
            return False
        sinir = self.container_zamani + timedelta(hours=KAPSAYICI_OMRU_SAAT)
        return (an or simdi()) >= sinir

    def sinirlar(self) -> dict[str, Any]:
        return sinirlari_getir(self.medya_tipi)

    def ozet(self) -> str:
        """Log satirlari icin tek satirlik, token icermeyen ozet."""
        plan = (
            self.planlanan_zaman.isoformat() if self.planlanan_zaman else "ilk uygun slot"
        )
        return (
            f"[#{self.id if self.id is not None else '-'}] {self.dosya_adi} "
            f"({self.medya_tipi}) durum={self.durum} plan={plan} "
            f"deneme={self.deneme_sayisi}"
        )

    # -- donusumler ----------------------------------------------------------

    @classmethod
    def eslemeden_olustur(cls, eslem: Mapping[str, Any]) -> Gorev:
        """sqlite3.Row / dict -> Gorev. db.py bu fonksiyonu kullanir."""
        veri = dict(eslem)
        return cls(
            dosya_yolu=Path(str(veri["dosya_yolu"])),
            medya_tipi=MedyaTipi(str(veri["medya_tipi"])),
            id=int(veri["id"]) if veri.get("id") is not None else None,
            aciklama=veri.get("aciklama"),
            planlanan_zaman=metinden_zaman(veri.get("planlanan_zaman")),
            durum=Durum(str(veri.get("durum", Durum.BEKLIYOR))),
            container_id=veri.get("container_id"),
            container_zamani=metinden_zaman(veri.get("container_zamani")),
            permalink=veri.get("permalink"),
            medya_id=veri.get("medya_id"),
            deneme_sayisi=int(veri.get("deneme_sayisi") or 0),
            hata_mesaji=veri.get("hata_mesaji"),
            olusturma_zamani=metinden_zaman(veri.get("olusturma_zamani")),
            guncelleme_zamani=metinden_zaman(veri.get("guncelleme_zamani")),
            yayin_zamani=metinden_zaman(veri.get("yayin_zamani")),
        )

    def sozluge_cevir(self) -> dict[str, Any]:
        """JSON/log dostu sozluk (zamanlar ISO 8601 metin)."""
        return {
            "id": self.id,
            "dosya_yolu": str(self.dosya_yolu),
            "medya_tipi": str(self.medya_tipi),
            "aciklama": self.aciklama,
            "planlanan_zaman": (
                zaman_metni(self.planlanan_zaman) if self.planlanan_zaman else None
            ),
            "durum": str(self.durum),
            "container_id": self.container_id,
            "container_zamani": (
                zaman_metni(self.container_zamani) if self.container_zamani else None
            ),
            "permalink": self.permalink,
            "medya_id": self.medya_id,
            "deneme_sayisi": self.deneme_sayisi,
            "hata_mesaji": self.hata_mesaji,
            "olusturma_zamani": (
                zaman_metni(self.olusturma_zamani) if self.olusturma_zamani else None
            ),
            "guncelleme_zamani": (
                zaman_metni(self.guncelleme_zamani) if self.guncelleme_zamani else None
            ),
            "yayin_zamani": (
                zaman_metni(self.yayin_zamani) if self.yayin_zamani else None
            ),
        }


__all__ = [
    "AKTIF_DURUMLAR",
    "CAGRI_LIMIT_KODLARI",
    "CAPTION_MAKS_ETIKET",
    "CAPTION_MAKS_HASHTAG",
    "CAPTION_MAKS_KARAKTER",
    "Durum",
    "GECICI_ALT_KODLAR",
    "GECICI_KODLAR",
    "GeciciHata",
    "Gorev",
    "IZIN_HATA_KODLARI",
    "KALICI_ALT_KODLAR",
    "KAPSAYICI_OMRU_SAAT",
    "KOTA_ALT_KODU",
    "KOTA_KODU",
    "KOTA_PENCERESI_SAAT",
    "KaliciHata",
    "KotaDoluHatasi",
    "MedyaDogrulamaHatasi",
    "MedyaTipi",
    "REELS_SINIRLAR",
    "SONLANMIS_DURUMLAR",
    "STORY_GORSEL_SINIRLAR",
    "STORY_SINIRLAR",
    "TOKEN_HATA_ALT_KODLARI",
    "TOKEN_HATA_KODLARI",
    "TokenGecersizHatasi",
    "YayinHatasi",
    "hata_sinifla",
    "metinden_zaman",
    "simdi",
    "sinirlari_getir",
    "zaman_metni",
]
