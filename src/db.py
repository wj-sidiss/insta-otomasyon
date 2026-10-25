# -*- coding: ascii -*-
"""SQLite kuyrugu: gorevler tablosu ve uzerindeki islemler.

Tasarim kararlari:
  - dosya_yolu UNIQUE: ayni dosya iki kez kuyruga giremez (watchdog bir dosya
    icin birden fazla olay uretir, bu kolon o yuzden benzersizdir).
  - WAL modu acik: okuma ve yazma birbirini kilitlemez.
  - Butun zamanlar UTC, mikrosaniyesiz ISO 8601 METIN olarak saklanir
    (bkz. modeller.zaman_metni). Sabit uzunlukta oldugu icin SQLite'ta
    metin karsilastirmasi kronolojik karsilastirmaya esittir.
  - Her fonksiyon veritabani yolunu parametre alir ve kendi baglantisini acar;
    boylece modul durum tutmaz ve testlerde gecici dosya kullanilabilir.

Import kurali (butun proje icin): proje koku sys.path'te olur ve moduller
'from src.db import ...' seklinde ice aktarilir.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from src.modeller import (
    AKTIF_DURUMLAR,
    KOTA_PENCERESI_SAAT,
    Durum,
    Gorev,
    MedyaTipi,
    simdi,
    zaman_metni,
)

#: Proje gunlukcu agaci "insta.*" altindadir (bkz. arsiv.gunlugu_yapilandir).
kayit = logging.getLogger("insta.db")

VeriTabaniYolu = Path | str

#: durum_guncelle() icinde "bu alana dokunma" anlamina gelen nobetci deger.
#: (None gecerli bir deger oldugu icin ayri bir nobetci sart.)
DOKUNMA: Final[Any] = object()

SEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS gorevler (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dosya_yolu        TEXT    NOT NULL UNIQUE,
    medya_tipi        TEXT    NOT NULL,
    aciklama          TEXT,
    planlanan_zaman   TEXT,
    durum             TEXT    NOT NULL DEFAULT 'bekliyor',
    container_id      TEXT,
    container_zamani  TEXT,
    permalink         TEXT,
    medya_id          TEXT,
    deneme_sayisi     INTEGER NOT NULL DEFAULT 0,
    hata_mesaji       TEXT,
    olusturma_zamani  TEXT    NOT NULL,
    guncelleme_zamani TEXT    NOT NULL,
    yayin_zamani      TEXT
);

CREATE INDEX IF NOT EXISTS idx_gorev_durum
    ON gorevler (durum, planlanan_zaman);
CREATE INDEX IF NOT EXISTS idx_gorev_yayin
    ON gorevler (yayin_zamani);
CREATE INDEX IF NOT EXISTS idx_gorev_container
    ON gorevler (container_id);
"""

#: Sema gelistikce eklenen kolonlar (eski veritabanlarini onarmak icin)
_BEKLENEN_KOLONLAR: Final[dict[str, str]] = {
    "container_zamani": "TEXT",
    "medya_id": "TEXT",
    "guncelleme_zamani": "TEXT",
    "yayin_zamani": "TEXT",
}


# ---------------------------------------------------------------------------
# Baglanti yonetimi
# ---------------------------------------------------------------------------


@contextmanager
def baglanti(db_yolu: VeriTabaniYolu) -> Iterator[sqlite3.Connection]:
    """Baglanti acar, WAL modunu ayarlar, cikista commit/rollback yapar.

    Kullanim:
        with baglanti(db_yolu) as baglan:
            baglan.execute(...)
    """
    yol = Path(db_yolu)
    yol.parent.mkdir(parents=True, exist_ok=True)
    baglan = sqlite3.connect(str(yol), timeout=30.0)
    baglan.row_factory = sqlite3.Row
    try:
        baglan.execute("PRAGMA journal_mode=WAL")
        baglan.execute("PRAGMA synchronous=NORMAL")
        baglan.execute("PRAGMA busy_timeout=15000")
        baglan.execute("PRAGMA foreign_keys=ON")
        yield baglan
        baglan.commit()
    except Exception:
        baglan.rollback()
        raise
    finally:
        baglan.close()


def veritabanini_hazirla(db_yolu: VeriTabaniYolu) -> None:
    """Semayi olusturur ve eksik kolonlari tamamlar. Her aciliste cagrilabilir."""
    with baglanti(db_yolu) as baglan:
        baglan.executescript(SEMA)
        _eksik_kolonlari_ekle(baglan)


