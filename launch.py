"""Boot Optimus, optionally from the user-confirmed source sandbox."""
import os
import sys
import importlib.util
from pathlib import Path

if '--source-workspace' in sys.argv:
    option = sys.argv.index('--source-workspace')
    if option + 1 >= len(sys.argv):
        raise SystemExit('Missing source workspace.')
    # Load the shipped, pure-stdlib helper directly. Importing optimus here would
    # cache its bundled package path and prevent approved edited code from loading.
    helper_path = (Path(sys._MEIPASS) / 'optimus' / 'filesystem.py' if getattr(sys, 'frozen', False)
                   else Path(__file__).resolve().parent / 'src' / 'optimus' / 'filesystem.py')
    if not helper_path.is_file():
        raise SystemExit('The shipped data-directory helper is missing.')
    helper_spec = importlib.util.spec_from_file_location('_optimus_bootstrap_filesystem', helper_path)
    if helper_spec is None or helper_spec.loader is None:
        raise SystemExit('The shipped data-directory helper is missing.')
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    try:
        authority = helper.resolve_data_root(Path(os.environ['LOCALAPPDATA']) / 'Optimus')
        supplied_workspace = Path(os.path.abspath(sys.argv[option + 1]))
        logical_allowed = authority / 'Sandbox' / 'workspace'
        helper._ordinary_data_path(logical_allowed)
        helper._ordinary_data_path(supplied_workspace)
        workspace = supplied_workspace.resolve(strict=True)
        allowed = logical_allowed.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise SystemExit('The saved Optimus data directory could not be verified.') from error
    if workspace != allowed or not (workspace / 'src/optimus/app.py').is_file():
        raise SystemExit('Source restart is limited to the saved Optimus workspace.')
    sys.path.insert(0, str(workspace / 'src'))
    os.environ['OPTIMUS_SOURCE_WORKSPACE'] = str(workspace)

from optimus.app import main
raise SystemExit(main())
