# -*- coding: ascii -*-
"""Zamanlayici testleri: yayin limiti, slot dagitimi ve hata politikasi.

Ozellikle dogrulanan davranis:
  - LIMIT DOLU ise HICBIR yayin yapilmaz (ne yerel sayac ne Meta kotasi asilir)
  - kota dolu bir HATA degil ERTELEMEdir (deneme sayisi artmaz)
  - kalici hatada dosya hata/ klasorune tasinir ve yanina .log yazilir
  - gecici hatada gorev kuyruga geri doner, azami denemede hataya duser
  - token gecersizse kuyruk durdurulur

Yayinci (instagram) katmani sahte bir nesne ile degistirilir; ag'a istek gitmez.
Arsivci ise GERCEK src/arsiv.py modulu uzerinden calisir, boylece moduller
arasi baglanti da test edilmis olur.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from conftest import ayari_degistir, gorev_ekle, video_dosyasi_olustur
from src import db
from src.ayarlar import Ayarlar
from src.modeller import (
    Durum,
    GeciciHata,
    Gorev,
    KaliciHata,
    KotaDoluHatasi,
    MedyaTipi,
    TokenGecersizHatasi,
    simdi,
)
from src.zamanlayici import KotaDurumu, Zamanlayici


# ---------------------------------------------------------------------------
# Sahte yayinci
# ---------------------------------------------------------------------------


class SahteYayinci:
    """instagram.py yerine gecen, ag'a cikmayan sahte yayinci."""

    def __init__(
        self,
        *,
        kota: tuple[int, int] = (0, 50),
        durum: str = "FINISHED",
        yayin_hatasi: BaseException | None = None,
        konteyner_hatasi: BaseException | None = None,
        token_gecerli: bool = True,
    ) -> None:
        self.kota = kota
        self.durum = durum
        self.yayin_hatasi = yayin_hatasi
        self.konteyner_hatasi = konteyner_hatasi
        self.token_gecerli = token_gecerli

        self.kota_sorgusu = 0
        self.konteynerler: list[str] = []
        self.yuklemeler: list[str] = []
        self.yayinlar: list[str] = []

    # -- Yayinci arayuzu -----------------------------------------------------

    def yayin_limiti_kontrol(self) -> tuple[int, int]:
        self.kota_sorgusu += 1
        return self.kota

    def konteyner_olustur(self, gorev: Gorev) -> tuple[str, str | None]:
        if self.konteyner_hatasi is not None:
            raise self.konteyner_hatasi
        self.konteynerler.append(gorev.dosya_adi)
        return f"konteyner-{len(self.konteynerler)}", None

    def dosya_yukle(
        self, gorev: Gorev, konteyner_id: str, yukleme_uri: str | None
    ) -> None:
        self.yuklemeler.append(gorev.dosya_adi)

    def durum_bekle(self, gorev: Gorev, konteyner_id: str) -> str:
        return self.durum

    def durum_sorgula(self, gorev: Gorev, konteyner_id: str) -> str:
        return self.durum

    def yayinla(self, gorev: Gorev, konteyner_id: str) -> str | None:
        if self.yayin_hatasi is not None:
            raise self.yayin_hatasi
        self.yayinlar.append(gorev.dosya_adi)
        return f"medya-{len(self.yayinlar)}"

    def permalink_getir(self, medya_id: str) -> str | None:
        return f"https://www.instagram.com/reel/{medya_id}/"

    def token_gecerli_mi(self) -> bool:
        return self.token_gecerli


def _zamanlayici_uret(
    ayar: Ayarlar, yayinci: SahteYayinci | None = None
) -> tuple[Zamanlayici, SahteYayinci]:
    """Hazir veritabani + sahte yayinci ile zamanlayici uretir."""
    db.veritabanini_hazirla(ayar.db_yolu)
    ayar.klasorleri_hazirla()
    sahte = yayinci or SahteYayinci()
    return Zamanlayici(ayar, yayinci=sahte, ilk_token_kontrolu=False), sahte


