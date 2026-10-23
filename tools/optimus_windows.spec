"""One payload with a windowed UI and a hidden console progress supervisor.

The second bootloader attaches to ConPTY; a GUI-subsystem executable cannot.
Both entry points run the same early private command dispatcher in launch.py.
"""
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

source = Path(SPECPATH).parent
datas, binaries, hiddenimports = collect_all('optimus')
datas += collect_data_files('certifi')
datas += [(str(source / name), '.') for name in ('LICENSE', 'NOTICE')]
hiddenimports += collect_submodules('psutil') + collect_submodules('send2trash')
a = Analysis([str(source / 'launch.py')], pathex=[str(source / 'src')],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports)
pyz = PYZ(a.pure)
shared = dict(exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
              strip=False, upx=True, icon=str(source / 'src/optimus/assets/optimus.ico'))
gui = EXE(pyz, a.scripts, [], name='Optimus', console=False, **shared)
console = EXE(pyz, a.scripts, [], name='OptimusConsole', console=True, **shared)
coll = COLLECT(gui, console, a.binaries, a.datas, strip=False, upx=True, name='Optimus')
