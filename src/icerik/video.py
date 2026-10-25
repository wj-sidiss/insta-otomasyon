# -*- coding: utf-8 -*-
"""Reels videosu birlestirme: senaryo + Turkce ses + secilen stok medya + ekran yazisi -> 1080x1920 (9:16) mp4.

Tamamen yerelde calisir (ffmpeg). Disariya giden tek sey, sesli anlatim istenirse anlatim metninin edge-tts uzerinden
Microsoft'un okuma servisine gonderilmesidir (anahtarsiz, resmi olmayan servis); ses "yok" secilirse hicbir sey gonderilmez.
Sahnede medya secilmediyse sahne, ekran yazisini tasiyan duz bir kart olarak cizilir (video yine tamamlanir).
Model ciktisi guvenilmezdir: metinler HTML'e basilmadan once kacirilir; ffmpeg'e giden her sey liste argumani olarak verilir
(kabuk yok) ve filtre grafigine dosya yolu girmez.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

from .paket import _PAKET_KILIDI, MEDYA_DOSYA_DESENI, _paketi_yaz, gecerli_id_mi, paketi_oku

GENISLIK, YUKSEKLIK, FPS = 1080, 1920, 30
EN_KISA_SAHNE, EN_UZUN_SAHNE = 2.0, 20.0
SES_SONRASI_PAYI = 0.45                  # anlatim bittikten sonra nefes payi
UZUN_VIDEO_UYARISI_SN = 90
VIDEO_DOSYASI = "reels.mp4"
SESLER = {"tr-TR-EmelNeural": "Emel (kadın)", "tr-TR-AhmetNeural": "Ahmet (erkek)", "yok": "Sessiz (ses gönderilmez)"}
VARSAYILAN_SES = "tr-TR-EmelNeural"
ILERLEME = Callable[[str, str], None]


class VideoHatasi(RuntimeError):
    """Kullaniciya oldugu gibi gosterilebilen video hatasi."""


# ---------------------------------------------------------------- araclar

def ffmpeg_yolu() -> str:
    yol = shutil.which("ffmpeg")
    if not yol or not shutil.which("ffprobe"):
        raise VideoHatasi("ffmpeg bulunamadı. Kurun (Windows: `winget install Gyan.FFmpeg`) ve terminali yeniden açın.")
    return yol


def _calistir(komut: list[str], zaman_asimi: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(komut, capture_output=True, timeout=zaman_asimi, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as hata:
        raise VideoHatasi("ffmpeg zaman aşımına uğradı.") from hata


def _ffmpeg(args: list[str]) -> None:
    sonuc = _calistir(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args])
    if sonuc.returncode != 0:                                            # bellek baskisinda x264 ara sira anlik hata verir: bir kez daha dene
        sonuc = _calistir(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args])
    if sonuc.returncode != 0:
        iz = sonuc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise VideoHatasi("ffmpeg hatası: " + " | ".join(iz[-3:])[:400])


def sure_olc(yol: Path) -> float:
    sonuc = _calistir(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(yol)], 60)
    try:
        sure = float(sonuc.stdout.decode().strip())
    except ValueError as hata:
        raise VideoHatasi(f"Süre okunamadı: {Path(yol).name}") from hata
    if not 0 < sure < 3600:
        raise VideoHatasi(f"Geçersiz süre: {Path(yol).name}")
    return sure


# ---------------------------------------------------------------- ses

def edge_tts_ile_seslendir(metin: str, hedef: Path, ses: str) -> Path | None:
    """Metni mp3'e cevirir. edge-tts yoksa ya da servis ses donmezse None (cagiran sessiz devam eder)."""
    try:
        import edge_tts
    except ImportError:
        return None
    adaylar = [ses] + [s for s in SESLER if s not in (ses, "yok")]            # sesten ses gelmezse digeri
    for aday in adaylar:
        for _ in range(2):
            try:
                asyncio.run(edge_tts.Communicate(metin, aday).save(str(hedef)))
                if hedef.is_file() and hedef.stat().st_size > 1000:
                    return hedef
            except Exception:
                continue
    return None


