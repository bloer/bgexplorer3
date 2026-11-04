""" Merging and importing versions of toy documents, and the signals that let
an application react to them """
import io
import unittest
from bgexplorer.versioning import (create_branch, merge_version, plan_merge,
                                   export_version, import_version, hooks,
                                   version_exists, get_record, MergeRule,
                                   MergeError, VersionFileError)
from tests.versioning.toy import ToyTestCase, archive_format


def warn(sender, **kwargs):
    return [f"warning about {sender}"]


def fail(sender, **kwargs):
    raise RuntimeError("receiver failed")


class MergeCase(ToyTestCase):
    def setUp(self):
        super().setUp()
        self.Part(name='bolt', weight=1.0).save()
        create_branch('dev', self.default_tag)
        bolt = self.Part.select_version('dev').get()
        bolt.weight = 2.0
        bolt.save()
        washer = self.Part(name='washer', weight=0.1)
        washer.version_tags = ['dev']
        washer.save()

    def weights(self, tag):
        return {p.name: p.weight for p in self.Part.select_version(tag)}


class TestMerge(MergeCase):
    def test_plan_changes_nothing(self):
        plan = plan_merge('dev', self.default_tag)
        self.assertTrue(plan.changes)
        self.assertEqual(plan.counts()['Part'],
                         {'add': 1, 'replace': 1, 'keep': 0})
        self.assertEqual(self.weights(self.default_tag), {'bolt': 1.0})

    def test_merge(self):
        merge_version('dev', self.default_tag, MergeRule.source)
        self.assertEqual(self.weights(self.default_tag),
                         {'bolt': 2.0, 'washer': 0.1})

    def test_rule_target_keeps(self):
        merge_version('dev', self.default_tag, MergeRule.target)
        self.assertEqual(self.weights(self.default_tag),
                         {'bolt': 1.0, 'washer': 0.1})

    def test_fingerprint_mismatch(self):
        plan = plan_merge('dev', self.default_tag)
        extra = self.Part(name='extra')
        extra.version_tags = ['dev']
        extra.save()
        with self.assertRaises(MergeError):
            merge_version('dev', self.default_tag,
                          fingerprint=plan.fingerprint)

    def test_receiver_gets_plan_and_warnings_are_collected(self):
        seen = []

        def receiver(sender, source=None, plan=None, **kwargs):
            seen.append((sender, source, plan.changes))
            return ["check this"]
        hooks.version_merged.connect(receiver)
        hooks.version_merged.is_muted = False
        try:
            plan = merge_version('dev', self.default_tag)
        finally:
            hooks.version_merged.disconnect(receiver)
            hooks.version_merged.is_muted = True
        self.assertEqual(seen, [(self.default_tag, 'dev', True)])
        self.assertEqual(plan.warnings, ["check this"])

    def test_raising_receiver_rolls_back(self):
        hooks.version_merged.connect(fail)
        hooks.version_merged.is_muted = False
        try:
            with self.assertRaisesRegex(MergeError, 'receiver failed'):
                merge_version('dev', self.default_tag, MergeRule.source)
        finally:
            hooks.version_merged.disconnect(fail)
            hooks.version_merged.is_muted = True
        self.assertEqual(self.weights(self.default_tag), {'bolt': 1.0})
        self.assertEqual(self.weights('dev'), {'bolt': 2.0, 'washer': 0.1})
        # and nothing stays locked
        self.assertIsNone(get_record(self.default_tag).lock)
        self.assertIsNone(get_record('dev').lock)


class TestArchive(MergeCase):
    def export(self, tag):
        buf = io.BytesIO()
        manifest = export_version(tag, buf)
        buf.seek(0)
        return manifest, buf

    def test_roundtrip(self):
        group = self.Group(name='kit', parts=[
            self.Part.select_version('dev').get(name='bolt')])
        group.version_tags = ['dev']
        group.save()
        with archive_format():
            manifest, buf = self.export('dev')
            self.assertEqual(manifest['format'], 'toy-archive')
            self.assertEqual(manifest['producer'], 'toys')
            self.assertEqual(manifest['counts']['Part'], 2)
            record = import_version(buf, 'copy')
        self.assertTrue(version_exists('copy'))
        self.assertIsNone(record.lock)
        self.assertEqual(self.weights('copy'), self.weights('dev'))
        orig = {p.name: p.original_id for p in self.Part.select_version('dev')}
        new = {p.name: p.original_id for p in self.Part.select_version('copy')}
        self.assertEqual(orig, new)
        kit = self.Group.select_version('copy').get()
        self.assertEqual([p.name for p in kit.parts], ['bolt'])

    def test_other_format_rejected(self):
        with archive_format('other'):
            _, buf = self.export('dev')
        with archive_format():
            with self.assertRaises(VersionFileError):
                import_version(buf, 'copy')
        self.assertFalse(version_exists('copy'))

    def test_not_an_archive(self):
        with self.assertRaises(VersionFileError):
            import_version(io.BytesIO(b'garbage'), 'copy')
        self.assertFalse(version_exists('copy'))

    def test_dangling_reference_rejected(self):
        group = self.Group(name='kit', parts=[
            self.Part.select_version('dev').get(name='bolt')])
        group.version_tags = ['dev']
        group.save()
        with archive_format():
            _, buf = self.export('dev')
            self.Part.objects(version_tags='dev', name='bolt').delete()
            # a file missing a referenced part can't be imported
            _, bad = self.export('dev')
            with self.assertRaises(VersionFileError) as cm:
                import_version(bad, 'copy')
        self.assertTrue(any('refers to documents' in p
                            for p in cm.exception.problems))

    def test_receiver_warnings_and_failure(self):
        seen = []

        def receiver(sender, manifest=None, counts=None, **kwargs):
            seen.append((sender, counts['Part']))
            return ["rebuilt"]
        with archive_format():
            _, buf = self.export('dev')
            hooks.version_imported.is_muted = False
            hooks.version_imported.connect(receiver)
            try:
                import_version(buf, 'copy')
            finally:
                hooks.version_imported.disconnect(receiver)
            self.assertEqual(seen, [('copy', 2)])

            _, buf = self.export('dev')
            hooks.version_imported.connect(fail)
            try:
                with self.assertRaises(VersionFileError):
                    import_version(buf, 'copy2')
            finally:
                hooks.version_imported.disconnect(fail)
                hooks.version_imported.is_muted = True
        self.assertFalse(version_exists('copy2'))
        self.assertEqual(self.Part.select_version('copy2').count(), 0)


if __name__ == '__main__':
    unittest.main()
