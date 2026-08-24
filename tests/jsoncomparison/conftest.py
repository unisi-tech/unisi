"""
Shared pytest setup for the unisi/jsoncomparison/ unit tests.

jsoncomparison (compare.py's Compare/Config, errors.py's Error subclasses,
ignore.py's Ignore) is a fully self-contained, pure-logic package: every
public entry point is a plain function/classmethod operating on the dicts,
lists and scalars it's handed, with no dependency on a running app, a User,
or the filesystem (aside from the *optional* output.file report feature,
which a handful of tests exercise directly via tmp_path). Unlike
tests/units or tests/core, there's no ambient framework state to snapshot/
restore here and no fixtures_app is needed -- these tests import
unisi.jsoncomparison directly and construct all their own inputs.

Importing anything under the `unisi` package (jsoncomparison is a
subpackage of it) still runs unisi/__init__.py first, which in turn runs
utils.py's module-scope config bootstrap -- see tests/core/test_utils.py's
docstring for the full story. That bootstrap is a no-op concern for this
directory: it only needs to have happened once per process (already
guaranteed by pytest itself importing conftest modules across the whole
session), and nothing here reads or depends on the resulting `config`
object's contents.
"""
import sys
from pathlib import Path

THIS_DIR = Path(__file__).parent
UNISI_ROOT = THIS_DIR.parent.parent
if str(UNISI_ROOT) not in sys.path:
    sys.path.insert(0, str(UNISI_ROOT))