def _gecmis_gorev(ayar: Ayarlar, ad: str = "tanitim.mp4") -> tuple[int, Path]:
    """Zamani gelmis (gecmis planli) bir gorev olusturur."""
    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, ad)
    gorev_id = gorev_ekle(
        Path(ayar.db_yolu),
        video,
        MedyaTipi.REELS,
        aciklama="test aciklamasi",
        planlanan_zaman=simdi() - timedelta(minutes=5),
    )
    return gorev_id, video


# ---------------------------------------------------------------------------
# 1) LIMIT DOLU -> YAYIN YAPILMAZ
# ---------------------------------------------------------------------------


def test_yerel_gunluk_limit_dolu_ise_yayin_yapilmaz(ayar: Ayarlar) -> None:
    dar_ayar = ayari_degistir(ayar, yayin={"gunluk_limit": 1})
    zamanlayici, sahte = _zamanlayici_uret(dar_ayar)

    # Son 24 saatte zaten 1 yayin yapilmis
    eski = video_dosyasi_olustur(dar_ayar.klasorler.yayinlandi, "onceki.mp4")
    eski_id = gorev_ekle(Path(dar_ayar.db_yolu), eski)
    db.durum_guncelle(
        dar_ayar.db_yolu, eski_id, Durum.YAYINLANDI, yayin_zamani=simdi()
    )

    gorev_id, _ = _gecmis_gorev(dar_ayar)

    uygun, aciklama = zamanlayici.yayin_limiti_kontrol()
    assert uygun is False
    assert "limit" in aciklama.lower()

    zamanlayici.kuyrugu_isle()

    assert sahte.yayinlar == [], "Limit doluyken yayin yapilmamali"
    assert sahte.konteynerler == [], "Limit doluyken konteyner bile acilmamali"
    assert sahte.kota_sorgusu == 0, "Yerel sayac doluyken Meta'ya sorulmamali"
    assert db.gorev_getir(dar_ayar.db_yolu, gorev_id).durum is Durum.BEKLIYOR


def test_meta_kotasi_dolu_ise_yayin_yapilmaz(ayar: Ayarlar) -> None:
    zamanlayici, sahte = _zamanlayici_uret(ayar, SahteYayinci(kota=(50, 50)))
    gorev_id, _ = _gecmis_gorev(ayar)

    uygun, aciklama = zamanlayici.yayin_limiti_kontrol()
    assert uygun is False
    assert "Meta" in aciklama

    zamanlayici.kuyrugu_isle()

    assert sahte.yayinlar == []
    assert sahte.konteynerler == []
    assert db.gorev_getir(ayar.db_yolu, gorev_id).durum is Durum.BEKLIYOR


def test_kota_sorgusu_kapatilabilir(ayar: Ayarlar) -> None:
    kapali = ayari_degistir(ayar, yayin={"kota_sorgula": False})
    zamanlayici, sahte = _zamanlayici_uret(kapali, SahteYayinci(kota=(50, 50)))
    _gecmis_gorev(kapali)

    uygun, aciklama = zamanlayici.yayin_limiti_kontrol()
    assert uygun is True, "kota_sorgula=false ise Meta kotasi engel olmamali"
    assert sahte.kota_sorgusu == 0
    assert "sorgulanmadi" in aciklama


def test_meta_kotasi_okunamazsa_yayin_engellenmez(ayar: Ayarlar) -> None:
    class PatlayanYayinci(SahteYayinci):
        def yayin_limiti_kontrol(self) -> tuple[int, int]:
            raise RuntimeError("ag hatasi")

    zamanlayici, _ = _zamanlayici_uret(ayar, PatlayanYayinci())
    uygun, aciklama = zamanlayici.yayin_limiti_kontrol()
    assert uygun is True, "Kota okunamazsa yerel sayac korumasi yeterlidir"
    assert "okunamadi" in aciklama


def test_kota_durumu_bilinmiyorsa_dolu_sayilmaz() -> None:
    assert KotaDurumu().dolu_mu() is False
    assert KotaDurumu(kullanilan=3, toplam=10).dolu_mu() is False
    assert KotaDurumu(kullanilan=10, toplam=10).dolu_mu() is True
    assert KotaDurumu(uygun=False).dolu_mu() is True


# ---------------------------------------------------------------------------
# 2) Basarili yayin akisi
# ---------------------------------------------------------------------------


