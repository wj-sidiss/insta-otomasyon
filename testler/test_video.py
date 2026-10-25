# -*- coding: utf-8 -*-
"""Reels video birlestirme: sure kurallari, ffmpeg komutu, HTML kacisi, gercek ffmpeg akisi ve panel uc noktalari."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.icerik import video
from src.icerik.anahtarlar import AnahtarDeposu
from src.icerik.panel import uygulama_olustur
from src.icerik.paket import paketi_oku
from src.icerik.stok import Onbellek
from src.icerik.video import (
    SESLER, VideoHatasi, kart_html, medya_yolu, sahne_komutu, sahne_suresi, video_oku, video_olustur, yazi_katmani_html,
)

PAKET_ID = "20261026-120000-test"
FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
ffmpeg_gerekli = pytest.mark.skipif(not FFMPEG, reason="ffmpeg yok")
ZEHIR = '<script>alert(1)</script>"><img src=x onerror=alert(2)> &amp;'


def sahneler(n=3):
    return [{"sure_sn": 3, "anlatim": f"Anlatim {i}.", "ekran_yazisi": f"Yazi {i}", "gorsel_aramasi": "x", "gorsel_istemi": "y"} for i in range(1, n + 1)]


def paket_yaz(klasor: Path, senaryo=None, medya=None) -> dict:
    hedef = klasor / PAKET_ID
    (hedef / "medya").mkdir(parents=True)
    paket = {"id": PAKET_ID, "konu": "k", "dil": "tr", "basliklar": ["b"], "senaryo": sahneler() if senaryo is None else senaryo, "medya": medya or {}}
    (hedef / "paket.json").write_text(json.dumps(paket, ensure_ascii=False), encoding="utf-8")
    return paket


def sahte_ses(metin, hedef, ses):
    """Gercek ffmpeg ile 1.2 sn'lik sinus mp3 uretir (agsiz test sesi)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2", str(hedef)], check=True)
    return hedef


def sahte_ciz(isler):
    from PIL import Image
    for _kaynak, hedef, seffaf in isler:
        Image.new("RGBA" if seffaf else "RGB", (video.GENISLIK, video.YUKSEKLIK), (0, 0, 0, 0) if seffaf else (20, 30, 60)).save(hedef)


def medya_uret(klasor: Path, ad: str, tur: str):
    yol = klasor / PAKET_ID / "medya" / ad
    if tur == "jpg":
        from PIL import Image
        Image.new("RGB", (1600, 900), (200, 80, 40)).save(yol)          # yatay: dikeye kirpilmali
    else:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24:duration=1", "-pix_fmt", "yuv420p", str(yol)], check=True)
    return yol


def boyut(yol: Path):
    cikti = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name", "-of", "csv=p=0", str(yol)],
                           capture_output=True, text=True, check=True).stdout.strip()
    return cikti


# ============================================================ saf mantik

@pytest.mark.parametrize("ses_sn,taban,beklenen", [
    (None, 5, 5.0), (None, 0, 4.0), (None, 1, 2.0), (None, 99, 20.0),            # ses yok: senaryo suresi, 2-20 arasi
    (3.0, 9, 3.45), (0.2, 3, 2.0), (30.0, 3, 20.0),                              # ses varsa anlatim belirler
])
def test_sahne_suresi(ses_sn, taban, beklenen):
    assert sahne_suresi({"sure_sn": taban}, ses_sn) == beklenen


def test_kart_ve_katman_html_model_ciktisini_kacirir():
    for h in (kart_html({"ekran_yazisi": ZEHIR}, 1, 3, imza=ZEHIR), yazi_katmani_html({"ekran_yazisi": ZEHIR})):
        assert "<script" not in h and "<img" not in h and "&lt;script&gt;" in h
    assert "A<br>B" in kart_html({"ekran_yazisi": "A\nB"}, 1, 1)
    assert "class=y" not in yazi_katmani_html({"ekran_yazisi": ""})                # yazi yoksa bos katman
    assert "anlatim metni" in kart_html({"ekran_yazisi": "", "anlatim": "anlatim metni"}, 1, 1)         # yazi yoksa anlatim


