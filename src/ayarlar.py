# -*- coding: ascii -*-
"""Ayar katmani: config/.env (sirlar) + config/ayarlar.yaml (davranis).

Iki kaynak vardir:
  1) config/.env        -> token ve kimlikler (pydantic-settings ile okunur)
                           ASLA depoya girmez, ASLA loga yazilmaz.
  2) config/ayarlar.yaml -> yayin slotlari, limitler, klasorler, zaman asimlari

Kullanim:
    from src.ayarlar import ayarlari_yukle
    ayar = ayarlari_yukle()
    ayar.klasorleri_hazirla()
    print(ayar.grafik_kok)          # https://graph.facebook.com/v23.0
    print(ayar.db_yolu)             # ...\\insta-otomasyon\\kuyruk.db

Token gelmeden once de calisir: zorunlu alanlar bos olabilir, sistem sahte
(mock) API ile test edilebilir. Gercek yayin oncesi 'yayina_hazir_mi()'
cagrilmalidir.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GRAFIK_HOST: Final[str] = "https://graph.facebook.com"
YUKLEME_HOST: Final[str] = "https://rupload.facebook.com"

_SAAT_BICIMI: Final[re.Pattern[str]] = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SURUM_BICIMI: Final[re.Pattern[str]] = re.compile(r"^v\d+\.\d+$")


class AyarHatasi(RuntimeError):
    """Ayar dosyalarinda eksik/hatali deger."""


def token_maskele(deger: str | None, gorunur: int = 4) -> str:
    """Loglarda token gostermek icin: sadece son birkac karakteri birakir."""
    if not deger:
        return "<bos>"
    if len(deger) <= gorunur:
        return "*" * len(deger)
    return "*" * 8 + deger[-gorunur:]


# ---------------------------------------------------------------------------
# 1) config/.env  --  sirlar
# ---------------------------------------------------------------------------


class GizliAyarlar(BaseSettings):
    """config/.env icerigi. Alan adlari .env anahtarlariyla eslesir."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    fb_sayfa_token: SecretStr = SecretStr("")   # FB_SAYFA_TOKEN  (Page access token)
    ig_hesap_id: str = ""                       # IG_HESAP_ID     (Instagram Business ID)
    fb_uygulama_id: str = ""                    # FB_UYGULAMA_ID  (debug_token icin)
    fb_uygulama_sirri: SecretStr = SecretStr("")  # FB_UYGULAMA_SIRRI (debug_token icin)
    api_surumu: str = "v23.0"                   # API_SURUMU

    @field_validator("api_surumu")
    @classmethod
    def _surumu_dogrula(cls, deger: str) -> str:
        deger = deger.strip()
        if not _SURUM_BICIMI.match(deger):
            raise ValueError(
                "API_SURUMU 'v23.0' biciminde olmali, verilen: " + repr(deger)
            )
        return deger

    @field_validator("ig_hesap_id", "fb_uygulama_id")
    @classmethod
    def _kimligi_temizle(cls, deger: str) -> str:
        return deger.strip()

    # -- yardimcilar ---------------------------------------------------------

    @property
    def token(self) -> str:
        """Acik metin Page access token. ASLA loglanmaz."""
        return self.fb_sayfa_token.get_secret_value().strip()

    @property
    def uygulama_sirri(self) -> str:
        return self.fb_uygulama_sirri.get_secret_value().strip()

    @property
    def uygulama_tokeni(self) -> str:
        """debug_token cagrisinda kullanilan '{app-id}|{app-secret}' bicimi."""
        return f"{self.fb_uygulama_id}|{self.uygulama_sirri}"

    def eksik_alanlar(self) -> list[str]:
        """Yayin yapabilmek icin doldurulmasi gereken bos alanlarin listesi."""
        eksik: list[str] = []
        if not self.token:
            eksik.append("FB_SAYFA_TOKEN")
        if not self.ig_hesap_id:
            eksik.append("IG_HESAP_ID")
        return eksik

    def token_dogrulanabilir_mi(self) -> bool:
        """debug_token cagrisi icin uygulama kimligi ve sirri var mi?"""
        return bool(self.fb_uygulama_id and self.uygulama_sirri)

    def gizlenmis_ozet(self) -> dict[str, str]:
        """Log'a basilabilir, sir icermeyen ozet."""
        return {
            "FB_SAYFA_TOKEN": token_maskele(self.token),
            "IG_HESAP_ID": self.ig_hesap_id or "<bos>",
            "FB_UYGULAMA_ID": self.fb_uygulama_id or "<bos>",
            "FB_UYGULAMA_SIRRI": token_maskele(self.uygulama_sirri),
            "API_SURUMU": self.api_surumu,
        }


