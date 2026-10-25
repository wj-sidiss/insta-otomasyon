# -*- coding: ascii -*-
"""Moduller arasi baglanti (kopru) testleri.

zamanlayici.py, instagram.py ve arsiv.py modullerini ADLARINA gore cozer.
Bu dosya o baglantilarin GERCEKTEN kuruldugunu dogrular; aksi halde sistem
sessizce "modul baglama hatasi" verip kuyrugu durdurur.

Burada sahte olan tek sey httpx istemcisidir; zamanlayici -> kopru ->
InstagramIstemcisi -> (sahte HTTP) -> arsiv zinciri gercek koddur.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from conftest import gorev_ekle, video_dosyasi_olustur
from src import db, medya
from src.ayarlar import Ayarlar
from src.instagram import InstagramIstemcisi
from src.modeller import Durum, MedyaDogrulamaHatasi, MedyaTipi, simdi
from src.zamanlayici import (
    ModulArsivci,
    ModulYayinci,
    Zamanlayici,
    _durum_kodu_coz,
    _konteyner_sonucu_coz,
    _medya_id_coz,
    _permalink_coz,
)
from test_instagram_akisi import (
    GRAFIK,
    IG_ID,
    MEDYA_ID,
    SURUM,
    TOKEN,
    SahteHttpIstemci,
    _hata_yaniti,
    _tam_akis_yanitlari,
)


# ---------------------------------------------------------------------------
# 1) Yanit cozuculeri instagram.py'nin dataclass'larini anlamali
# ---------------------------------------------------------------------------


def test_konteyner_dataclass_cozulur() -> None:
    from src.instagram import Konteyner

    konteyner = Konteyner(
        id="k-1", yukleme_adresi="https://rupload/x", olusturma_zamani=simdi()
    )
    assert _konteyner_sonucu_coz(konteyner) == ("k-1", "https://rupload/x")


def test_konteyner_durumu_dataclass_cozulur() -> None:
    from src.instagram import KonteynerDurumu

    durum = KonteynerDurumu(durum_kodu="FINISHED", durum="Finished")
    assert _durum_kodu_coz(durum) == "FINISHED"


def test_yayin_sonucu_dataclass_cozulur() -> None:
    from src.instagram import YayinSonucu

    sonuc = YayinSonucu(medya_id="m-1", permalink="https://www.instagram.com/reel/x/")
    assert _medya_id_coz(sonuc) == "m-1"
    assert _permalink_coz(sonuc) == "https://www.instagram.com/reel/x/"


def test_permalink_medya_id_yerine_yazilmaz() -> None:
    """Eski 'yayinla' surumu permalink dondururse medya_id alanina yazilmamali."""
    baglanti = "https://www.instagram.com/reel/AbCdEf/"
    assert _medya_id_coz(baglanti) is None
    assert _permalink_coz(baglanti) == baglanti


# ---------------------------------------------------------------------------
# 2) Kopru gercek modulleri bulabilmeli
# ---------------------------------------------------------------------------


def test_yayinci_koprusu_instagram_istemcisini_uretir(ayar_tokenli: Ayarlar) -> None:
    kopru = ModulYayinci(ayar_tokenli)
    try:
        assert isinstance(kopru._nesne, InstagramIstemcisi)
        # Butun zorunlu adimlar cozulebilmeli
        for adim in (
            "yayin_limiti_kontrol",
            "konteyner_olustur",
            "dosya_yukle",
            "durum_bekle",
            "yayinla",
            "token_gecerli_mi",
            "durum_sorgula",
        ):
            assert kopru._islev(adim) is not None, f"'{adim}' adimi bulunamadi"
    finally:
        if isinstance(kopru._nesne, InstagramIstemcisi):
            kopru._nesne.kapat()


def test_yayinla_adimi_ayrintili_surumu_secer(ayar_tokenli: Ayarlar) -> None:
    """Medya kimligi lazim; sadece permalink donduren surum secilmemeli."""
    kopru = ModulYayinci(ayar_tokenli)
    try:
        islev = kopru._islev("yayinla")
        assert islev.__name__ == "yayinla_ayrintili"
    finally:
        if isinstance(kopru._nesne, InstagramIstemcisi):
            kopru._nesne.kapat()


def test_arsiv_koprusu_gercek_fonksiyonlari_bulur(ayar: Ayarlar) -> None:
    kopru = ModulArsivci(ayar)
    assert kopru._islev("arsivle").__name__ == "basarili_tasi"
    assert kopru._islev("hataya_tasi").__name__ == "hatali_tasi"


def test_arsiv_koprusu_istisna_nesnesi_gecirir(ayar: Ayarlar) -> None:
    """arsiv.hatali_tasi() gercek istisna bekler; kopru metni sarmalamalidir."""
    ayar.klasorleri_hazirla()
    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "bozuk.mp4")
    gorev = db.Gorev(dosya_yolu=video, medya_tipi=MedyaTipi.REELS, id=1)

    kopru = ModulArsivci(ayar)
    yeni_yol = kopru.hataya_tasi(gorev, "Video formati desteklenmiyor")

    assert yeni_yol is not None and yeni_yol.exists()
    kayitlar = list(Path(ayar.klasorler.hata).glob("bozuk*.log"))
    assert len(kayitlar) == 1
    assert "Video formati desteklenmiyor" in kayitlar[0].read_text(encoding="ascii")


# ---------------------------------------------------------------------------
# 3) Ucbastan uca: zamanlayici -> kopru -> instagram -> arsiv
# ---------------------------------------------------------------------------


@pytest.fixture
def uykusuz(monkeypatch: pytest.MonkeyPatch):
    import time as _zaman

    monkeypatch.setattr(_zaman, "sleep", lambda _sure: None)
    yield


@pytest.fixture
def ffprobe_yok(monkeypatch: pytest.MonkeyPatch):
    """ffprobe kurulu degilmis gibi davranir (dokumante edilmis calisma kipi).

    Boylece testler sahte (icerigi gecersiz) MP4 dosyalariyla calisabilir;
    medya katmani sure/oran/kodek kontrolunu yumusak gecer.
    """
    monkeypatch.setattr(medya, "_ffprobe_yolu", lambda: None)
    yield


def _kopru_kur(ayar: Ayarlar, sahte: SahteHttpIstemci) -> ModulYayinci:
    """ModulYayinci'yi sahte HTTP istemcisi kullanan bir istemciyle donatir."""
    kopru = ModulYayinci(ayar)
    if isinstance(kopru._nesne, InstagramIstemcisi):
        kopru._nesne.kapat()
    kopru._nesne = InstagramIstemcisi(
        TOKEN,
        IG_ID,
        SURUM,
        istemci=sahte,
        durum_sorgu_araligi_sn=0.01,
        durum_azami_bekleme_sn=5.0,
        azami_deneme=2,
    )
    return kopru