def test_medya_yolu_deseni_ve_varlik(tmp_path):
    paket = paket_yaz(tmp_path)
    medya_uret(tmp_path, "01-pexels-abc123.jpg", "jpg") if FFMPEG else (tmp_path / PAKET_ID / "medya" / "01-pexels-abc123.jpg").write_bytes(b"x")
    paket["medya"] = {"1": {"dosya": "01-pexels-abc123.jpg"}, "2": {"dosya": "../../paket.json"}, "3": {"dosya": "yok-yok.jpg"},
                      "4": {"dosya": "04-pexels-abc.jpg"}, "5": "metin", "6": {"dosya": 5}, "7": None}
    assert medya_yolu(tmp_path, paket, 1).name == "01-pexels-abc123.jpg"
    for no in (2, 3, 4, 5, 6, 7, 8):
        assert medya_yolu(tmp_path, paket, no) is None, no


def test_ffmpeg_komutu_kabuksuz_ve_filtreye_yol_girmez():
    yol = Path("C:/kullanici/ad; rm -rf x/01-pexels-abc.jpg")
    k = sahne_komutu(yol, Path("kart.png"), Path("yazi.png"), Path("ses.mp3"), 4.5, Path("cikti.mp4"))
    assert all(isinstance(a, str) for a in k)
    grafik = k[k.index("-filter_complex") + 1]
    assert "rm -rf" not in grafik and "kullanici" not in grafik and "01-pexels" not in grafik
    assert "zoompan" in grafik and "overlay" in grafik and "d=135" in grafik and "1080x1920" in grafik
    assert k[-1] == "cikti.mp4" and "-t" in k and "4.5" in k
    v = sahne_komutu(Path("klip.mp4"), Path("kart.png"), None, None, 3.0, Path("c.mp4"))
    g = v[v.index("-filter_complex") + 1]
    assert "-stream_loop" in v and "crop=1080:1920" in g and "zoompan" not in g and "overlay" not in g and "anullsrc" in " ".join(v)
    kart = sahne_komutu(None, Path("kart.png"), None, None, 3.0, Path("c.mp4"))
    assert "kart.png" in kart and "-stream_loop" not in kart


def test_gecersiz_girdiler(tmp_path):
    paket = paket_yaz(tmp_path)
    with pytest.raises(ValueError):
        video_olustur({**paket, "id": "../x"}, klasor=tmp_path)
    with pytest.raises(ValueError):
        video_olustur(paket, klasor=tmp_path, ses="tr-TR-Bilinmeyen")
    with pytest.raises(VideoHatasi, match="boş"):
        video_olustur({**paket, "senaryo": []}, klasor=tmp_path)
    with pytest.raises(VideoHatasi, match="boş"):
        video_olustur({**paket, "senaryo": [{"anlatim": "", "ekran_yazisi": ""}]}, klasor=tmp_path)


def test_ffmpeg_yoksa_anlasilir_hata(tmp_path, monkeypatch):
    monkeypatch.setattr(video.shutil, "which", lambda ad: None)
    with pytest.raises(VideoHatasi, match="ffmpeg bulunamadı"):
        video_olustur(paket_yaz(tmp_path), klasor=tmp_path)


def test_edge_tts_yoksa_none_doner(monkeypatch, tmp_path):
    import builtins
    gercek = builtins.__import__

    def engelle(ad, *a, **k):
        if ad == "edge_tts":
            raise ImportError
        return gercek(ad, *a, **k)
    monkeypatch.setattr(builtins, "__import__", engelle)
    assert video.edge_tts_ile_seslendir("merhaba", tmp_path / "s.mp3", "tr-TR-EmelNeural") is None


def test_edge_tts_ses_donmezse_diger_sesi_dener(monkeypatch, tmp_path):
    import types
    deneyen = []

    class Iletisim:
        def __init__(self, metin, ses):
            self.ses = ses

        async def save(self, yol):
            deneyen.append(self.ses)
            if self.ses == "tr-TR-EmelNeural":
                raise RuntimeError("NoAudioReceived")
            Path(yol).write_bytes(b"x" * 2000)

    monkeypatch.setitem(__import__("sys").modules, "edge_tts", types.SimpleNamespace(Communicate=Iletisim))
    sonuc = video.edge_tts_ile_seslendir("merhaba", tmp_path / "s.mp3", "tr-TR-EmelNeural")
    assert sonuc and deneyen == ["tr-TR-EmelNeural", "tr-TR-EmelNeural", "tr-TR-AhmetNeural"]