# ---------------------------------------------------------------------------
# 2) config/ayarlar.yaml  --  davranis
# ---------------------------------------------------------------------------


class _Taban(BaseModel):
    # Yazim hatasi sessizce varsayilana dusmesin diye bilinmeyen anahtar yasak.
    model_config = ConfigDict(extra="forbid")


class KlasorAyarlari(_Taban):
    """Izlenen ve kullanilan klasorler. Goreli yollar proje kokune goredir."""

    gelen_reels: Path = Path("gelen/reels")
    gelen_story: Path = Path("gelen/story")
    yayinlandi: Path = Path("yayinlandi")
    hata: Path = Path("hata")
    veritabani: Path = Path("kuyruk.db")


class ZamanlamaAyarlari(_Taban):
    """Yayin slotlari ve tarama sikligi."""

    zaman_dilimi: str = "Europe/Istanbul"
    yayin_slotlari: list[str] = Field(default_factory=lambda: ["12:00", "19:00"])
    tarama_araligi_sn: int = Field(default=60, ge=10, le=3600)
    slot_tolerans_dk: int = Field(default=30, ge=0, le=720)

    @field_validator("zaman_dilimi")
    @classmethod
    def _dilimi_dogrula(cls, deger: str) -> str:
        try:
            ZoneInfo(deger)
        except (ZoneInfoNotFoundError, ValueError) as hata:
            raise ValueError(
                f"Zaman dilimi taninmadi: {deger!r}. Windows'ta 'tzdata' paketi "
                f"kurulu olmalidir. Ayrinti: {hata}"
            ) from hata
        return deger

    @field_validator("yayin_slotlari")
    @classmethod
    def _slotlari_dogrula(cls, deger: list[str]) -> list[str]:
        if not deger:
            raise ValueError("En az bir yayin slotu tanimlanmali (ornek: '12:00').")
        temiz: list[str] = []
        for slot in deger:
            metin = str(slot).strip()
            if not _SAAT_BICIMI.match(metin):
                raise ValueError(
                    f"Yayin slotu 'SS:DD' biciminde olmali (00:00-23:59), "
                    f"verilen: {slot!r}"
                )
            temiz.append(metin)
        return sorted(set(temiz))


class YayinAyarlari(_Taban):
    """Yayin akisi davranisi: limitler, bekleme sureleri, deneme sayisi."""

    gunluk_limit: int = Field(default=25, ge=1, le=100)
    azami_deneme: int = Field(default=5, ge=1, le=20)
    durum_sorgu_araligi_sn: int = Field(default=20, ge=5, le=300)
    durum_azami_bekleme_sn: int = Field(default=900, ge=60, le=7200)
    yukleme_zaman_asimi_sn: int = Field(default=900, ge=60, le=7200)
    istek_zaman_asimi_sn: int = Field(default=60, ge=10, le=600)
    baglanti_zaman_asimi_sn: int = Field(default=30, ge=5, le=300)
    akisa_paylas: bool = True                 # share_to_feed - SADECE REELS
    varsayilan_aciklama: str = ""             # .txt yoksa kullanilir
    kota_sorgula: bool = True                 # yayin oncesi content_publishing_limit
    hatali_dosyayi_tasi: bool = True          # kalici hatada hata/ klasorune tasi


class DosyaAyarlari(_Taban):
    """Klasor izleme ve dosya kabul kurallari."""

    kararlilik_bekleme_sn: float = Field(default=3.0, ge=0.5, le=60.0)
    kararlilik_tekrar: int = Field(default=3, ge=1, le=20)
    video_uzantilari: list[str] = Field(default_factory=lambda: [".mp4", ".mov"])
    gorsel_uzantilari: list[str] = Field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".webp"]
    )
    aciklama_uzantisi: str = ".txt"
    yoksayilan_onekler: list[str] = Field(default_factory=lambda: [".", "~", "_"])

    @field_validator("video_uzantilari", "gorsel_uzantilari")
    @classmethod
    def _uzantilari_duzelt(cls, deger: list[str]) -> list[str]:
        temiz: list[str] = []
        for uzanti in deger:
            metin = str(uzanti).strip().lower()
            if not metin:
                continue
            if not metin.startswith("."):
                metin = "." + metin
            temiz.append(metin)
        if not temiz:
            raise ValueError("Uzanti listesi bos olamaz.")
        return temiz


