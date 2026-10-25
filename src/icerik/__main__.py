# -*- coding: utf-8 -*-
"""`python -m src.icerik` ile paneli baslatir (yalnizca 127.0.0.1)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from .panel import uygulama_olustur


def main() -> int:
    ayristirici = argparse.ArgumentParser(prog="python -m src.icerik", description="Icerik Studyosu paneli")
    ayristirici.add_argument("--port", type=int, default=8000, help="Dinlenecek port (varsayilan 8000)")
    ayristirici.add_argument("--paket-klasoru", default="paketler", help="Paketlerin yazilacagi klasor")
    ayristirici.add_argument("--imza", default="", help="Kapaga yazilacak kullanici adi (istege bagli)")
    secenek = ayristirici.parse_args()

    uygulama = uygulama_olustur(paket_klasoru=Path(secenek.paket_klasoru), imza=secenek.imza)
    print(f"Panel: http://127.0.0.1:{secenek.port}  (kapatmak icin Ctrl+C)")
    try:
        # host SABIT: panel ag disina asla acilmaz.
        uvicorn.run(uygulama, host="127.0.0.1", port=secenek.port, log_level="warning")
    except OSError as hata:
        print(f"Panel baslatilamadi (port {secenek.port} dolu olabilir): {hata}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
