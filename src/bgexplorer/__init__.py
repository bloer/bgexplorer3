import importlib

# Loaded on first use, so that subpackages such as `bgexplorer.versioning`
# can be imported without the web application and the models
_EXPORTS = {
    'create_app': '.application.app',
    'units': '.models.common',
    'AsymmetricUncertainty': '.models.asymmetric',
    'Histogram': '.models.hiteff',
    'HitEfficiency': '.models.hiteff',
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        return getattr(importlib.import_module(_EXPORTS[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