def sahne_suresi(sahne: dict, ses_suresi: float | None) -> float:
    """Sahne suresi: anlatim varsa onun uzunlugu (+pay), yoksa senaryodaki sure; 2-20 sn arasi."""
    taban = float(sahne.get("sure_sn") or 0)
    sure = (ses_suresi + SES_SONRASI_PAYI) if ses_suresi else (taban or 4.0)
    return round(min(max(sure, EN_KISA_SAHNE), EN_UZUN_SAHNE), 2)


# ---------------------------------------------------------------- gorseller (kart + yazi katmani)

_ORTAK = ("*{box-sizing:border-box;margin:0}html,body{width:%dpx;height:%dpx}"
          "body{font-family:'Segoe UI','Helvetica Neue',Arial,sans-serif;color:#fff;overflow:hidden}" % (GENISLIK, YUKSEKLIK))


def _boyut(yazi: str) -> int:
    n = len(yazi)
    return 96 if n <= 24 else 78 if n <= 48 else 62 if n <= 80 else 50


def kart_html(sahne: dict, no: int, toplam: int, imza: str = "") -> str:
    """Medyasi olmayan sahne: duz arka plan + buyuk ekran yazisi. Tum alanlar kacirilir."""
    yazi = sahne.get("ekran_yazisi") or sahne.get("anlatim") or ""
    kacir = lambda s: html.escape(str(s), quote=True).replace("\n", "<br>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{_ORTAK}"
            "body{background:linear-gradient(160deg,#101826 0%,#1b2a4a 55%,#0d1220 100%);display:flex;align-items:center;justify-content:center}"
            ".k{padding:0 90px;text-align:center;font-weight:800;line-height:1.18;letter-spacing:-.5px}"
            ".a{position:absolute;left:0;right:0;bottom:330px;text-align:center;font-size:30px;opacity:.55;letter-spacing:2px}"
            f"</style></head><body><div class='k' style='font-size:{_boyut(yazi)}px'>{kacir(yazi)}</div>"
            f"<div class='a'>{kacir(imza)}</div></body></html>")


def yazi_katmani_html(sahne: dict) -> str:
    """Medyanin ustune binen seffaf ekran yazisi. Reels arayuzunun kapattigi alt/yan bolgeler bos birakilir."""
    yazi = sahne.get("ekran_yazisi") or ""
    kacir = html.escape(yazi, quote=True).replace("\n", "<br>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{_ORTAK}"
            "html,body{background:transparent}"
            "body{display:flex;align-items:flex-end;justify-content:center;padding-bottom:520px}"
            ".y{max-width:880px;padding:26px 40px;border-radius:26px;background:rgba(10,14,24,.68);text-align:center;"
            f"font-weight:800;line-height:1.2;font-size:{min(_boyut(yazi), 68)}px;text-shadow:0 2px 8px rgba(0,0,0,.5)}}"
            f"</style></head><body>{f'<div class=y>{kacir}</div>' if yazi else ''}</body></html>")


