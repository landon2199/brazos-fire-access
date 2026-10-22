"""Fire-station drive-time access and location-allocation on an OpenStreetMap road network."""
import os as _os
import sys as _sys

# Windows + conda: MKL can crash or hang on its default threading layer when GDAL's
# threads are active, and DLLs resolve wrongly when the env is not activated.
_os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
if _sys.platform == "win32":
    _bin = _os.path.join(_sys.prefix, "Library", "bin")
    if _os.path.isdir(_bin) and not _os.environ.get("PATH", "").lower().startswith(_bin.lower()):
        _os.environ["PATH"] = _bin + _os.pathsep + _os.environ.get("PATH", "")
    for _var, _sub in (("GDAL_DATA", ("Library", "share", "gdal")), ("PROJ_DATA", ("Library", "share", "proj"))):
        _p = _os.path.join(_sys.prefix, *_sub)
        if _var not in _os.environ and _os.path.isdir(_p):
            _os.environ[_var] = _p

__version__ = "0.1.0"
