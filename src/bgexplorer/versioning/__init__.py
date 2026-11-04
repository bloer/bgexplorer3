"""Copy-on-write versioning of MongoEngine documents"""
from . import hooks, record, locks  # noqa: F401  (locks installs the write guard)
