"""Build the standalone Windows exe into dist/.

    python scripts/build_exe.py                     dist/Rivals-2-Palette-Creator-v<version>.exe
    python scripts/build_exe.py --include-renders   also zip the exe with the render tree
    python scripts/build_exe.py --no-venv           build with this interpreter (CI)
    python scripts/build_exe.py --clean             rebuild the build venv and caches first

Steps: make a private venv (.venv-build) holding only requirements.txt plus
PyInstaller, run palette_creator.spec, then start the exe with --smoke-test and
fail the build unless it comes up cleanly.

The venv matters. PyInstaller bundles whatever the interpreter can import, so
building from a global Python drags unrelated packages into the exe.

The exe keeps its data in a Resources folder next to itself; renders come from
the Downloads tab, or from the --include-renders zip.
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv-build"
SPEC = ROOT / "palette_creator.spec"
WORK = ROOT / "build" / "pyinstaller"
RENDERS = Path("Resources/Character_Renders_Source")


def read_version() -> str:
    text = (ROOT / "palette_creator" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise SystemExit("ERROR: no __version__ found in palette_creator/__init__.py")
    return match.group(1)


def run(cmd: list, **kwargs) -> None:
    print("  > " + " ".join(str(c) for c in cmd), flush=True)
    result = subprocess.run([str(c) for c in cmd], cwd=ROOT, **kwargs)
    if result.returncode:
        raise SystemExit(f"ERROR: command failed with exit code {result.returncode}")


def human(size: float) -> str:
    return f"{size / 1024 / 1024:,.1f} MB"


def build_python(use_venv: bool, clean: bool) -> Path:
    """Return the interpreter to build with, creating the venv if needed."""
    if not use_venv:
        return Path(sys.executable)
    if clean and VENV.exists():
        print(f"Removing {VENV.name}...")
        shutil.rmtree(VENV)
    python = VENV / "Scripts" / "python.exe"
    if not python.is_file():
        print(f"Creating build venv {VENV.name} from Python {sys.version.split()[0]}...")
        run([sys.executable, "-m", "venv", VENV])
    return python


def smoke_test(exe: Path) -> None:
    """Start the built exe and make sure every dependency and the window load.

    Runs from an empty temp folder so nothing is picked up from the project by
    accident: if it works there, it works on a machine without Python.
    """
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / exe.name
        shutil.copy2(exe, staged)
        report = Path(tmp) / "smoke.txt"
        started = time.perf_counter()
        try:
            result = subprocess.run(
                [str(staged), "--smoke-test", str(report)], cwd=tmp, timeout=300
            )
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
        elapsed = time.perf_counter() - started
        text = report.read_text(encoding="utf-8") if report.is_file() else "(no report written)"
        for line in text.rstrip().splitlines():
            print(f"    {line}")
        if code != 0 or "SMOKE TEST PASSED" not in text:
            raise SystemExit(f"ERROR: the built exe failed its smoke test (exit {code})")
        print(f"  started and exited cleanly in {elapsed:.1f} s")


def zip_with_renders(exe: Path, version: str, force: bool) -> Path:
    source = ROOT / RENDERS
    if not source.is_dir():
        raise SystemExit(f"ERROR: --include-renders but {RENDERS.as_posix()} does not exist.")
    out = exe.with_name(f"{exe.stem}-with-renders.zip")
    if out.exists() and not force:
        raise SystemExit(f"ERROR: {out.name} already exists. Re-run with --force.")
    folder = f"Rivals-2-Palette-Creator-{version}"
    files = sorted(p for p in source.rglob("*") if p.is_file() and p.name != "Thumbs.db")
    print(f"Writing {out.name} (exe + {len(files):,} renders)...")
    # Stored, not deflated: the exe and the PNGs are already compressed.
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        zf.write(exe, f"{folder}/{exe.name}")
        for path in files:
            zf.write(path, f"{folder}/{path.relative_to(ROOT).as_posix()}")
    with zipfile.ZipFile(out) as zf:
        damaged = zf.testzip()
        if damaged:
            raise SystemExit(f"ERROR: archive is corrupt at {damaged}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--no-venv", action="store_true",
                        help="build with the current interpreter instead of .venv-build")
    parser.add_argument("--clean", action="store_true",
                        help="delete .venv-build and the PyInstaller cache first")
    parser.add_argument("--include-renders", action="store_true",
                        help=f"also write a zip of the exe plus {RENDERS.as_posix()}")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing --include-renders zip")
    parser.add_argument("--skip-smoke-test", action="store_true",
                        help="do not launch the exe after building")
    args = parser.parse_args()

    if sys.platform != "win32":
        print("ERROR: the exe has to be built on Windows (PyInstaller does not cross-compile).")
        return 1

    version = read_version()
    exe = ROOT / "dist" / f"Rivals-2-Palette-Creator-v{version}.exe"
    print(f"Rivals 2 Palette Creator - exe build {version}")
    print()

    python = build_python(not args.no_venv, args.clean)
    if args.clean and WORK.exists():
        shutil.rmtree(WORK)

    print("Installing build dependencies...")
    run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
         "--only-binary=:all:", "-r", "requirements.txt", "-r", "requirements-build.txt"])
    print()

    print("Running PyInstaller...")
    run([python, "-m", "PyInstaller", SPEC, "--noconfirm", "--log-level", "WARN",
         "--distpath", ROOT / "dist", "--workpath", WORK])
    if not exe.is_file():
        print(f"ERROR: PyInstaller finished but {exe} is missing.")
        return 1
    print()

    if args.skip_smoke_test:
        print("Smoke test skipped.")
    else:
        print("Smoke testing the exe...")
        smoke_test(exe)
    print()

    outputs = [exe]
    if args.include_renders:
        outputs.append(zip_with_renders(exe, version, args.force))
        print()

    print("=" * 60)
    for path in outputs:
        print(f"  {path.name}  {human(path.stat().st_size)}")
        print(f"  {path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
