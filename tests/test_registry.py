""" Version control finds the versioned classes by itself """
import unittest
import bgexplorer.models  # noqa: F401, imports every model module
from bgexplorer.models.assay import Assay
from bgexplorer.models.component import Component, Assembly
from bgexplorer.models.cosmogenic import ActivatedMaterial, CosmogenicActivation
from bgexplorer.models.emissionspec import EmissionSpec
from bgexplorer.models.hiteff import HitEfficiency
from bgexplorer.models.sourceterm import SourceTerm
from bgexplorer.versioning.verdoc import (VersionedDocument, versioned_classes,
                                      referenced_classes)


class TestRegistry(unittest.TestCase):
    def test_versioned_classes(self):
        # one class per collection, and no more
        self.assertCountEqual(versioned_classes(),
                              [Component, EmissionSpec, HitEfficiency,
                               ActivatedMaterial, SourceTerm])
        for cls in (Assembly, CosmogenicActivation, Assay):
            self.assertNotIn(cls, versioned_classes())
            self.assertIn(cls._get_collection_name(),
                          [c._get_collection_name()
                           for c in versioned_classes()])

    def test_referenced_classes(self):
        self.assertCountEqual(referenced_classes(),
                              [Component, EmissionSpec, HitEfficiency,
                               ActivatedMaterial])
        # what refers to others comes last
        self.assertEqual(versioned_classes()[-1], SourceTerm)

    def test_new_subclasses(self):
        class Extra(VersionedDocument):
            meta = {'allow_inheritance': True}

        class ExtraChild(Extra):
            pass

        class Hidden(VersionedDocument):
            VERSION_REGISTER = False

        class Abstract(VersionedDocument):
            meta = {'abstract': True}

        try:
            self.assertIn(Extra, versioned_classes())
            self.assertIn(Extra, referenced_classes())
            self.assertNotIn(Hidden, versioned_classes())
            self.assertNotIn(Abstract, versioned_classes())
            self.assertNotIn(ExtraChild, versioned_classes())
        finally:
            from bgexplorer.versioning import verdoc
            for cls in (Extra, ExtraChild, Hidden, Abstract):
                verdoc._defined.remove(cls)
