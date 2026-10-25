"""Tek surec kilidi gercekten iki kopyayi engelliyor mu?"""
import subprocess
import sys
import textwrap

from src.surec_kilidi import KilitAlinamadi, SurecKilidi


def test_ayni_surecte_ikinci_kilit_reddedilir(tmp_path):
    yol = tmp_path / "kuyruk.lock"
    with SurecKilidi(yol):
        try:
            with SurecKilidi(yol):
                raise AssertionError("ikinci kilit alinmamaliydi")
        except KilitAlinamadi:
            pass


def test_ayri_surecte_ikinci_kopya_reddedilir(tmp_path):
    yol = tmp_path / "kuyruk.lock"
    betik = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(tmp_path.parent.parent)!r})
        from src.surec_kilidi import KilitAlinamadi, SurecKilidi
        try:
            with SurecKilidi({str(yol)!r}):
                print("ALDI")
        except KilitAlinamadi:
            print("REDDEDILDI")
    """)
    with SurecKilidi(yol):
        sonuc = subprocess.run(
            [sys.executable, "-c", betik], capture_output=True, text=True, cwd="."
        )
    assert "REDDEDILDI" in sonuc.stdout, f"stdout={sonuc.stdout!r} stderr={sonuc.stderr!r}"


def test_kilit_birakilinca_tekrar_alinabilir(tmp_path):
    yol = tmp_path / "kuyruk.lock"
    with SurecKilidi(yol):
        pass
    with SurecKilidi(yol):
        pass  # ikinci kez sorunsuz alinmali
