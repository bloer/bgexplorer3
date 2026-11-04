""" Signals that other parts of the application can connect to.

They are blinker signals like the ones MongoEngine uses for documents, and
like those they hold receivers weakly, so connect module-level functions.
Receivers that raise abort the operation that sent the signal.
"""
from blinker import Signal
from typing import List

# Data in a version changed. Sent with the version name as sender, and with
# `record_id` (the id of the version's record) and `document`, the versioned
# document that was saved or deleted, or None when many changed at once.
version_changed = Signal('version_changed')

# A merge changed the target version. Sent with the target as sender and
# `source` and `plan` (a control.MergePlan) once the merged documents are
# adopted, while both versions are locked. Receivers can update what is
# derived from the documents and return a list of warning strings; one that
# raises undoes the merge.
version_merged = Signal('version_merged')

# A version was imported from a file. Sent with the new version as sender and
# `manifest` and `counts` once its documents are inserted, while it is locked.
# Receivers can return warnings and raise to remove the version again.
version_imported = Signal('version_imported')


def send_collecting_warnings(signal: Signal, sender, **kwargs) -> List[str]:
    """ Send `signal`; return the warning lists its receivers returned,
    joined. Exceptions from receivers propagate
    """
    warnings = []
    for _, result in signal.send(sender, **kwargs):
        if result:
            warnings.extend(result)
    return warnings
