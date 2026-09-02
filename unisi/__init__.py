from .utils import *
from .llmrag import Q, Qx
from .units import *
from .users import User
from .server import start, handle, test, context_user, context_screen
from .tables import *
from .containers import *
from .proxy import *
from .dbunits import *
from .graphs import *
from .db import Database, Dbtable, db

# `from unisi import *` is the documented convention for every screen/block
# module (README, quickstart, every example). Without an explicit __all__,
# Python's default `import *` behavior re-exports *everything* reachable
# from this package's namespace that doesn't start with '_' -- and that
# includes submodules that only got registered here as a side effect of
# the `from .xxx import ...` lines above (Python always sets
# `unisi.<submodule> = <module>` the moment a submodule is imported
# anywhere, regardless of whether __init__.py re-exports it on purpose).
#
# Concretely, without this, `from unisi import *` also binds names like
# `persist`, `db` -- wait, `db` is intentional above, but `tables`, `users`,
# `proxy`, `graphs`, `containers`, `units`, `os`, `json`, `sys`, `requests`
# and two dozen others to their *module objects*. A screen that (very
# reasonably) writes `persist = True` at module level is fine, but a
# screen that never mentions `persist` at all still ends up with a
# module-level `persist` name bound to `<module 'unisi.persist' ...>`,
# which `compile_screen()` then reads via `getattr(module, 'persist',
# False)` when building the Screen -- silently replacing the intended
# `False` default with a truthy, non-boolean, unserializable value.
#
# Rebuilding __all__ here from everything currently exposed, minus module
# objects, fixes this at the root and stays correct as the public API
# grows: a new class or function is still exported automatically, a new
# accidental `from .x import *` leaking a submodule is not.
import types as _types
__all__ = [_name for _name, _value in dict(globals()).items()
           if not _name.startswith('_') and not isinstance(_value, _types.ModuleType)]
del _types
