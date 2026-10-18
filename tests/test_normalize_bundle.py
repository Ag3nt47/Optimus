"""Packaging guards use fabricated binary metadata and temporary directories only."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('normalize_bundle', Path(__file__).parents[1] / 'tools' / 'normalize_bundle.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BundleNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / 'bundle'
        self.internal = self.bundle / '_internal'
        self.qt = self.internal / 'PySide6' / 'Qt6Core.dll'
        self.conflict = self.internal / 'icuuc.dll'
        self.data = self.internal / 'icudt78.dll'
        self.system = self.root / 'System32'
        self.system_icu = self.system / 'icuuc.dll'
        self.metadata = {}
        self.add(self.qt, imports={'icuuc.dll': {'ucnv_open', 'ucnv_close'}})
        self.add(self.conflict, exports={'ucnv_open_78', 'ucnv_close_78'}, imports={'icudt78.dll': {'icudt78_dat'}})
        self.add(self.data, exports={'icudt78_dat'})
        self.add(self.system_icu, exports={'ucnv_open', 'ucnv_close'})

    def add(self, path, exports=(), imports=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture only')
        self.metadata[path] = {'exports': set(exports), 'imports': imports or {}}

    def plan(self):
        return module.plan_normalization(self.bundle, self.system, self.metadata.__getitem__)

    def test_known_collision_only(self):
        self.assertEqual(set(self.plan()), {self.conflict, self.data})
        self.assertTrue(self.conflict.exists())
        result = module.normalize_bundle(self.bundle, self.system, self.metadata.__getitem__)
        self.assertEqual(len(result['removed']), 2)
        self.assertFalse(self.conflict.exists())
        self.assertTrue(self.qt.exists())
        self.assertTrue(self.system_icu.exists())

    def test_missing_system_api_refuses(self):
        self.metadata[self.system_icu]['exports'] = {'ucnv_open'}
        with self.assertRaises(ValueError): self.plan()
        self.assertTrue(self.conflict.exists())

    def test_unknown_conflict_refuses(self):
        self.metadata[self.conflict]['exports'] = {'some_other_api'}
        with self.assertRaises(ValueError): self.plan()
        self.assertTrue(self.data.exists())

    def test_other_consumer_refuses(self):
        self.add(self.internal / 'other.dll', imports={'icuuc.dll': {'ucnv_open_78'}})
        with self.assertRaises(ValueError): self.plan()
        self.assertTrue(self.conflict.exists())

    def test_already_compatible_is_unchanged(self):
        self.metadata[self.conflict]['exports'] = {'ucnv_open', 'ucnv_close'}
        self.assertEqual(self.plan(), [])
        self.assertTrue(self.data.exists())

    def test_no_conflict_is_idempotent(self):
        self.conflict.unlink()
        self.assertEqual(self.plan(), [])

    def test_different_qt_api_refuses(self):
        self.metadata[self.qt]['imports'] = {'icuuc.dll': {'ucnv_open_78'}}
        with self.assertRaises(ValueError): self.plan()

    def test_other_data_consumer_refuses(self):
        self.add(self.internal / 'other.dll', imports={'icudt78.dll': {'icudt78_dat'}})
        with self.assertRaises(ValueError): self.plan()


if __name__ == '__main__':
    unittest.main()