def test_video_oku_bozuk_ya_da_eksik(tmp_path):
    assert video_oku(tmp_path, "../x") is None and video_oku(tmp_path, PAKET_ID) is None
    (tmp_path / PAKET_ID / "video").mkdir(parents=True)
    (tmp_path / PAKET_ID / "video" / "video.json").write_text("{bozuk", encoding="utf-8")
    assert video_oku(tmp_path, PAKET_ID) is None
    (tmp_path / PAKET_ID / "video" / "video.json").write_text("{}", encoding="utf-8")
    assert video_oku(tmp_path, PAKET_ID) is None                                 # mp4 yok


# ============================================================ gercek ffmpeg

@ffmpeg_gerekli
def test_gercek_akis_karma_medya_ve_kartla_1080x1920_mp4(tmp_path):
    paket = paket_yaz(tmp_path, medya={"1": {"dosya": "01-pexels-aaa111.jpg"}, "2": {"dosya": "02-pexels-bbb222.mp4"}})
    medya_uret(tmp_path, "01-pexels-aaa111.jpg", "jpg")
    medya_uret(tmp_path, "02-pexels-bbb222.mp4", "mp4")
    adimlar = []
    s = video_olustur(paket, klasor=tmp_path, imza="@x", seslendir=sahte_ses, ciz=sahte_ciz, ilerleme=lambda a, m: adimlar.append(a))
    mp4 = tmp_path / PAKET_ID / "video" / "reels.mp4"
    assert boyut(mp4).startswith("h264,1080,1920")
    assert s["sahne_sayisi"] == 3 and s["medyali_sahne"] == 2 and s["sure_sn"] == 6.0          # 1.2+0.45=1.65 sn, 2 sn tabanina yukselir: 3 x 2
    assert sum("düz kart" in u for u in s["uyarilar"]) == 1 and "3" in s["uyarilar"][0]
    assert {"ses", "gorsel", "sahne", "birlestir", "kayit"} <= set(adimlar)
    assert abs(video.sure_olc(mp4) - s["sure_sn"]) < 0.5
    sesli = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,channels", "-of", "csv=p=0", str(mp4)],
                           capture_output=True, text=True).stdout.strip()
    assert sesli == "aac,2"
    assert video_oku(tmp_path, PAKET_ID)["dosya"] == "reels.mp4" and paketi_oku(tmp_path, PAKET_ID)["video"]["sahne_sayisi"] == 3
    assert not list((tmp_path / PAKET_ID / "video").glob("*.tmp*"))


@ffmpeg_gerekli
def test_gercek_akis_sessiz_secenegi_servise_hicbir_sey_gondermez(tmp_path):
    cagrilar = []
    s = video_olustur(paket_yaz(tmp_path), klasor=tmp_path, ses="yok", seslendir=lambda *a: cagrilar.append(a), ciz=sahte_ciz)
    assert cagrilar == [] and s["ses"] == "yok" and s["sure_sn"] == 9.0                            # 3 x senaryo suresi 3 sn


@ffmpeg_gerekli
def test_ses_uretilemezse_sahne_sessiz_devam_eder_ve_uyari_verir(tmp_path):
    s = video_olustur(paket_yaz(tmp_path), klasor=tmp_path, seslendir=lambda *a: None, ciz=sahte_ciz)
    assert sum("ses üretilemedi" in u for u in s["uyarilar"]) == 3 and s["sure_sn"] == 9.0


@ffmpeg_gerekli
def test_bozuk_medya_dosyasi_anlasilir_hata_verir_eski_video_bozulmaz(tmp_path):
    paket = paket_yaz(tmp_path, medya={"1": {"dosya": "01-pexels-aaa111.mp4"}})
    video_olustur(paket, klasor=tmp_path, ses="yok", ciz=sahte_ciz)
    once = (tmp_path / PAKET_ID / "video" / "reels.mp4").read_bytes()
    (tmp_path / PAKET_ID / "medya" / "01-pexels-aaa111.mp4").write_bytes(b"bozuk veri")
    with pytest.raises(VideoHatasi, match="ffmpeg hatası"):
        video_olustur(paket, klasor=tmp_path, ses="yok", ciz=sahte_ciz)
    assert (tmp_path / PAKET_ID / "video" / "reels.mp4").read_bytes() == once