def gorselleri_ciz(isler: list[tuple[str, Path, bool]], *, genislik: int = GENISLIK, yukseklik: int = YUKSEKLIK) -> None:
    """(html, hedef_png, seffaf_mi) listesini tek tarayici oturumunda PNG'ye cizer."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as hata:
        raise VideoHatasi("Video için Playwright gerekli: `pip install playwright` ve `python -m playwright install chromium`.") from hata
    try:
        with sync_playwright() as oynatici:
            tarayici = oynatici.chromium.launch()
            try:
                sayfa = tarayici.new_page(viewport={"width": genislik, "height": yukseklik})
                for kaynak, hedef, seffaf in isler:
                    sayfa.set_content(kaynak, wait_until="load")
                    sayfa.screenshot(path=str(hedef), type="png", omit_background=seffaf)
            finally:
                tarayici.close()
    except Exception as hata:
        raise VideoHatasi(f"Sahne görselleri çizilemedi: {hata}. `python -m playwright install chromium` deneyin.") from hata


# ---------------------------------------------------------------- sahne ve birlestirme

def medya_yolu(klasor: Path, paket: dict, no: int) -> Path | None:
    """Sahneye secilen medya dosyasi; ad deseni ve klasor icinde kalma dogrulanir, yoksa None."""
    kayit = (paket.get("medya") or {}).get(str(no))
    ad = kayit.get("dosya", "") if isinstance(kayit, dict) else ""
    if not isinstance(ad, str) or not MEDYA_DOSYA_DESENI.fullmatch(ad):
        return None
    yol = Path(klasor) / paket["id"] / "medya" / ad
    return yol if yol.is_file() else None


def _video_mu(yol: Path) -> bool:
    return yol.suffix.lower() in (".mp4", ".webm")


def sahne_komutu(medya: Path | None, kart: Path, katman: Path | None, ses: Path | None, sure: float, cikti: Path) -> list[str]:
    """Tek sahneyi 1080x1920/30fps mp4'e ceviren ffmpeg argumanlari. Filtreye yalnizca sayilar girer."""
    kare = max(int(round(sure * FPS)), 1)
    girisler: list[str] = []
    if medya is not None and _video_mu(medya):
        girisler += ["-stream_loop", "-1", "-t", f"{sure}", "-i", str(medya)]
        arka = f"[0:v]scale={GENISLIK}:{YUKSEKLIK}:force_original_aspect_ratio=increase,crop={GENISLIK}:{YUKSEKLIK},setsar=1,fps={FPS}[bg]"
    else:
        girisler += ["-i", str(medya if medya is not None else kart)]
        # yavas yakinlasma (Ken Burns): once ortadan kirp, sonra zoompan; ifadede yalnizca sabit sayilar var
        arka = (f"[0:v]scale={GENISLIK * 12 // 10}:{YUKSEKLIK * 12 // 10}:force_original_aspect_ratio=increase,"
                f"crop={GENISLIK * 12 // 10}:{YUKSEKLIK * 12 // 10},"
                f"zoompan=z='min(1+0.0006*on,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={kare}:s={GENISLIK}x{YUKSEKLIK}:fps={FPS},setsar=1[bg]")
    n = 1
    if katman is not None:
        girisler += ["-loop", "1", "-framerate", str(FPS), "-t", f"{sure}", "-i", str(katman)]
        grafik = f"{arka};[bg][{n}:v]overlay=0:0:format=auto,format=yuv420p[v]"
        n += 1
    else:
        grafik = f"{arka};[bg]format=yuv420p[v]"
    if ses is not None:
        girisler += ["-i", str(ses)]
        ses_filtre = f"[{n}:a]aresample=44100,aformat=channel_layouts=stereo,apad,atrim=0:{sure}[a]"
    else:
        girisler += ["-f", "lavfi", "-t", f"{sure}", "-i", "anullsrc=r=44100:cl=stereo"]
        ses_filtre = f"[{n}:a]atrim=0:{sure}[a]"
    return [*girisler, "-filter_complex", f"{grafik};{ses_filtre}", "-map", "[v]", "-map", "[a]", "-t", f"{sure}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart", str(cikti)]