def test_basarili_yayin_dosyayi_arsive_tasir(ayar: Ayarlar) -> None:
    zamanlayici, sahte = _zamanlayici_uret(ayar)
    gorev_id, video = _gecmis_gorev(ayar)

    zamanlayici.kuyrugu_isle()

    assert sahte.konteynerler == ["tanitim.mp4"]
    assert sahte.yuklemeler == ["tanitim.mp4"]
    assert sahte.yayinlar == ["tanitim.mp4"]

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.YAYINLANDI
    assert gorev.medya_id == "medya-1"
    assert gorev.permalink is not None and gorev.permalink.startswith("https://")
    assert gorev.yayin_zamani is not None

    # Dosya gelen/ klasorunden cikip yayinlandi/YYYY-AA/ altina tasinmali
    assert not video.exists()
    arsivlenen = list(Path(ayar.klasorler.yayinlandi).rglob("*tanitim.mp4"))
    assert len(arsivlenen) == 1
    assert gorev.dosya_yolu == arsivlenen[0].resolve()


def test_yayinlanmis_konteyner_tekrar_yayinlanmaz(ayar: Ayarlar) -> None:
    """durum_bekle PUBLISHED dondurduyse media_publish CAGRILMAZ (cift yayin riski)."""
    zamanlayici, sahte = _zamanlayici_uret(ayar, SahteYayinci(durum="PUBLISHED"))
    gorev_id, _ = _gecmis_gorev(ayar)

    zamanlayici.kuyrugu_isle()

    assert sahte.yayinlar == [], "PUBLISHED durumunda tekrar yayin yapilmamali"
    assert db.gorev_getir(ayar.db_yolu, gorev_id).durum is Durum.YAYINLANDI


def test_hala_islenen_video_beklemede_kalir(ayar: Ayarlar) -> None:
    zamanlayici, sahte = _zamanlayici_uret(ayar, SahteYayinci(durum="IN_PROGRESS"))
    gorev_id, video = _gecmis_gorev(ayar)

    zamanlayici.kuyrugu_isle()

    assert sahte.yayinlar == []
    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.HAZIRLANIYOR, "Gorev bir sonraki turda surdurulmeli"
    assert gorev.container_id is not None
    assert video.exists(), "Yayinlanmadan dosya tasinmamali"


def test_tur_basina_gorev_siniri(ayar: Ayarlar) -> None:
    db.veritabanini_hazirla(ayar.db_yolu)
    ayar.klasorleri_hazirla()
    sahte = SahteYayinci()
    zamanlayici = Zamanlayici(
        ayar, yayinci=sahte, tur_basina_gorev=2, ilk_token_kontrolu=False
    )
    for sira in range(4):
        video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, f"video{sira}.mp4")
        gorev_ekle(
            Path(ayar.db_yolu),
            video,
            MedyaTipi.REELS,
            planlanan_zaman=simdi() - timedelta(minutes=10 - sira),
        )

    zamanlayici.kuyrugu_isle()
    assert len(sahte.yayinlar) == 2, "Bir turda en fazla tur_basina_gorev yayinlanir"


# ---------------------------------------------------------------------------
# 3) Hata politikasi
# ---------------------------------------------------------------------------


def test_kalici_hata_dosyayi_hata_klasorune_tasir(ayar: Ayarlar) -> None:
    hata = KaliciHata("Video formati desteklenmiyor", alt_kod=2207026)
    zamanlayici, sahte = _zamanlayici_uret(ayar, SahteYayinci(yayin_hatasi=hata))
    gorev_id, video = _gecmis_gorev(ayar)

    zamanlayici.kuyrugu_isle()

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.HATA
    assert gorev.hata_mesaji is not None

    assert not video.exists()
    hata_klasoru = Path(ayar.klasorler.hata)
    tasinan = list(hata_klasoru.glob("tanitim*.mp4"))
    assert len(tasinan) == 1, "Dosya hata/ klasorune tasinmali"

    # arsiv.py yanina Turkce aciklamali .log dosyasi yazmali
    kayitlar = list(hata_klasoru.glob("tanitim*.log"))
    assert len(kayitlar) == 1
    icerik = kayitlar[0].read_text(encoding="ascii")
    assert "Instagram yayin hatasi" in icerik
    assert "2207026" in icerik
    assert "moov" in icerik, "Alt koda ozel Turkce cozum onerisi yazilmali"
    assert "HAYIR (kalici hata)" in icerik


