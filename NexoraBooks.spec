# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for Nexora Books.

Produces a single folder, dist/NexoraBooks, containing NexoraBooks.exe and
everything it needs. Build it with build_windows.bat, or directly:

    pyinstaller NexoraBooks.spec --noconfirm
"""

block_cipher = None

hidden = [
    # Uvicorn loads these by name at runtime, so PyInstaller cannot see them
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.sql.default_comparator",
    "email.mime.text",
    "email.mime.multipart",
    # Reached only through "NexoraBooks.exe --reset-two-factor", so nothing
    # imports it at start-up and PyInstaller would otherwise leave it out.
    # Somebody shut out of their own books by a lost phone must not need a
    # Python installation to get back in.
    "reset_two_factor",
]

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("app/templates", "app/templates"),
        ("app/static", "app/static"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NexoraBooks",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # the console window shows the network address for staff
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app/static/nexorabooks.ico" if __import__("os").path.exists("app/static/nexorabooks.ico") else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NexoraBooks",
)
