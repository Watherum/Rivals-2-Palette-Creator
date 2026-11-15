# PyInstaller spec: one windowed exe. Run it through scripts/build_exe.py
# (or BUILD_EXE.bat), which sets up the build environment and tests the result.

import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)

version = re.search(
    r'^__version__\s*=\s*["\']([^"\']+)["\']',
    (ROOT / "palette_creator" / "__init__.py").read_text(encoding="utf-8"),
    re.M,
).group(1)
numbers = [int(n) for n in re.findall(r"\d+", version)][:4]
numbers += [0] * (4 - len(numbers))
quad = ", ".join(str(n) for n in numbers)

# Shows up under Properties > Details on the exe.
version_file = ROOT / "build" / "version_info.txt"
version_file.parent.mkdir(parents=True, exist_ok=True)
version_file.write_text(
    f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({quad}), prodvers=({quad}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Watherum'),
      StringStruct('FileDescription', 'Rivals 2 Palette Creator'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'Rivals-2-Palette-Creator'),
      StringStruct('OriginalFilename', 'Rivals-2-Palette-Creator.exe'),
      StringStruct('ProductName', 'Rivals 2 Palette Creator'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
    encoding="utf-8",
)

icon = ROOT / "Resources" / "app.ico"  # optional: drop one here to brand the exe

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    # customtkinter loads its theme json and fonts from disk at start-up.
    datas=collect_data_files("customtkinter"),
    hiddenimports=[],
    # Do not exclude numpy.f2py to save space: scipy's array-api shim walks
    # every numpy attribute at import and the exe dies without it.
    excludes=["pytest", "matplotlib", "IPython", "tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"Rivals-2-Palette-Creator-v{version}",
    console=False,
    # UPX is off on purpose: packed exes trip antivirus far more often and
    # unpack slower on every launch.
    upx=False,
    version=str(version_file),
    icon=str(icon) if icon.is_file() else None,
)