class KayitAyarlari(_Taban):
    """Log dosyasi ayarlari (kullaniciya gorunen mesajlar Turkce)."""

    seviye: str = "INFO"
    dosya: Path = Path("hata/otomasyon.log")
    azami_bayt: int = Field(default=5 * 1024 * 1024, ge=64 * 1024)
    yedek_sayisi: int = Field(default=5, ge=0, le=50)

    @field_validator("seviye")
    @classmethod
    def _seviyeyi_dogrula(cls, deger: str) -> str:
        metin = str(deger).strip().upper()
        gecerli = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if metin not in gecerli:
            raise ValueError(
                "Kayit seviyesi su degerlerden biri olmali: " + ", ".join(sorted(gecerli))
            )
        return metin


class YamlAyarlari(_Taban):
    """config/ayarlar.yaml dosyasinin tamami."""

    klasorler: KlasorAyarlari = Field(default_factory=KlasorAyarlari)
    zamanlama: ZamanlamaAyarlari = Field(default_factory=ZamanlamaAyarlari)
    yayin: YayinAyarlari = Field(default_factory=YayinAyarlari)
    dosya: DosyaAyarlari = Field(default_factory=DosyaAyarlari)
    kayit: KayitAyarlari = Field(default_factory=KayitAyarlari)