@ffmpeg_gerekli
def test_gercek_tarayici_katmani_ve_karti_1080x1920_cizer(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    kart, katman = tmp_path / "k.png", tmp_path / "y.png"
    try:
        video.gorselleri_ciz([(kart_html({"ekran_yazisi": ZEHIR * 2}, 1, 1, "@x"), kart, False), (yazi_katmani_html({"ekran_yazisi": "Merhaba dünya"}), katman, True)])
    except VideoHatasi as hata:
        pytest.skip(f"chromium yok: {hata}")
    assert PIL.open(kart).size == (1080, 1920) and PIL.open(katman).size == (1080, 1920)
    assert PIL.open(katman).convert("RGBA").getpixel((5, 5))[3] == 0              # katman seffaf


# ============================================================ panel

@pytest.fixture
def panel(tmp_path):
    klasor = tmp_path / "paketler"
    klasor.mkdir()
    paket_yaz(klasor)
    app = uygulama_olustur(
        paket_klasoru=klasor, saglayicilar=lambda: [], saglayici_bulucu=lambda ad: None, izinli_hostlar=("testserver",), kapak_ciz=False,
        anahtar_depo=AnahtarDeposu(tmp_path / "k.json", ortam={}), stok_onbellek=Onbellek(), video_seslendir=sahte_ses, video_gorsel_ciz=sahte_ciz, imza="@wj",
    )
    c = TestClient(app)
    c.klasor = klasor
    return c


def _bekle(c, is_id):
    bitis = time.time() + 60
    while time.time() < bitis:
        d = c.get(f"/api/is/{is_id}").json()
        if d["durum"] != "calisiyor":
            return d
        time.sleep(0.1)
    raise AssertionError("is bitmedi")


def test_panel_sesler_listesi(panel):
    d = panel.get("/api/sesler").json()
    assert {s["ad"] for s in d["sesler"]} == set(SESLER) and d["varsayilan"] == "tr-TR-EmelNeural"


@pytest.mark.parametrize("govde", [{"ses": "baska-ses"}, {"ses": "x" * 60}, {"ses": 5}])
def test_panel_gecersiz_ses_422(panel, govde):
    assert panel.post(f"/api/paket/{PAKET_ID}/video", json=govde).status_code == 422


def test_panel_olmayan_paket_ve_olmayan_video_404(panel):
    assert panel.post("/api/paket/20261026-999999-yok/video", json={}).status_code == 404
    assert panel.get(f"/api/paket/{PAKET_ID}/video").status_code == 404
    assert panel.get(f"/paketler/{PAKET_ID}/video/reels.mp4").status_code == 404
    assert panel.get("/paketler/..%2Fx/video/reels.mp4").status_code in (400, 404)


@ffmpeg_gerekli
def test_panel_uctan_uca_video_ve_dosya_sunumu(panel):
    r = panel.post(f"/api/paket/{PAKET_ID}/video", json={})
    assert r.status_code == 202
    d = _bekle(panel, r.json()["is_id"])
    assert d["durum"] == "bitti", d
    v = panel.get(f"/api/paket/{PAKET_ID}/video").json()
    assert v["sahne_sayisi"] == 3 and v["dosya"] == "reels.mp4"
    assert panel.get(f"/api/paket/{PAKET_ID}").json()["video"]["sahne_sayisi"] == 3
    m = panel.get(f"/paketler/{PAKET_ID}/video/reels.mp4")
    assert m.status_code == 200 and m.headers["content-type"] == "video/mp4" and m.content[4:8] == b"ftyp"
    parca = panel.get(f"/paketler/{PAKET_ID}/video/reels.mp4", headers={"Range": "bytes=0-99"})
    assert parca.status_code == 206 and len(parca.content) == 100                                 # tarayici oynaticisi icin aralik destegi


@ffmpeg_gerekli
def test_panel_ikinci_uretim_409_ve_yabanci_site_403(panel):
    r = panel.post(f"/api/paket/{PAKET_ID}/video", json={"ses": "yok"})
    assert r.status_code == 202
    assert panel.post(f"/api/paket/{PAKET_ID}/video", json={}).status_code in (202, 409)          # ilk is bitmis olabilir
    _bekle(panel, r.json()["is_id"])
    assert panel.post(f"/api/paket/{PAKET_ID}/video", json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert panel.post(f"/api/paket/{PAKET_ID}/video", json={}, headers={"Origin": "https://kotu.example"}).status_code == 403
    assert panel.get(f"/paketler/{PAKET_ID}/video/reels.mp4", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_panel_ffmpeg_yoksa_hata_kullaniciya_ulasir(panel, monkeypatch):
    monkeypatch.setattr(video.shutil, "which", lambda ad: None)
    r = panel.post(f"/api/paket/{PAKET_ID}/video", json={})
    d = _bekle(panel, r.json()["is_id"])
    assert d["durum"] == "hata" and "ffmpeg bulunamadı" in d["hata"]