def test_zamanlayici_gercek_instagram_modulu_ile_yayinlar(
    ayar_tokenli: Ayarlar, uykusuz: None, ffprobe_yok: None
) -> None:
    ayar = ayar_tokenli
    db.veritabanini_hazirla(ayar.db_yolu)
    ayar.klasorleri_hazirla()

    sahte = SahteHttpIstemci(_tam_akis_yanitlari())
    kopru = _kopru_kur(ayar, sahte)
    zamanlayici = Zamanlayici(ayar, yayinci=kopru, ilk_token_kontrolu=False)

    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "tanitim.mp4")
    gorev_id = gorev_ekle(
        Path(ayar.db_yolu),
        video,
        MedyaTipi.REELS,
        aciklama="Merhaba #tanitim",
        planlanan_zaman=simdi() - timedelta(minutes=5),
    )

    zamanlayici.kuyrugu_isle()

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.YAYINLANDI, gorev.hata_mesaji
    assert gorev.medya_id == MEDYA_ID
    assert gorev.permalink == "https://www.instagram.com/reel/AbCdEf/"

    # Gercek HTTP akisi yapilmis olmali
    sira = sahte.cagri_sirasi()
    assert f"GET {GRAFIK}/{IG_ID}/content_publishing_limit" in sira
    assert f"POST {GRAFIK}/{IG_ID}/media" in sira
    assert f"POST {GRAFIK}/{IG_ID}/media_publish" in sira

    # Konteyner istegi dogru kurulmus olmali
    konteyner = sahte.ilk("POST", f"{GRAFIK}/{IG_ID}/media")
    assert konteyner.veri["upload_type"] == "resumable"
    assert konteyner.veri["media_type"] == "REELS"
    assert konteyner.veri["caption"] == "Merhaba #tanitim"
    assert "video_url" not in konteyner.veri

    # Dosya arsive tasinmis olmali
    assert not video.exists()
    assert len(list(Path(ayar.klasorler.yayinlandi).rglob("*tanitim.mp4"))) == 1


