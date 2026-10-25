"""Tek surec garantisi.

Program iki kez baslatilirsa (kisayola iki tik, ikinci terminal) her iki surec
de kuyruktan AYNI gorevi alip ayni videoyu Instagram'a iki kez yayinlayabilir.
Kuyruk sorgusu bunu tek basina engelleyemez: yarim kalmis gorevleri kurtarmak
icin YUKLENIYOR/HAZIRLANIYOR satirlarini da dondurmesi gerekiyor.

Cozum isletim sistemi seviyesinde dosya kilidi. Surec cokerse kilit otomatik
dusuyor, bayat kilit dosyasi temizlemeye gerek kalmiyor.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType


class KilitAlinamadi(RuntimeError):
    """Baska bir kopya zaten calisiyor."""


class SurecKilidi:
    """with blogu icinde tek surec garantisi saglar."""

    def __init__(self, kilit_yolu: Path) -> None:
        self.kilit_yolu = Path(kilit_yolu)
        self._tanitici: int | None = None

    def __enter__(self) -> "SurecKilidi":
        self.kilit_yolu.parent.mkdir(parents=True, exist_ok=True)
        self._tanitici = os.open(
            self.kilit_yolu, os.O_RDWR | os.O_CREAT, 0o644
        )
        try:
            self._kilitle()
        except OSError as hata:
            os.close(self._tanitici)
            self._tanitici = None
            raise KilitAlinamadi(
                "Otomasyonun baska bir kopyasi zaten calisiyor. "
                "Iki kopya ayni videoyu iki kez yayinlayabilir, bu yuzden "
                f"baslatilmadi. Kilit dosyasi: {self.kilit_yolu}"
            ) from hata

        os.truncate(self._tanitici, 0)
        os.write(self._tanitici, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(
        self,
        tur: type[BaseException] | None,
        deger: BaseException | None,
        iz: TracebackType | None,
    ) -> None:
        if self._tanitici is None:
            return
        try:
            self._kilidi_birak()
        finally:
            os.close(self._tanitici)
            self._tanitici = None
            try:
                self.kilit_yolu.unlink()
            except OSError:
                pass  # baska kopya devralmis olabilir, sorun degil

    # --- platforma ozel ---

    def _kilitle(self) -> None:
        assert self._tanitici is not None
        if sys.platform == "win32":
            import msvcrt

            os.lseek(self._tanitici, 0, os.SEEK_SET)
            msvcrt.locking(self._tanitici, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._tanitici, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _kilidi_birak(self) -> None:
        assert self._tanitici is not None
        if sys.platform == "win32":
            import msvcrt

            os.lseek(self._tanitici, 0, os.SEEK_SET)
            msvcrt.locking(self._tanitici, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._tanitici, fcntl.LOCK_UN)
