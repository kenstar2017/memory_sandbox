# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Memory Sandbox macOS .app (Web UI, no tkinter)

from pathlib import Path

ROOT = Path(SPEC).resolve().parent.parent
ICNS = ROOT / 'packaging' / 'MemorySandbox.icns'

block_cipher = None

a = Analysis(
    [str(ROOT / 'app_web.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'config.yaml'), '.'),
    ],
    hiddenimports=[
        'yaml',
        'core',
        'core.sandbox',
        'core.config',
        'core.sensory',
        'core.working',
        'core.long_term',
        'core.embedding',
        'core.rules',
        'core.llm',
        'core.paths',
        'core.utils',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', '_tkinter', 'tcl', 'tk'],
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
    name='MemorySandbox',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MemorySandbox',
)

bundle_kwargs = dict(
    name='MemorySandbox.app',
    bundle_identifier='com.local.memorysandbox',
    info_plist={
        'CFBundleName': 'MemorySandbox',
        'CFBundleDisplayName': '记忆沙箱',
        'CFBundleShortVersionString': '0.1.2',
        'CFBundleVersion': '0.1.2',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
        'NSPrincipalClass': 'NSApplication',
        # 允许本地回环网络（Web UI）
        'NSAppTransportSecurity': {
            'NSAllowsLocalNetworking': True,
        },
    },
)
if ICNS.exists():
    bundle_kwargs['icon'] = str(ICNS)

app = BUNDLE(coll, **bundle_kwargs)