# ---------------------------------------------------------------------------
# 3) Birlesik ayar nesnesi
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ayarlar:
    """Uygulamanin tek ayar nesnesi. ayarlari_yukle() ile uretilir."""

    proje_koku: Path
    gizli: GizliAyarlar
    klasorler: KlasorAyarlari
    zamanlama: ZamanlamaAyarlari
    yayin: YayinAyarlari
    dosya: DosyaAyarlari
    kayit: KayitAyarlari

    # -- kisayollar ----------------------------------------------------------

    @property
    def api_surumu(self) -> str:
        return self.gizli.api_surumu

    @property
    def token(self) -> str:
        """Page access token (acik metin). Loga yazma, token_maskele() kullan."""
        return self.gizli.token

    @property
    def ig_hesap_id(self) -> str:
        return self.gizli.ig_hesap_id

    @property
    def grafik_kok(self) -> str:
        """Ornek: https://graph.facebook.com/v23.0"""
        return f"{GRAFIK_HOST}/{self.api_surumu}"

    @property
    def rupload_kok(self) -> str:
        """Ornek: https://rupload.facebook.com/ig-api-upload/v23.0

        NOT: kapsayici yanitindaki 'uri' alani varsa ONCE o kullanilmali;
        bu adres yalnizca yedek (fallback) olarak kurulur.
        """
        return f"{YUKLEME_HOST}/ig-api-upload/{self.api_surumu}"

    @cached_property
    def zaman_dilimi(self) -> ZoneInfo:
        return ZoneInfo(self.zamanlama.zaman_dilimi)

    @property
    def db_yolu(self) -> Path:
        return self.klasorler.veritabani

    @property
    def izlenen_klasorler(self) -> dict[str, Path]:
        """Medya tipi -> izlenecek klasor. izleyici.py bunu kullanir."""
        return {
            "REELS": self.klasorler.gelen_reels,
            "STORIES": self.klasorler.gelen_story,
        }

    @property
    def kabul_edilen_uzantilar(self) -> tuple[str, ...]:
        return tuple(self.dosya.video_uzantilari) + tuple(self.dosya.gorsel_uzantilari)

    # -- yardimcilar ---------------------------------------------------------

    def yayina_hazir_mi(self) -> tuple[bool, list[str]]:
        """(hazir_mi, eksik_alanlar). Gercek yayin oncesi cagrilmalidir."""
        eksik = self.gizli.eksik_alanlar()
        return (not eksik), eksik

    def yayina_hazir_degilse_hata(self) -> None:
        """Eksik ayar varsa Turkce, anlasilir bir hata firlatir."""
        hazir, eksik = self.yayina_hazir_mi()
        if not hazir:
            raise AyarHatasi(
                "config/.env dosyasinda su alanlar bos: "
                + ", ".join(eksik)
                + ". Ornek dosya: config/.env.ornek"
            )

    def klasorleri_hazirla(self) -> None:
        """Gerekli klasorleri olusturur (varsa dokunmaz)."""
        for klasor in (
            self.klasorler.gelen_reels,
            self.klasorler.gelen_story,
            self.klasorler.yayinlandi,
            self.klasorler.hata,
        ):
            klasor.mkdir(parents=True, exist_ok=True)
        self.klasorler.veritabani.parent.mkdir(parents=True, exist_ok=True)
        self.kayit.dosya.parent.mkdir(parents=True, exist_ok=True)

    def slot_saatleri(self) -> list[tuple[int, int]]:
        """['12:00', '19:00'] -> [(12, 0), (19, 0)]"""
        saatler: list[tuple[int, int]] = []
        for slot in self.zamanlama.yayin_slotlari:
            saat, dakika = slot.split(":")
            saatler.append((int(saat), int(dakika)))
        return saatler

    def sonraki_slot(self, an: datetime | None = None) -> datetime:
        """Verilen andan SONRAKI ilk yayin slotunu timezone-aware dondurur.

        Donen deger yerel zaman diliminde (ornek Europe/Istanbul) uretilir;
        veritabanina yazmadan once modeller.zaman_metni() UTC'ye cevirir.
        """
        # modeller.py'ye bagimlilik olusturmamak icin zaman burada uretilir;
        # modeller.simdi() ile ayni davranis: UTC, mikrosaniyesiz.
        su_an = (an or datetime.now(UTC).replace(microsecond=0)).astimezone(
            self.zaman_dilimi
        )
        adaylar: list[datetime] = []
        for gun_farki in (0, 1):
            gun = su_an + timedelta(days=gun_farki)
            for saat, dakika in self.slot_saatleri():
                aday = gun.replace(
                    hour=saat, minute=dakika, second=0, microsecond=0
                )
                if aday > su_an:
                    adaylar.append(aday)
        if not adaylar:  # teorik olarak imkansiz; savunmaci
            return su_an + timedelta(days=1)
        return min(adaylar)

    def gizlenmis_ozet(self) -> dict[str, Any]:
        """Aciliste loglanabilir, sir icermeyen ozet."""
        return {
            "proje_koku": str(self.proje_koku),
            "api_surumu": self.api_surumu,
            "zaman_dilimi": self.zamanlama.zaman_dilimi,
            "yayin_slotlari": self.zamanlama.yayin_slotlari,
            "gunluk_limit": self.yayin.gunluk_limit,
            "azami_deneme": self.yayin.azami_deneme,
            "veritabani": str(self.db_yolu),
            "gizli": self.gizli.gizlenmis_ozet(),
        }


# ---------------------------------------------------------------------------
# Yukleme
# ---------------------------------------------------------------------------


def proje_koku_bul() -> Path:
    """src/ klasorunun bir ustu = proje koku."""
    return Path(__file__).resolve().parent.parent


def _yaml_oku(yaml_yolu: Path) -> dict[str, Any]:
    try:
        ham = yaml_yolu.read_text(encoding="utf-8")
    except OSError as hata:
        raise AyarHatasi(f"Ayar dosyasi okunamadi: {yaml_yolu} ({hata})") from hata
    try:
        veri = yaml.safe_load(ham) or {}
    except yaml.YAMLError as hata:
        raise AyarHatasi(
            f"Ayar dosyasi bozuk (YAML hatasi): {yaml_yolu}\n{hata}"
        ) from hata
    if not isinstance(veri, dict):
        raise AyarHatasi(
            f"Ayar dosyasinin kok yapisi sozluk olmali: {yaml_yolu}"
        )
    return veri