def test_gecici_hata_gorevi_kuyruga_geri_koyar(ayar: Ayarlar) -> None:
    hata = GeciciHata("Instagram sunucu hatasi", alt_kod=2207001)
    zamanlayici, _ = _zamanlayici_uret(ayar, SahteYayinci(yayin_hatasi=hata))
    gorev_id, video = _gecmis_gorev(ayar)

    zamanlayici.kuyrugu_isle()

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.BEKLIYOR
    assert gorev.deneme_sayisi == 1
    assert gorev.planlanan_zaman is not None and gorev.planlanan_zaman > simdi()
    assert video.exists(), "Gecici hatada dosya tasinmamali"


def test_azami_denemede_gecici_hata_kalici_olur(ayar: Ayarlar) -> None:
    hata = GeciciHata("surekli basarisiz", alt_kod=2207001)
    zamanlayici, _ = _zamanlayici_uret(ayar, SahteYayinci(yayin_hatasi=hata))
    gorev_id, video = _gecmis_gorev(ayar)
    # azami_deneme = 3 (conftest); iki deneme zaten yapilmis kabul edilir
    db.durum_guncelle(ayar.db_yolu, gorev_id, deneme_artir=True)
    db.durum_guncelle(ayar.db_yolu, gorev_id, deneme_artir=True)

    zamanlayici.kuyrugu_isle()

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.HATA
    assert "Azami deneme" in (gorev.hata_mesaji or "")
    assert not video.exists()


def test_kota_dolu_hatasi_erteleme_sayilir(ayar: Ayarlar) -> None:
    hata = KotaDoluHatasi("Gunluk yayin kotasi doldu", kod=9, alt_kod=2207042)
    zamanlayici, _ = _zamanlayici_uret(ayar, SahteYayinci(yayin_hatasi=hata))
    gorev_id, video = _gecmis_gorev(ayar)

    zamanlayici.kuyrugu_isle()

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.BEKLIYOR
    assert gorev.deneme_sayisi == 0, "Kota dolu bir HATA degildir, deneme artmaz"
    assert gorev.planlanan_zaman is not None and gorev.planlanan_zaman > simdi()
    assert zamanlayici.kota_bekleme_zamani is not None
    assert video.exists()


def test_kota_beklemesi_sonraki_turu_atlatir(ayar: Ayarlar) -> None:
    hata = KotaDoluHatasi("kota dolu", kod=9, alt_kod=2207042)
    zamanlayici, sahte = _zamanlayici_uret(ayar, SahteYayinci(yayin_hatasi=hata))
    _gecmis_gorev(ayar)

    zamanlayici.kuyrugu_isle()
    onceki_konteyner = len(sahte.konteynerler)
    zamanlayici.kuyrugu_isle()   # kota beklemesi suruyor

    assert len(sahte.konteynerler) == onceki_konteyner


def test_token_gecersizse_kuyruk_durdurulur(ayar: Ayarlar) -> None:
    hata = TokenGecersizHatasi("Erisim anahtari gecersiz", kod=190)
    zamanlayici, sahte = _zamanlayici_uret(ayar, SahteYayinci(yayin_hatasi=hata))
    gorev_id, video = _gecmis_gorev(ayar)

    zamanlayici.kuyrugu_isle()

    assert zamanlayici.kuyruk_durduruldu is True
    assert zamanlayici.durdurma_sebebi is not None
    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.BEKLIYOR, "Gorev kaybolmamali"
    assert video.exists(), "Token hatasinda dosya hata/ klasorune TASINMAZ"

    # Durdurulmus kuyrukta yeni tur hicbir sey yapmamali
    onceki = len(sahte.konteynerler)
    zamanlayici.kuyrugu_isle()
    assert len(sahte.konteynerler) == onceki

    zamanlayici.kuyrugu_devam_ettir()
    assert zamanlayici.kuyruk_durduruldu is False


