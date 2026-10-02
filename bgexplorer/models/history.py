""" A log of operations on whole versions, such as branching and merging """
from mongoengine import (Document, DateTimeField, StringField, EnumField,
                         DictField, Q)
from enum import Enum
from typing import Optional
import datetime
import logging
from . import verdoc
log = logging.getLogger(__name__)


class EventAction(Enum):
    create_branch = 'create_branch'
    create_tag = 'create_tag'
    delete = 'delete'
    import_document = 'import_document'
    merge = 'merge'
    merge_rollback = 'merge_rollback'
    clear_lock = 'clear_lock'


class VersionEvent(Document):
    """ Something that happened to `version`. `other_version` is the other
    version involved, if any: the source of a branch, import or merge
    """
    time = DateTimeField(default=datetime.datetime.now)
    user = StringField()
    action = EnumField(EventAction, required=True)
    version = StringField(required=True)
    other_version = StringField()
    message = StringField()
    details = DictField()
    meta = {'indexes': ['version', 'other_version', '-time'],
            'ordering': ['-time']}

    @property
    def summary(self) -> str:
        """ A short description of the event """
        other = self.other_version
        text = {
            EventAction.create_branch: (f"Branch {self.version} created"
                                        + (f" from {other}" if other else "")),
            EventAction.create_tag: f"Tag {self.version} created from {other}",
            EventAction.delete: f"{self.version} deleted",
            EventAction.import_document: (f"Imported into {self.version} "
                                          f"from {other}"),
            EventAction.merge: f"Merged {other} into {self.version}",
            EventAction.merge_rollback: (f"Merge of {other} into "
                                         f"{self.version} rolled back"),
            EventAction.clear_lock: f"Lock on {self.version} cleared",
        }[self.action]
        return f"{text}: {self.message}" if self.message else text


def log_event(action: EventAction, version: str,
              other_version: Optional[str] = None,
              message: Optional[str] = None, **details) -> VersionEvent:
    """ Record an event. The user is the one making changes, see
    verdoc.set_user_provider
    """
    log.info("%s %s %s %s", action.value, version, other_version or '',
             message or '')
    return VersionEvent(action=action, version=version,
                        other_version=other_version, message=message,
                        user=verdoc.current_user_name(),
                        details=details).save()


def recent_events(version: str, limit: int = 20):
    """ The latest events involving `version` """
    return VersionEvent.objects(Q(version=version)
                                | Q(other_version=version))[:limit]