def test_token_hatasi_kuyrugu_durdurur_dosya_tasinmaz(
    ayar_tokenli: Ayarlar, uykusuz: None, ffprobe_yok: None
) -> None:
    ayar = ayar_tokenli
    db.veritabanini_hazirla(ayar.db_yolu)
    ayar.klasorleri_hazirla()

    yanitlar = _tam_akis_yanitlari()
    yanitlar[("POST", f"{GRAFIK}/{IG_ID}/media")] = [
        _hata_yaniti(400, "Error validating access token", hata_kodu=190)
    ]
    sahte = SahteHttpIstemci(yanitlar)
    kopru = _kopru_kur(ayar, sahte)
    zamanlayici = Zamanlayici(ayar, yayinci=kopru, ilk_token_kontrolu=False)

    video = video_dosyasi_olustur(ayar.klasorler.gelen_reels, "tanitim.mp4")
    gorev_id = gorev_ekle(
        Path(ayar.db_yolu),
        video,
        MedyaTipi.REELS,
        planlanan_zaman=simdi() - timedelta(minutes=5),
    )

    zamanlayici.kuyrugu_isle()

    assert zamanlayici.kuyruk_durduruldu is True
    assert db.gorev_getir(ayar.db_yolu, gorev_id).durum is Durum.BEKLIYOR
    assert video.exists(), "Token hatasinda dosya hata/ klasorune tasinmamali"


def test_bozuk_dosya_kalici_hata_ile_hata_klasorune_gider(
    ayar_tokenli: Ayarlar, uykusuz: None, ffprobe_yok: None
) -> None:
    """Medya katmani elerse konteyner bile acilmaz, dosya hata/ klasorune gider."""
    ayar = ayar_tokenli
    db.veritabanini_hazirla(ayar.db_yolu)
    ayar.klasorleri_hazirla()

    sahte = SahteHttpIstemci(_tam_akis_yanitlari())
    kopru = _kopru_kur(ayar, sahte)
    zamanlayici = Zamanlayici(ayar, yayinci=kopru, ilk_token_kontrolu=False)

    # Uzantisi kabul edilen ama icerigi bos olan dosya: yerel dogrulama eler
    bos = ayar.klasorler.gelen_reels / "bos.mp4"
    bos.write_bytes(b"")
    gorev_id = gorev_ekle(
        Path(ayar.db_yolu),
        bos,
        MedyaTipi.REELS,
        planlanan_zaman=simdi() - timedelta(minutes=5),
    )

    zamanlayici.kuyrugu_isle()

    gorev = db.gorev_getir(ayar.db_yolu, gorev_id)
    assert gorev.durum is Durum.HATA
    assert f"POST {GRAFIK}/{IG_ID}/media" not in sahte.cagri_sirasi()
    assert len(list(Path(ayar.klasorler.hata).glob("bos*.mp4"))) == 1
    assert len(list(Path(ayar.klasorler.hata).glob("bos*.log"))) == 1


# ---------------------------------------------------------------------------
# 4) Medya katmani baglantisi
# ---------------------------------------------------------------------------


def test_gorsel_reels_olarak_yayinlanamaz(ayar: Ayarlar) -> None:
    ayar.klasorleri_hazirla()
    gorsel = ayar.klasorler.gelen_reels / "kapak.jpg"
    _jpeg_yaz(gorsel)

    with pytest.raises(MedyaDogrulamaHatasi) as bilgi:
        medya.yayina_hazirla(gorsel, MedyaTipi.REELS)
    assert "gelen/story" in str(bilgi.value)


@pytest.mark.skipif(not medya.ffmpeg_var_mi(), reason="ffmpeg kurulu degil")
def test_gorsel_story_videosuna_cevrilir(ayar: Ayarlar) -> None:
    ayar.klasorleri_hazirla()
    gorsel = ayar.klasorler.gelen_story / "kapak.jpg"
    _jpeg_yaz(gorsel)

    yol, bilgi = medya.yayina_hazirla(gorsel, MedyaTipi.STORIES)
    try:
        assert yol.suffix == ".mp4"
        assert yol.exists() and yol.stat().st_size > 0
        assert medya.gecici_dosya_mi(yol), "Uretilen video gecici dosya olarak izlenmeli"
        if bilgi.sure_sn is not None:
            assert 3 <= bilgi.sure_sn <= 60
    finally:
        medya.gecici_dosyayi_sil(yol)


def _jpeg_yaz(yol: Path) -> None:
    """Test icin kucuk bir JPEG uretir."""
    from PIL import Image

    yol.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (540, 960), (32, 64, 128)).save(yol, format="JPEG", quality=80)
