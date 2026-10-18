"""Boot Optimus, optionally from the user-confirmed source sandbox."""
import os
import sys
from pathlib import Path

if '--source-workspace' in sys.argv:
    option = sys.argv.index('--source-workspace')
    if option + 1 >= len(sys.argv):
        raise SystemExit('Missing source workspace.')
    workspace = Path(sys.argv[option + 1]).resolve()
    allowed = (Path(os.environ['LOCALAPPDATA']) / 'Optimus' / 'Sandbox' / 'workspace').resolve()
    if workspace != allowed or not (workspace / 'src/optimus/app.py').is_file():
        raise SystemExit('Source restart is limited to the saved Optimus workspace.')
    sys.path.insert(0, str(workspace / 'src'))
    os.environ['OPTIMUS_SOURCE_WORKSPACE'] = str(workspace)

from optimus.app import main
raise SystemExit(main())