def video_olustur(
    paket: dict, *, klasor: Path, ses: str = VARSAYILAN_SES, imza: str = "",
    seslendir: Callable[[str, Path, str], Path | None] = edge_tts_ile_seslendir,
    ciz: Callable[[list[tuple[str, Path, bool]]], None] = gorselleri_ciz,
    ilerleme: ILERLEME | None = None,
) -> dict:
    if not gecerli_id_mi(paket.get("id", "")):
        raise ValueError("Geçersiz paket kimliği.")
    if ses not in SESLER:
        raise ValueError("Geçersiz ses.")
    sahneler = [s for s in paket.get("senaryo", []) if s.get("anlatim") or s.get("ekran_yazisi")]
    if not sahneler:
        raise VideoHatasi("Bu paketin senaryosu boş; önce içerik paketi üretin.")
    ffmpeg_yolu()
    bildir = ilerleme or (lambda adim, mesaj: None)
    uyarilar: list[str] = []
    hedef = Path(klasor) / paket["id"] / "video"
    hedef.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="reels-") as gecici:
        t = Path(gecici)
        # 1) ses
        sesler: list[Path | None] = []
        for i, sahne in enumerate(sahneler, 1):
            dosya = None
            if ses != "yok" and sahne.get("anlatim"):
                bildir("ses", f"Ses üretiliyor {i}/{len(sahneler)}…")
                dosya = seslendir(sahne["anlatim"], t / f"ses-{i:02d}.mp3", ses)
                if dosya is None:
                    uyarilar.append(f"Sahne {i}: ses üretilemedi (edge-tts yok ya da servis yanıt vermedi); sahne sessiz.")
            sesler.append(dosya)
        sureler = [sahne_suresi(s, sure_olc(d) if d else None) for s, d in zip(sahneler, sesler)]

        # 2) kart ve yazi katmanlari
        bildir("gorsel", "Sahne görselleri hazırlanıyor…")
        medyalar = [medya_yolu(klasor, paket, i) for i in range(1, len(sahneler) + 1)]
        isler, kartlar, katmanlar = [], {}, {}
        for i, (sahne, medya) in enumerate(zip(sahneler, medyalar), 1):
            if medya is None:
                kartlar[i] = t / f"kart-{i:02d}.png"
                isler.append((kart_html(sahne, i, len(sahneler), imza), kartlar[i], False))
            elif sahne.get("ekran_yazisi"):
                katmanlar[i] = t / f"yazi-{i:02d}.png"
                isler.append((yazi_katmani_html(sahne), katmanlar[i], True))
        if kartlar:
            uyarilar.append(f"{len(kartlar)}/{len(sahneler)} sahnede medya seçilmemiş, düz kart kullanıldı (sahne: {', '.join(map(str, kartlar))}).")
        if isler:
            ciz(isler)

        # 3) sahne klipleri
        parcalar = []
        for i, (medya, sure) in enumerate(zip(medyalar, sureler), 1):
            bildir("sahne", f"Sahne {i}/{len(sahneler)} işleniyor…")
            cikti = t / f"sahne-{i:02d}.mp4"
            _ffmpeg(sahne_komutu(medya, kartlar.get(i, t / "yok.png"), katmanlar.get(i), sesler[i - 1], sure, cikti))
            parcalar.append(cikti)

        # 4) birlestir + ses seviyesi
        bildir("birlestir", "Sahneler birleştiriliyor…")
        liste = t / "liste.txt"
        liste.write_text("".join(f"file '{p.as_posix()}'\n" for p in parcalar), encoding="utf-8")
        gecici_cikti = hedef / (VIDEO_DOSYASI + ".tmp.mp4")
        _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(liste), "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                 "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", str(gecici_cikti)])
        os.replace(gecici_cikti, hedef / VIDEO_DOSYASI)

    toplam = round(sum(sureler), 1)
    if toplam > UZUN_VIDEO_UYARISI_SN:
        uyarilar.append(f"Video {toplam} sn: Reels için {UZUN_VIDEO_UYARISI_SN} sn altı daha iyi izlenir.")
    sonuc = {"dosya": VIDEO_DOSYASI, "sure_sn": toplam, "sahne_sayisi": len(sahneler), "ses": ses, "uyarilar": uyarilar,
             "olusturma": datetime.now().astimezone().isoformat(timespec="seconds"),
             "medyali_sahne": sum(1 for m in medyalar if m is not None)}
    with _PAKET_KILIDI:
        tam = paketi_oku(klasor, paket["id"])
        if tam is not None:
            tam["video"] = {k: sonuc[k] for k in ("sure_sn", "sahne_sayisi", "olusturma")}
            _paketi_yaz(Path(klasor) / paket["id"] / "paket.json", tam)
    kayit = hedef / "video.json"
    gecici_kayit = kayit.with_suffix(".tmp")
    gecici_kayit.write_text(json.dumps(sonuc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(gecici_kayit, kayit)
    bildir("kayit", "Video hazır.")
    return sonuc


def video_oku(klasor: Path, paket_id: str) -> dict | None:
    if not gecerli_id_mi(paket_id):
        return None
    try:
        veri = json.loads((Path(klasor) / paket_id / "video" / "video.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return veri if isinstance(veri, dict) and (Path(klasor) / paket_id / "video" / VIDEO_DOSYASI).is_file() else None
