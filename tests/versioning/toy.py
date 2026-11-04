""" Versioned classes that know nothing about bgexplorer, to test the
versioning package on its own
"""
import contextlib
import unittest
from types import SimpleNamespace
from mongoengine import StringField, FloatField, disconnect
from bgexplorer.versioning import (VersionedDocument, VersionedListField,
                                   VersionedReferenceField, VersionRecord,
                                   hooks, archive, create_version)
from bgexplorer.versioning.history import VersionEvent
from bgexplorer.versioning import verdoc, versioned_classes
from tests.dbutil import connect_test_db


def define_toys() -> SimpleNamespace:
    """ Define the toy classes: Part, Group which refers to parts and Total,
    which is derived from groups. They are registered with the versioning
    package until `forget_toys`
    """
    class Part(VersionedDocument):
        name = StringField(required=True)
        weight = FloatField()

        @classmethod
        def item_label(cls, son):
            return f"part {son['name']}"

    class Group(VersionedDocument):
        name = StringField(required=True)
        parts = VersionedListField(VersionedReferenceField(Part))

        @classmethod
        def find_dangling(cls, version_tag):
            have = set(Part._get_collection().distinct(
                'original_id', {'version_tags': version_tag}))
            return [son['_id'] for son in cls._get_collection().find(
                {'version_tags': version_tag}, {'parts': 1})
                if not {verdoc.ref_id(p) for p in son.get('parts', [])} <= have]

    class Total(VersionedDocument):
        VERSION_DERIVED = True
        group = VersionedReferenceField(Group)
        weight = FloatField()

    return SimpleNamespace(Part=Part, Group=Group, Total=Total)


def forget_toys(toys: SimpleNamespace) -> None:
    for cls in vars(toys).values():
        verdoc._defined.remove(cls)


class ToyTestCase(unittest.TestCase):
    """ Connects to the test database, defines the toy classes and silences
    the application's receivers of the version signals
    """
    @classmethod
    def setUpClass(cls):
        connect_test_db()
        cls.toys = define_toys()
        cls.Part, cls.Group, cls.Total = (cls.toys.Part, cls.toys.Group,
                                          cls.toys.Total)
        stack = contextlib.ExitStack()
        for signal in (hooks.version_merged, hooks.version_imported):
            stack.enter_context(signal.muted())
        stack.callback(forget_toys, cls.toys)
        stack.callback(disconnect)
        cls.addClassCleanup(stack.close)

    def setUp(self):
        for cls in [VersionRecord, VersionEvent] + versioned_classes():
            cls.drop_collection()
        self.default_tag = VersionedDocument.get_default_tag()
        create_version(self.default_tag)


@contextlib.contextmanager
def archive_format(name='toy-archive'):
    """ Use another archive format name within the context """
    saved = dict(archive._format)
    archive.configure(name, 'toys', '0')
    try:
        yield
    finally:
        archive._format.update(saved)