def test_dosya_silinmisse_gorev_hataya_duser(ayar: Ayarlar) -> None:
    zamanlayici, sahte = _zamanlayici_uret(ayar)
    gorev_id, video = _gecmis_gorev(ayar)
    video.unlink()

    zamanlayici.kuyrugu_isle()

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.HATA
    assert sahte.konteynerler == []


# ---------------------------------------------------------------------------
# 4) Slot dagitimi
# ---------------------------------------------------------------------------


def test_zamansiz_gorevler_ayri_slotlara_dagitilir(ayar: Ayarlar) -> None:
    zamanlayici, _ = _zamanlayici_uret(ayar)
    for sira in range(3):
        video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, f"video{sira}.mp4")
        gorev_ekle(Path(ayar.db_yolu), video)

    yerlestirilen = zamanlayici.slotlari_dagit()
    assert yerlestirilen == 3

    planlar = [
        gorev.planlanan_zaman for gorev in db.bekleyenleri_getir(ayar.db_yolu)
    ]
    assert all(plan is not None for plan in planlar)
    assert len(set(planlar)) == 3, "Ayni slota iki gorev konmamali"
    assert planlar == sorted(planlar)


def test_planli_gorevin_slotu_degistirilmez(ayar: Ayarlar) -> None:
    zamanlayici, _ = _zamanlayici_uret(ayar)
    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "planli.mp4")
    plan = simdi() + timedelta(days=1)
    gorev_id = gorev_ekle(Path(ayar.db_yolu), video, planlanan_zaman=plan)

    zamanlayici.slotlari_dagit()

    assert db.gorev_getir(ayar.db_yolu, gorev_id).planlanan_zaman == plan


def test_dolu_slot_atlanir(ayar: Ayarlar) -> None:
    zamanlayici, _ = _zamanlayici_uret(ayar)
    dolu = ayar.sonraki_slot(simdi())
    planli = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "planli.mp4")
    gorev_ekle(Path(ayar.db_yolu), planli, planlanan_zaman=dolu)
    zamansiz = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "zamansiz.mp4")
    zamansiz_id = gorev_ekle(Path(ayar.db_yolu), zamansiz)

    zamanlayici.slotlari_dagit()

    yeni_plan = db.gorev_getir(ayar.db_yolu, zamansiz_id).planlanan_zaman
    assert yeni_plan is not None and yeni_plan != dolu


# ---------------------------------------------------------------------------
# 5) Acilista toparlama ve ozet
# ---------------------------------------------------------------------------


def test_suresi_dolan_konteyner_bastan_alinir(ayar: Ayarlar) -> None:
    zamanlayici, _ = _zamanlayici_uret(ayar)
    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "yarim.mp4")
    gorev_id = gorev_ekle(Path(ayar.db_yolu), video)
    db.durum_guncelle(
        ayar.db_yolu,
        gorev_id,
        Durum.YUKLENIYOR,
        container_id="eski-konteyner",
        container_zamani=simdi() - timedelta(hours=25),
    )

    assert zamanlayici.baslangic_toparlamasi() == 1

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.BEKLIYOR
    assert gorev.container_id is None


def test_taze_konteyner_kaldigi_yerden_surdurulur(ayar: Ayarlar) -> None:
    zamanlayici, _ = _zamanlayici_uret(ayar)
    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "yarim.mp4")
    gorev_id = gorev_ekle(Path(ayar.db_yolu), video)
    db.durum_guncelle(
        ayar.db_yolu,
        gorev_id,
        Durum.HAZIRLANIYOR,
        container_id="taze-konteyner",
        container_zamani=simdi() - timedelta(hours=1),
    )

    assert zamanlayici.baslangic_toparlamasi() == 0
    assert db.gorev_getir(ayar.db_yolu, gorev_id).durum is Durum.HAZIRLANIYOR


def test_ozet_sir_icermez(ayar: Ayarlar) -> None:
    zamanlayici, _ = _zamanlayici_uret(ayar)
    ozet: dict[str, Any] = zamanlayici.ozet()
    assert set(ozet) >= {
        "kuyruk",
        "son_24_saat_yayin",
        "gunluk_limit",
        "kuyruk_durduruldu",
        "yayin_slotlari",
    }
    assert "token" not in str(ozet).lower()
