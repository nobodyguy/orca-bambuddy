"""OrcaSlicer plugin: send sliced plates to a Bambuddy server and watch the printers.

Inside OrcaSlicer the ``orca`` module exists and the plugin registers itself; outside
it (tests, tooling) only the OrcaSlicer-independent modules are importable.
"""

try:
    import orca  # noqa: F401 - provided by OrcaSlicer's embedded interpreter
except ImportError:
    orca = None

if orca is not None:
    from .orca_adapter import BambuddyPlugin  # noqa: F401 - @orca.plugin registers it
