# -*- coding: ascii -*-
"""Giris noktasi: ayarlari yukler, izleyiciyi ve zamanlayiciyi baslatir.

Calistirma:
    python -m src.main                  # normal calisma (Ctrl+C ile durur)
    python -m src.main --kuru-calistirma # hicbir API cagrisi yapmadan rapor ver
    python -m src.main --yoklama         # ag surucusu/paylasim klasoru icin
    python src\\main.py                   # ayni sey (proje koku sys.path'e eklenir)

Yaptigi isler (sirayla):
  1) config/.env ve config/ayarlar.yaml okunur, dogrulanir.
  2) Eksik/hatali ayar varsa ANLASILIR TURKCE hata verilir ve program durur;
     neyi nereden alip nereye yazacaginiz adim adim anlatilir.
  3) Gunlukleme (log) kurulur: konsol + hata/otomasyon.log
  4) Klasorler ve SQLite semasi hazirlanir.
  5) KlasorIzleyici baslatilir (gelen/reels, gelen/story).
  6) Zamanlayici baslatilir (kuyruk taramasi + haftalik token kontrolu).
  7) Ctrl+C gelince TEMIZ kapanis yapilir: calisan yayin varsa bitmesi beklenir.
     Ikinci Ctrl+C beklemeyi keser ve programi hemen sonlandirir.

Turkce karakter kullanilmaz (ASCII), konsol encoding sorunu cikmasin diye.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Sequence

# --- proje kokunu sys.path'e ekle -------------------------------------------
# 'python src/main.py' ile calistirildiginda 'src.xxx' ice aktarimlari bulunsun.
PROJE_KOKU: Final[Path] = Path(__file__).resolve().parent.parent
if str(PROJE_KOKU) not in sys.path:
    sys.path.insert(0, str(PROJE_KOKU))

from src import db, medya  # noqa: E402 - sys.path ayarindan SONRA ice aktarilir
from src.surec_kilidi import KilitAlinamadi, SurecKilidi
from src.arsiv import arsiv_ozeti, gunlugu_yapilandir, gunlukcu_al  # noqa: E402
from src.ayarlar import AyarHatasi, Ayarlar, ayarlari_yukle, token_maskele  # noqa: E402
from src.izleyici import (  # noqa: E402
    WATCHDOG_VAR,
    KlasorIzleyici,
    dosya_adini_ayristir,
    dosya_kabul_edilir_mi,
    klasordeki_adaylar,
    planlanan_zamani_hesapla,
)
from src.modeller import Durum, simdi  # noqa: E402
from src.zamanlayici import Zamanlayici  # noqa: E402

KAYIT: Final[logging.Logger] = gunlukcu_al("main")

#: Cikis kodlari
CIKIS_TAMAM: Final[int] = 0
CIKIS_BEKLENMEYEN: Final[int] = 1
CIKIS_AYAR_HATASI: Final[int] = 2

CIZGI: Final[str] = "=" * 72
INCE_CIZGI: Final[str] = "-" * 72


# ---------------------------------------------------------------------------
# 1) Yapilandirma dogrulamasi - kullaniciya ne yapacagini ADIM ADIM soyler
# ---------------------------------------------------------------------------

#: Eksik .env alani -> kullaniciya gosterilecek Turkce yonerge satirlari
_EKSIK_ALAN_YONERGESI: Final[dict[str, tuple[str, ...]]] = {
    "FB_SAYFA_TOKEN": (
        "FB_SAYFA_TOKEN  (Facebook Sayfa erisim anahtari / Page access token)",
        "  1) https://developers.facebook.com/tools/explorer adresini acin.",
        "  2) Uygulamanizi secin, 'User Token' uretin ve su izinleri isaretleyin:",
        "       instagram_basic, instagram_content_publish, pages_read_engagement",
        "  3) Kisa omurlu tokeni UZUN OMURLU yapin:",
        "       GET /oauth/access_token?grant_type=fb_exchange_token",
        "           &client_id=<UYGULAMA_ID>&client_secret=<UYGULAMA_SIRRI>",
        "           &fb_exchange_token=<KISA_OMURLU_TOKEN>",
        "  4) UZUN omurlu token ile GET /me/accounts cagirin; sayfanizin",
        "     'access_token' alanini kopyalayin. (Bu adimi KISA omurlu token ile",
        "     yaparsaniz sayfa tokeni de kisa omurlu olur ve otomasyon birkac",
        "     saat sonra sessizce durur.)",
        "  5) Kopyaladiginiz degeri config/.env icine FB_SAYFA_TOKEN= olarak yazin.",
    ),
    "IG_HESAP_ID": (
        "IG_HESAP_ID  (Instagram isletme hesabi kimligi)",
        "  1) Instagram hesabiniz 'Isletme' veya 'Icerik Ureticisi' olmali ve",
        "     Facebook Sayfaniza BAGLI olmalidir.",
        "  2) Su cagriyi yapin:",
        "       GET /<SAYFA_ID>?fields=instagram_business_account",
        "           &access_token=<SAYFA_TOKENI>",
        "  3) Donen 'instagram_business_account' icindeki 'id' degerini (17 haneli",
        "     sayi) config/.env icine IG_HESAP_ID= olarak yazin.",
    ),
}


def eksik_ayar_metni(ayar: Ayarlar, eksik: Sequence[str]) -> str:
    """Eksik .env alanlari icin kullaniciya gosterilecek Turkce yonergeyi uretir."""
    env_yolu = ayar.proje_koku / "config" / ".env"
    ornek_yolu = ayar.proje_koku / "config" / ".env.ornek"

    satirlar: list[str] = [
        "",
        CIZGI,
        "  YAPILANDIRMA EKSIK - PROGRAM BASLATILAMADI",
        CIZGI,
        "",
        "  Su alanlar config/.env dosyasinda bos veya tanimsiz:",
        "    " + ", ".join(eksik),
        "",
    ]

    if not env_yolu.exists():
        satirlar.extend(
            [
                "  config/.env dosyasi HENUZ YOK. Once ornekten kopyalayin:",
                "",
                f"    Copy-Item \"{ornek_yolu}\" \"{env_yolu}\"",
                "",
                "  (PowerShell komutu. Sonra dosyayi bir metin editoru ile acin.)",
                "",
            ]
        )
    else:
        satirlar.extend([f"  Duzenlenecek dosya: {env_yolu}", ""])

    satirlar.append(INCE_CIZGI)
    for alan in eksik:
        for satir in _EKSIK_ALAN_YONERGESI.get(
            alan, (f"{alan}  (aciklama icin config/.env.ornek dosyasina bakin)",)
        ):
            satirlar.append("  " + satir)
        satirlar.append(INCE_CIZGI)

    satirlar.extend(
        [
            "",
            "  Dosyayi kaydettikten sonra programi tekrar calistirin:",
            "      python -m src.main",
            "",
            "  Ayarlari yayin yapmadan denemek icin:",
            "      python -m src.main --kuru-calistirma",
            "",
            "  Ayrintili kurulum rehberi: BASLA.md",
            CIZGI,
            "",
        ]
    )
    return "\n".join(satirlar)


def yapilandirmayi_dogrula(ayar: Ayarlar, *, kuru_calistirma: bool = False) -> bool:
    """Yayin icin gereken ayarlar tamam mi? Degilse Turkce yonerge basar.

    Doner:
        True  -> devam edilebilir
        False -> program durmali (sadece normal calismada olur)
    """
    hazir, eksik = ayar.yayina_hazir_mi()
    if hazir:
        return True

    if kuru_calistirma:
        # Kuru calistirma zaten API'ye dokunmaz; eksik ayar calismayi engellemez.
        print("")
        print("  UYARI: config/.env eksik -> " + ", ".join(eksik))
        print("  Kuru calistirma devam ediyor (zaten API cagrisi yapilmiyor).")
        print("  Gercek yayin icin once bu alanlari doldurun; ayrinti: BASLA.md")
        print("")
        return True

    print(eksik_ayar_metni(ayar, eksik), file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# 2) Kuru calistirma (dry-run) - HICBIR API CAGRISI YAPILMAZ
# ---------------------------------------------------------------------------


def _yerel_metin(ayar: Ayarlar, an: datetime | None) -> str:
    """Zamani kullaniciya gosterilecek yerel bicime cevirir."""
    if an is None:
        return "-"
    return an.astimezone(ayar.zaman_dilimi).strftime("%d.%m.%Y %H:%M")


def _slot_simulasyonu(
    ayar: Ayarlar,
    dolu_slotlar: set[datetime],
    adet: int,
    an: datetime,
) -> list[datetime]:
    """Zamansiz gorevlere verilecek slotlari HESAPLAR (veritabanina yazmaz).

    Zamanlayici.slotlari_dagit() ile ayni mantik: her gorev bir sonraki BOS
    slota konur, ayni slota iki gorev yazilmaz.
    """
    dolu = set(dolu_slotlar)
    aday = ayar.sonraki_slot(an)
    sonuc: list[datetime] = []
    for _ in range(max(0, adet)):
        while aday in dolu:
            aday = ayar.sonraki_slot(aday)
        sonuc.append(aday)
        dolu.add(aday)
        aday = ayar.sonraki_slot(aday)
    return sonuc


def _yeni_dosyalari_listele(ayar: Ayarlar) -> list[dict[str, Any]]:
    """Klasorlerde bekleyen ve HENUZ kuyrukta olmayan dosyalari listeler.

    Sadece okuma yapar; hicbir sey kuyruga eklenmez.
    """
    kayitlar: list[dict[str, Any]] = []
    for yol, tip in klasordeki_adaylar(ayar):
        uygun, sebep = dosya_kabul_edilir_mi(yol, ayar, tip)
        if not uygun:
            continue
        mevcut = db.dosyaya_gore_getir(ayar.db_yolu, yol)
        ad_bilgisi = dosya_adini_ayristir(yol.name, zaman_dilimi=ayar.zaman_dilimi)
        kayitlar.append(
            {
                "yol": yol,
                "tip": tip,
                "kuyrukta": mevcut is not None,
                "durum": mevcut.durum if mevcut is not None else None,
                "plan": planlanan_zamani_hesapla(ad_bilgisi, ayar),
                "uyari": ad_bilgisi.uyari,
                "sebep": sebep,
            }
        )
    return kayitlar


def kuru_calistirma(ayar: Ayarlar) -> int:
    """Kuyrugu ve zamanlamayi gosterir; HICBIR Meta API cagrisi yapmaz."""
    print("")
    print(CIZGI)
    print("  KURU CALISTIRMA (deneme kipi) - hicbir yayin yapilmayacak")
    print(CIZGI)

    su_an = simdi()

    # --- ayarlar ---
    print("")
    print("  AYARLAR")
    print(INCE_CIZGI)
    print(f"  Proje koku        : {ayar.proje_koku}")
    print(f"  Zaman dilimi      : {ayar.zamanlama.zaman_dilimi}")
    print(f"  Su an (yerel)     : {_yerel_metin(ayar, su_an)}")
    print(f"  API surumu        : {ayar.api_surumu}")
    print(f"  Sayfa tokeni      : {token_maskele(ayar.token)}")
    print(f"  Instagram hesabi  : {ayar.ig_hesap_id or '<bos>'}")
    print(f"  Yayin slotlari    : {', '.join(ayar.zamanlama.yayin_slotlari)}")
    print(f"  Gunluk limit      : {ayar.yayin.gunluk_limit}")
    print(f"  Azami deneme      : {ayar.yayin.azami_deneme}")
    print(f"  Veritabani        : {ayar.db_yolu}")

    # --- yardimci araclar ---
    araclar = medya.arac_durumu()
    print("")
    print("  ARACLAR")
    print(INCE_CIZGI)
    print(f"  watchdog (klasor izleme) : {'var' if WATCHDOG_VAR else 'YOK -> yoklama kipi'}")
    print(f"  ffmpeg  (gorsel -> video): {'var' if araclar['ffmpeg'] else 'YOK'}")
    print(f"  ffprobe (video kontrolu) : {'var' if araclar['ffprobe'] else 'YOK'}")
    if not araclar["ffprobe"]:
        print("    Not: ffprobe yoksa sure/oran/kodek kontrolu ATLANIR.")
    if not araclar["ffmpeg"]:
        print("    Not: ffmpeg yoksa gorseller Story videosuna cevrilemez.")

    # --- klasordeki yeni dosyalar ---
    yeniler = _yeni_dosyalari_listele(ayar)
    bekleyen_yeni = [k for k in yeniler if not k["kuyrukta"]]
    print("")
    print("  GELEN KLASORLERI")
    print(INCE_CIZGI)
    print(f"  gelen/reels : {ayar.klasorler.gelen_reels}")
    print(f"  gelen/story : {ayar.klasorler.gelen_story}")
    if not yeniler:
        print("  Klasorlerde islenecek dosya yok.")
    for kayit in yeniler:
        durum_metni = (
            f"zaten kuyrukta ({kayit['durum']})"
            if kayit["kuyrukta"]
            else "kuyruga ALINACAK"
        )
        plan_metni = (
            _yerel_metin(ayar, kayit["plan"])
            if kayit["plan"] is not None
            else "ilk uygun slot"
        )
        print(
            f"  - {kayit['yol'].name}  [{kayit['tip']}]  {durum_metni}  "
            f"plan={plan_metni}"
        )
        if kayit["uyari"]:
            print(f"      UYARI: {kayit['uyari']}")

    # --- kuyruk ---
    ozet = db.kuyruk_ozeti(ayar.db_yolu)
    print("")
    print("  KUYRUK OZETI")
    print(INCE_CIZGI)
    for durum in Durum:
        print(f"  {str(durum):<14}: {ozet.get(str(durum), 0)}")

    bekleyenler = db.bekleyenleri_getir(ayar.db_yolu)
    print("")
    print("  KUYRUKTAKI AKTIF GOREVLER")
    print(INCE_CIZGI)
    if not bekleyenler:
        print("  Kuyrukta aktif gorev yok.")
    for gorev in bekleyenler:
        plan_metni = (
            _yerel_metin(ayar, gorev.planlanan_zaman)
            if gorev.planlanan_zaman is not None
            else "(zamansiz)"
        )
        print(
            f"  [#{gorev.id}] {gorev.dosya_adi:<40} {str(gorev.medya_tipi):<8} "
            f"{str(gorev.durum):<13} plan={plan_metni} deneme={gorev.deneme_sayisi}"
        )

    # --- slot dagitimi (sadece hesap, veritabanina yazilmaz) ---
    zamansizlar = [
        gorev
        for gorev in bekleyenler
        if gorev.planlanan_zaman is None and gorev.durum is Durum.BEKLIYOR
    ]
    dolu_slotlar = {
        gorev.planlanan_zaman
        for gorev in bekleyenler
        if gorev.planlanan_zaman is not None
    }
    dolu_slotlar.update(
        kayit["plan"] for kayit in bekleyen_yeni if kayit["plan"] is not None
    )
    zamansiz_yeni = [kayit for kayit in bekleyen_yeni if kayit["plan"] is None]

    print("")
    print("  PLANLANAN YAYIN SIRASI (simulasyon - veritabanina yazilmadi)")
    print(INCE_CIZGI)
    slotlar = _slot_simulasyonu(
        ayar, dolu_slotlar, len(zamansizlar) + len(zamansiz_yeni), su_an
    )
    sira = 0
    for gorev in zamansizlar:
        print(
            f"  [#{gorev.id}] {gorev.dosya_adi:<40} -> "
            f"{_yerel_metin(ayar, slotlar[sira])}"
        )
        sira += 1
    for kayit in zamansiz_yeni:
        print(
            f"  (yeni)  {kayit['yol'].name:<40} -> "
            f"{_yerel_metin(ayar, slotlar[sira])}"
        )
        sira += 1
    if sira == 0:
        print("  Slota yerlestirilecek zamansiz gorev yok.")
    print("")
    print("  Sonraki bos slot: " + _yerel_metin(ayar, ayar.sonraki_slot(su_an)))

    # --- yerel yayin limiti (Meta'ya SORULMAZ) ---
    yerel_sayac = db.son_24_saatte_yayinlanan_sayisi(ayar.db_yolu, su_an)
    print("")
    print("  YAYIN LIMITI (yerel sayac - Meta'ya sorulmadi)")
    print(INCE_CIZGI)
    print(f"  Son 24 saatte yayinlanan : {yerel_sayac} / {ayar.yayin.gunluk_limit}")
    if yerel_sayac >= int(ayar.yayin.gunluk_limit):
        print("  DIKKAT: Gunluk limit dolu, normal calismada yayin yapilmaz.")

    # --- arsiv ---
    arsiv = arsiv_ozeti(ayar)
    print("")
    print("  ARSIV")
    print(INCE_CIZGI)
    print(f"  Yayinlanmis dosya : {arsiv['arsiv_toplam']}")
    print(f"  Hatali dosya      : {arsiv['hatali_dosya']}")

    print("")
    print(CIZGI)
    print("  Kuru calistirma bitti. Meta'ya hicbir istek gonderilmedi.")
    print("  Gercekten yayinlamak icin: python -m src.main")
    print(CIZGI)
    print("")
    return CIKIS_TAMAM


# ---------------------------------------------------------------------------
# 3) Normal calisma
# ---------------------------------------------------------------------------


def _sinyalleri_bagla(dur: threading.Event) -> None:
    """Ctrl+C / kapatma sinyallerini temiz kapanisa baglar.

    Ilk sinyal: kapanis istegi (calisan yayinin bitmesi beklenir).
    Ikinci sinyal: bekleme kesilir, program hemen sonlanir.
    """

    def _isle(sinyal_no: int, _cerceve: Any) -> None:
        if dur.is_set():
            print("")
            KAYIT.warning(
                "Ikinci kapanis istegi alindi, bekleme kesiliyor. "
                "Yarim kalan gorev kuyrukta korunur."
            )
            raise KeyboardInterrupt
        print("")
        KAYIT.info(
            "Kapanis istegi alindi (sinyal %s). Calisan yayin varsa bitmesi "
            "beklenecek; zorla cikmak icin Ctrl+C'ye tekrar basin.",
            sinyal_no,
        )
        dur.set()

    for ad in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sinyal = getattr(signal, ad, None)
        if sinyal is None:
            continue
        try:
            signal.signal(sinyal, _isle)
        except (ValueError, OSError):  # pragma: no cover - platforma bagli
            KAYIT.debug("Sinyal baglanamadi: %s", ad)


def _acilis_bilgisi(ayar: Ayarlar) -> None:
    """Acilista kullaniciya gorunen kisa ozet (sir icermez)."""
    araclar = medya.arac_durumu()
    KAYIT.info(CIZGI)
    KAYIT.info("  insta-otomasyon basliyor")
    KAYIT.info(CIZGI)
    KAYIT.info("Proje koku      : %s", ayar.proje_koku)
    KAYIT.info("Izlenen klasor  : %s", ayar.klasorler.gelen_reels)
    KAYIT.info("Izlenen klasor  : %s", ayar.klasorler.gelen_story)
    KAYIT.info("Veritabani      : %s", ayar.db_yolu)
    KAYIT.info("Gunluk dosyasi  : %s", ayar.kayit.dosya)
    KAYIT.info("Zaman dilimi    : %s", ayar.zamanlama.zaman_dilimi)
    KAYIT.info("Yayin slotlari  : %s", ", ".join(ayar.zamanlama.yayin_slotlari))
    KAYIT.info("Gunluk limit    : %s", ayar.yayin.gunluk_limit)
    KAYIT.info("Sayfa tokeni    : %s", token_maskele(ayar.token))
    KAYIT.info(
        "Araclar         : watchdog=%s ffmpeg=%s ffprobe=%s",
        "var" if WATCHDOG_VAR else "yok",
        "var" if araclar["ffmpeg"] else "yok",
        "var" if araclar["ffprobe"] else "yok",
    )
    KAYIT.info("Durdurmak icin Ctrl+C'ye basin.")
    KAYIT.info(CIZGI)


def calistir(ayar: Ayarlar, *, yoklama: bool = False) -> int:
    """Izleyici + zamanlayiciyi baslatir ve temiz kapanisi yonetir."""
    ayar.klasorleri_hazirla()
    db.veritabanini_hazirla(ayar.db_yolu)

    dur = threading.Event()
    _sinyalleri_bagla(dur)
    _acilis_bilgisi(ayar)

    izleyici = KlasorIzleyici(ayar, yoklama_kullan=yoklama)
    zamanlayici = Zamanlayici(ayar)
    izleyici_basladi = False
    zamanlayici_basladi = False

    try:
        izleyici.baslat()
        izleyici_basladi = True
        zamanlayici.basla(engelleyici=False)
        zamanlayici_basladi = True
        KAYIT.info("Sistem calisiyor. Dosyalarinizi gelen/ klasorlerine birakin.")

        while not dur.is_set():
            dur.wait(1.0)
    except KeyboardInterrupt:
        dur.set()
        KAYIT.info("Klavyeden durdurma istegi alindi.")
    except Exception as hata:
        KAYIT.exception("Beklenmeyen hata, program kapaniyor: %s", hata)
        return CIKIS_BEKLENMEYEN
    finally:
        _kapat(izleyici if izleyici_basladi else None,
               zamanlayici if zamanlayici_basladi else None)

    return CIKIS_TAMAM


def _kapat(izleyici: KlasorIzleyici | None, zamanlayici: Zamanlayici | None) -> None:
    """Temiz kapanis: once yeni is alinmaz, sonra calisan yayinin bitmesi beklenir."""
    KAYIT.info("Kapaniyor...")

    # 1) Once izleyici: yeni dosya kuyruga alinmasin.
    if izleyici is not None:
        try:
            izleyici.durdur()
        except Exception as hata:
            KAYIT.warning("Izleyici duzgun durdurulamadi: %s", hata)

    # 2) Sonra zamanlayici: CALISAN YAYIN VARSA BITMESI BEKLENIR.
    if zamanlayici is not None:
        try:
            KAYIT.info(
                "Calisan yayin varsa bitmesi bekleniyor... "
                "(beklemeyi kesmek icin Ctrl+C)"
            )
            zamanlayici.durdur(bekle=True)
        except KeyboardInterrupt:
            KAYIT.warning(
                "Bekleme kesildi. Yarim kalan gorev kuyrukta duruyor; program "
                "yeniden baslatildiginda kaldigi yerden devam eder."
            )
        except Exception as hata:
            KAYIT.warning("Zamanlayici duzgun durdurulamadi: %s", hata)

    # 3) Gorsel -> video cevriminden kalan gecici dosyalar silinir.
    try:
        silinen = medya.gecici_dosyalari_temizle()
        if silinen:
            KAYIT.info("%d gecici dosya silindi.", silinen)
    except Exception as hata:  # pragma: no cover - savunmaci
        KAYIT.debug("Gecici dosyalar temizlenemedi: %s", hata)

    KAYIT.info("Kapandi. Kuyruktaki gorevler korunuyor.")


# ---------------------------------------------------------------------------
# 4) Komut satiri
# ---------------------------------------------------------------------------


def argumanlari_ayristir(argumanlar: Sequence[str] | None = None) -> argparse.Namespace:
    """Komut satiri secenekleri."""
    ayristirici = argparse.ArgumentParser(
        prog="python -m src.main",
        description=(
            "Instagram Reels/Story otomasyonu: gelen/reels ve gelen/story "
            "klasorlerine atilan videolari planlanan saatte yayinlar."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ornekler:\n"
            "  python -m src.main                   normal calisma\n"
            "  python -m src.main --kuru-calistirma yayin yapmadan durum raporu\n"
            "  python -m src.main --yoklama         ag surucusu / paylasim klasoru\n"
            "  python -m src.main --seviye DEBUG    ayrintili gunluk\n"
        ),
    )
    ayristirici.add_argument(
        "--kuru-calistirma",
        "--dry-run",
        dest="kuru_calistirma",
        action="store_true",
        help=(
            "Hicbir Meta API cagrisi yapmadan kuyrugu, klasorleri ve yayin "
            "zamanlamasini gosterir."
        ),
    )
    ayristirici.add_argument(
        "--yoklama",
        action="store_true",
        help=(
            "Klasor olaylari yerine periyodik yoklama kullanir (ag surucusu, "
            "paylasim klasoru veya sanal makine icin)."
        ),
    )
    ayristirici.add_argument(
        "--seviye",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
        help="Gunluk (log) seviyesi; verilmezse ayarlar.yaml kullanilir.",
    )
    ayristirici.add_argument(
        "--proje-koku",
        default=None,
        help="Farkli bir proje klasoru kullan (varsayilan: bu dosyanin ust klasoru).",
    )
    return ayristirici.parse_args(list(argumanlar) if argumanlar is not None else None)


def main(argumanlar: Sequence[str] | None = None) -> int:
    """Program giris noktasi. Doner: islem sistemi cikis kodu."""
    secenekler = argumanlari_ayristir(argumanlar)

    # --- 1) ayarlari yukle ---
    try:
        ayar = ayarlari_yukle(secenekler.proje_koku or PROJE_KOKU)
    except AyarHatasi as hata:
        print("", file=sys.stderr)
        print(CIZGI, file=sys.stderr)
        print("  AYAR DOSYASI HATASI - PROGRAM BASLATILAMADI", file=sys.stderr)
        print(CIZGI, file=sys.stderr)
        print(str(hata), file=sys.stderr)
        print("", file=sys.stderr)
        print(
            "  Ornek dosyalar: config/.env.ornek ve config/ayarlar.ornek.yaml",
            file=sys.stderr,
        )
        print("  Ayrintili rehber: BASLA.md", file=sys.stderr)
        print(CIZGI, file=sys.stderr)
        return CIKIS_AYAR_HATASI

    # --- 2) gunlukleme ---
    try:
        ayar.klasorleri_hazirla()
    except OSError as hata:
        print(f"Klasorler olusturulamadi: {hata}", file=sys.stderr)
        return CIKIS_AYAR_HATASI
    gunlugu_yapilandir(ayar, seviye=secenekler.seviye)

    # --- 3) yapilandirma dogrulamasi ---
    if not yapilandirmayi_dogrula(
        ayar, kuru_calistirma=secenekler.kuru_calistirma
    ):
        return CIKIS_AYAR_HATASI

    # --- 4) veritabani semasi ---
    try:
        db.veritabanini_hazirla(ayar.db_yolu)
    except Exception as hata:
        print(
            f"Veritabani hazirlanamadi ({ayar.db_yolu}): {hata}", file=sys.stderr
        )
        return CIKIS_BEKLENMEYEN

    # --- 5) calistir ---
    if secenekler.kuru_calistirma:
        # Kuru calistirma yayin yapmaz, kilit gerekmez.
        return kuru_calistirma(ayar)

    # Tek surec garantisi. Iki kopya ayni anda calisirsa ikisi de kuyruktan
    # ayni gorevi alip ayni videoyu Instagram'a iki kez yayinlayabilir.
    try:
        with SurecKilidi(Path(ayar.db_yolu).with_suffix(".lock")):
            return calistir(ayar, yoklama=secenekler.yoklama)
    except KilitAlinamadi as hata:
        print(str(hata), file=sys.stderr)
        return CIKIS_AYAR_HATASI


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CIKIS_AYAR_HATASI",
    "CIKIS_BEKLENMEYEN",
    "CIKIS_TAMAM",
    "PROJE_KOKU",
    "argumanlari_ayristir",
    "calistir",
    "eksik_ayar_metni",
    "kuru_calistirma",
    "main",
    "yapilandirmayi_dogrula",
]