def _dogrulama_hatasini_cevir(hata: ValidationError, kaynak: str) -> AyarHatasi:
    satirlar: list[str] = []
    for ayrinti in hata.errors():
        yol = ".".join(str(parca) for parca in ayrinti.get("loc", ())) or "(kok)"
        satirlar.append(f"  - {yol}: {ayrinti.get('msg', 'gecersiz deger')}")
    return AyarHatasi(
        f"Ayar dogrulamasi basarisiz ({kaynak}):\n" + "\n".join(satirlar)
    )


def _mutlak_yap(yol: Path, kok: Path) -> Path:
    return yol if yol.is_absolute() else (kok / yol)


# Ayni yollar icin tekrar tekrar dosya okumamak adina basit onbellek.
_ONBELLEK: dict[tuple[str, str, str], Ayarlar] = {}


def ayarlari_yukle(
    proje_koku: Path | str | None = None,
    *,
    env_yolu: Path | str | None = None,
    yaml_yolu: Path | str | None = None,
    onbellek: bool = True,
) -> Ayarlar:
    """Ayarlari okur, dogrular ve tek bir Ayarlar nesnesi dondurur.

    - env_yolu verilmezse: <proje_koku>/config/.env (yoksa sadece ortam degiskenleri)
    - yaml_yolu verilmezse: <proje_koku>/config/ayarlar.yaml,
      o da yoksa config/ayarlar.ornek.yaml, o da yoksa varsayilanlar.
    """
    kok = Path(proje_koku).resolve() if proje_koku else proje_koku_bul()
    env = Path(env_yolu) if env_yolu else kok / "config" / ".env"
    if yaml_yolu:
        yml = Path(yaml_yolu)
    else:
        yml = kok / "config" / "ayarlar.yaml"
        if not yml.exists():
            ornek = kok / "config" / "ayarlar.ornek.yaml"
            if ornek.exists():
                print(
                    "UYARI: config/ayarlar.yaml bulunamadi, ornek dosya "
                    "kullaniliyor: config/ayarlar.ornek.yaml"
                )
                yml = ornek

    anahtar = (str(kok), str(env), str(yml))
    if onbellek and anahtar in _ONBELLEK:
        return _ONBELLEK[anahtar]

    # --- .env ---
    try:
        gizli = GizliAyarlar(_env_file=str(env) if env.exists() else None)  # type: ignore[call-arg]
    except ValidationError as hata:
        raise _dogrulama_hatasini_cevir(hata, str(env)) from hata

    # --- ayarlar.yaml ---
    ham = _yaml_oku(yml) if yml.exists() else {}
    try:
        yapilandirma = YamlAyarlari(**ham)
    except ValidationError as hata:
        raise _dogrulama_hatasini_cevir(hata, str(yml)) from hata

    # Goreli yollari proje kokune gore mutlak yap
    klasorler = KlasorAyarlari(
        gelen_reels=_mutlak_yap(yapilandirma.klasorler.gelen_reels, kok),
        gelen_story=_mutlak_yap(yapilandirma.klasorler.gelen_story, kok),
        yayinlandi=_mutlak_yap(yapilandirma.klasorler.yayinlandi, kok),
        hata=_mutlak_yap(yapilandirma.klasorler.hata, kok),
        veritabani=_mutlak_yap(yapilandirma.klasorler.veritabani, kok),
    )
    kayit = yapilandirma.kayit.model_copy(
        update={"dosya": _mutlak_yap(yapilandirma.kayit.dosya, kok)}
    )

    ayar = Ayarlar(
        proje_koku=kok,
        gizli=gizli,
        klasorler=klasorler,
        zamanlama=yapilandirma.zamanlama,
        yayin=yapilandirma.yayin,
        dosya=yapilandirma.dosya,
        kayit=kayit,
    )
    if onbellek:
        _ONBELLEK[anahtar] = ayar
    return ayar


def onbellegi_temizle() -> None:
    """Testlerde ayarlari yeniden okumak icin."""
    _ONBELLEK.clear()


__all__ = [
    "Ayarlar",
    "AyarHatasi",
    "DosyaAyarlari",
    "GRAFIK_HOST",
    "GizliAyarlar",
    "KayitAyarlari",
    "KlasorAyarlari",
    "YUKLEME_HOST",
    "YamlAyarlari",
    "YayinAyarlari",
    "ZamanlamaAyarlari",
    "ayarlari_yukle",
    "onbellegi_temizle",
    "proje_koku_bul",
    "token_maskele",
]