def _eksik_kolonlari_ekle(baglan: sqlite3.Connection) -> None:
    """Eski bir kuyruk.db varsa yeni kolonlari sessizce ekler."""
    mevcut = {
        str(satir["name"])
        for satir in baglan.execute("PRAGMA table_info(gorevler)").fetchall()
    }
    for kolon, tip in _BEKLENEN_KOLONLAR.items():
        if kolon not in mevcut:
            baglan.execute(f"ALTER TABLE gorevler ADD COLUMN {kolon} {tip}")


def _yol_metni(dosya_yolu: Path | str) -> str:
    """Dosya yolunu benzersiz anahtar olarak kullanilabilir bicime getirir.

    Ayni dosyanin farkli yazimlari (goreli/mutlak) ayni satira dusmeli.
    """
    return str(Path(dosya_yolu).expanduser().resolve())


def _zaman(deger: datetime | None) -> str | None:
    return zaman_metni(deger) if deger is not None else None


# ---------------------------------------------------------------------------
# Yazma islemleri
# ---------------------------------------------------------------------------


def kuyruga_ekle(
    db_yolu: VeriTabaniYolu,
    dosya_yolu: Path | str,
    medya_tipi: MedyaTipi | str,
    *,
    aciklama: str | None = None,
    planlanan_zaman: datetime | None = None,
) -> int | None:
    """Yeni gorevi kuyruga ekler.

    Doner:
        int  -> yeni eklenen gorevin id'si
        None -> dosya ZATEN kuyrukta (UNIQUE kisiti), hicbir sey yapilmadi
    """
    tip = MedyaTipi(str(medya_tipi))
    an = zaman_metni(simdi())
    with baglanti(db_yolu) as baglan:
        imlec = baglan.execute(
            """
            INSERT OR IGNORE INTO gorevler (
                dosya_yolu, medya_tipi, aciklama, planlanan_zaman, durum,
                deneme_sayisi, olusturma_zamani, guncelleme_zamani
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                _yol_metni(dosya_yolu),
                str(tip),
                aciklama,
                _zaman(planlanan_zaman),
                str(Durum.BEKLIYOR),
                an,
                an,
            ),
        )
        if imlec.rowcount == 0:
            return None
        return int(imlec.lastrowid or 0)


def durum_guncelle(
    db_yolu: VeriTabaniYolu,
    gorev_id: int,
    durum: Durum | str | None = None,
    *,
    container_id: Any = DOKUNMA,
    container_zamani: Any = DOKUNMA,
    permalink: Any = DOKUNMA,
    medya_id: Any = DOKUNMA,
    hata_mesaji: Any = DOKUNMA,
    planlanan_zaman: Any = DOKUNMA,
    yayin_zamani: Any = DOKUNMA,
    deneme_artir: bool = False,
    deneme_sifirla: bool = False,
) -> bool:
    """Gorevin durumunu ve secilen alanlarini gunceller.

    Sadece verilen alanlar degisir; verilmeyenlere dokunulmaz (DOKUNMA nobetcisi).
    Bir alani bilerek bosaltmak icin acikca None gonderin (ornek: hata_mesaji=None).

    durum YAYINLANDI ise ve yayin_zamani verilmediyse, yayin zamani otomatik
    olarak 'simdi' yazilir (24 saatlik kota sayimi bu kolondan yapilir).

    Doner: guncellenen satir var mi (True/False).
    """
    atamalar: list[str] = []
    degerler: list[Any] = []

    if durum is not None:
        yeni_durum = Durum(str(durum))
        atamalar.append("durum = ?")
        degerler.append(str(yeni_durum))
        if yeni_durum is Durum.YAYINLANDI and yayin_zamani is DOKUNMA:
            yayin_zamani = simdi()

    ikili: list[tuple[str, Any]] = [
        ("container_id", container_id),
        ("permalink", permalink),
        ("medya_id", medya_id),
        ("hata_mesaji", hata_mesaji),
    ]
    for kolon, deger in ikili:
        if deger is not DOKUNMA:
            atamalar.append(f"{kolon} = ?")
            degerler.append(deger if deger is None else str(deger))

    zamanlar: list[tuple[str, Any]] = [
        ("container_zamani", container_zamani),
        ("planlanan_zaman", planlanan_zaman),
        ("yayin_zamani", yayin_zamani),
    ]
    for kolon, deger in zamanlar:
        if deger is not DOKUNMA:
            atamalar.append(f"{kolon} = ?")
            degerler.append(_zaman(deger))

    if deneme_artir:
        atamalar.append("deneme_sayisi = deneme_sayisi + 1")
    if deneme_sifirla:
        atamalar.append("deneme_sayisi = 0")

    atamalar.append("guncelleme_zamani = ?")
    degerler.append(zaman_metni(simdi()))
    degerler.append(int(gorev_id))

    with baglanti(db_yolu) as baglan:
        imlec = baglan.execute(
            f"UPDATE gorevler SET {', '.join(atamalar)} WHERE id = ?",
            tuple(degerler),
        )
        return imlec.rowcount > 0


def gorev_sil(db_yolu: VeriTabaniYolu, gorev_id: int) -> bool:
    """Gorevi kuyruktan siler (dosya tasindiktan sonra temizlik icin)."""
    with baglanti(db_yolu) as baglan:
        imlec = baglan.execute("DELETE FROM gorevler WHERE id = ?", (int(gorev_id),))
        return imlec.rowcount > 0


def dosya_yolunu_guncelle(
    db_yolu: VeriTabaniYolu, gorev_id: int, yeni_yol: Path | str
) -> bool:
    """Dosya arsive/hataya tasindiktan sonra kayittaki yolu gunceller."""
    with baglanti(db_yolu) as baglan:
        imlec = baglan.execute(
            "UPDATE gorevler SET dosya_yolu = ?, guncelleme_zamani = ? WHERE id = ?",
            (_yol_metni(yeni_yol), zaman_metni(simdi()), int(gorev_id)),
        )
        return imlec.rowcount > 0


# ---------------------------------------------------------------------------
# Okuma islemleri
# ---------------------------------------------------------------------------


def gorev_getir(db_yolu: VeriTabaniYolu, gorev_id: int) -> Gorev | None:
    """Tek gorevi id ile getirir."""
    with baglanti(db_yolu) as baglan:
        satir = baglan.execute(
            "SELECT * FROM gorevler WHERE id = ?", (int(gorev_id),)
        ).fetchone()
    return Gorev.eslemeden_olustur(satir) if satir else None


def dosyaya_gore_getir(
    db_yolu: VeriTabaniYolu, dosya_yolu: Path | str
) -> Gorev | None:
    """Dosya yolundan goreve ulasir (ayni dosya tekrar dusmus mu kontrolu)."""
    with baglanti(db_yolu) as baglan:
        satir = baglan.execute(
            "SELECT * FROM gorevler WHERE dosya_yolu = ?", (_yol_metni(dosya_yolu),)
        ).fetchone()
    return Gorev.eslemeden_olustur(satir) if satir else None


def bekleyenleri_getir(
    db_yolu: VeriTabaniYolu,
    *,
    durumlar: Iterable[Durum | str] | None = None,
    limit: int | None = None,
) -> list[Gorev]:
    """Kuyrukta bekleyen gorevleri sirali dondurur.

    Varsayilan durumlar: BEKLIYOR, YUKLENIYOR, HAZIRLANIYOR (yani aktif olanlar);
    program cokup yeniden basladiginda yarim kalan isler de gelsin diye.
    Siralama: once planlanan zamani olanlar (zamanina gore), sonra planlanmamislar.
    """
    secilen = [str(Durum(str(d))) for d in (durumlar or AKTIF_DURUMLAR)]
    yer = ", ".join("?" for _ in secilen)
    sorgu = f"""
        SELECT * FROM gorevler
         WHERE durum IN ({yer})
         ORDER BY CASE WHEN planlanan_zaman IS NULL THEN 1 ELSE 0 END,
                  planlanan_zaman ASC,
                  olusturma_zamani ASC,
                  id ASC
    """
    degerler: list[Any] = list(secilen)
    if limit is not None:
        sorgu += " LIMIT ?"
        degerler.append(int(limit))
    with baglanti(db_yolu) as baglan:
        satirlar = baglan.execute(sorgu, tuple(degerler)).fetchall()
    return [Gorev.eslemeden_olustur(satir) for satir in satirlar]


def siradaki_gorevi_al(
    db_yolu: VeriTabaniYolu,
    *,
    an: datetime | None = None,
    azami_deneme: int | None = None,
    durumlar: Iterable[Durum | str] | None = None,
    sahiplen: bool = False,
    sahiplenme_durumu: Durum = Durum.YUKLENIYOR,
) -> Gorev | None:
    """Yayinlanmaya hazir SIRADAKI gorevi dondurur (yoksa None).

    Kosullar:
      - durum aktif (varsayilan: BEKLIYOR, YUKLENIYOR, HAZIRLANIYOR)
      - planlanan_zaman bos VEYA gecmis
      - deneme_sayisi < azami_deneme (verilmisse)

    sahiplen=True verilirse gorev AYNI SQL ifadesinde 'sahiplenme_durumu'na
    cekilir (sadece BEKLIYOR durumundakiler icin); boylece iki islem ayni
    gorevi ayni anda alamaz. Yarim kalmis (YUKLENIYOR/HAZIRLANIYOR) gorevlerin
    durumu korunur, ustune yazilmaz.
    """
    su_an = zaman_metni(an or simdi())
    secilen = [str(Durum(str(d))) for d in (durumlar or AKTIF_DURUMLAR)]
    yer = ", ".join("?" for _ in secilen)
    deneme_kosulu = "AND deneme_sayisi < ?" if azami_deneme is not None else ""

    ic_sorgu = f"""
        SELECT id FROM gorevler
         WHERE durum IN ({yer})
           AND (planlanan_zaman IS NULL OR planlanan_zaman <= ?)
           {deneme_kosulu}
         ORDER BY CASE WHEN planlanan_zaman IS NULL THEN 1 ELSE 0 END,
                  planlanan_zaman ASC,
                  olusturma_zamani ASC,
                  id ASC
         LIMIT 1
    """
    ic_degerler: list[Any] = [*secilen, su_an]
    if azami_deneme is not None:
        ic_degerler.append(int(azami_deneme))

    with baglanti(db_yolu) as baglan:
        if not sahiplen:
            satir = baglan.execute(
                f"SELECT * FROM gorevler WHERE id = ({ic_sorgu})", tuple(ic_degerler)
            ).fetchone()
            return Gorev.eslemeden_olustur(satir) if satir else None

        # Tek ifade = atomik sahiplenme (SQLite RETURNING, 3.35+)
        satir = baglan.execute(
            f"""
            UPDATE gorevler
               SET durum = CASE WHEN durum = ? THEN ? ELSE durum END,
                   guncelleme_zamani = ?
             WHERE id = ({ic_sorgu})
            RETURNING *
            """,
            (
                str(Durum.BEKLIYOR),
                str(Durum(sahiplenme_durumu)),
                zaman_metni(simdi()),
                *ic_degerler,
            ),
        ).fetchone()
        return Gorev.eslemeden_olustur(satir) if satir else None


def son_24_saatte_yayinlanan_sayisi(
    db_yolu: VeriTabaniYolu, an: datetime | None = None
) -> int:
    """Kayan 24 saatlik pencerede basariyla yayinlanan gorev sayisi.

    Meta'nin kotasi da kayan penceredir (gece yarisi sifirlanmaz). Bu sayac
    YEREL kayittir; kesin kontrol icin yayin oncesi Meta'nin
    /content_publishing_limit ucu de sorgulanmalidir.
    """
    su_an = an or simdi()
    esik = zaman_metni(su_an - timedelta(hours=KOTA_PENCERESI_SAAT))
    with baglanti(db_yolu) as baglan:
        satir = baglan.execute(
            """
            SELECT COUNT(*) AS adet FROM gorevler
             WHERE durum = ?
               AND yayin_zamani IS NOT NULL
               AND yayin_zamani >= ?
            """,
            (str(Durum.YAYINLANDI), esik),
        ).fetchone()
    return int(satir["adet"] if satir else 0)


def gorevleri_listele(
    db_yolu: VeriTabaniYolu,
    *,
    durum: Durum | str | None = None,
    limit: int = 100,
) -> list[Gorev]:
    """Raporlama/CLI icin gorev listesi (en yeni kayit basta)."""
    sorgu = "SELECT * FROM gorevler"
    degerler: list[Any] = []
    if durum is not None:
        sorgu += " WHERE durum = ?"
        degerler.append(str(Durum(str(durum))))
    sorgu += " ORDER BY id DESC LIMIT ?"
    degerler.append(int(limit))
    with baglanti(db_yolu) as baglan:
        satirlar = baglan.execute(sorgu, tuple(degerler)).fetchall()
    return [Gorev.eslemeden_olustur(satir) for satir in satirlar]


def kuyruk_ozeti(db_yolu: VeriTabaniYolu) -> dict[str, int]:
    """Durum -> adet sozlugu (butun durumlar, bos olanlar 0 ile birlikte)."""
    ozet: dict[str, int] = {str(d): 0 for d in Durum}
    with baglanti(db_yolu) as baglan:
        for satir in baglan.execute(
            "SELECT durum, COUNT(*) AS adet FROM gorevler GROUP BY durum"
        ):
            ozet[str(satir["durum"])] = int(satir["adet"])
    return ozet


__all__ = [
    "DOKUNMA",
    "SEMA",
    "VeriTabaniYolu",
    "baglanti",
    "bekleyenleri_getir",
    "dosya_yolunu_guncelle",
    "dosyaya_gore_getir",
    "durum_guncelle",
    "gorev_getir",
    "gorev_sil",
    "gorevleri_listele",
    "kuyruga_ekle",
    "kuyruk_ozeti",
    "siradaki_gorevi_al",
    "son_24_saatte_yayinlanan_sayisi",
    "veritabanini_hazirla",
]
