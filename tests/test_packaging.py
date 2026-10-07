import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('collect_sources', Path(__file__).resolve().parents[1] / 'packaging/collect_sources.py')
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class InventoryTests(unittest.TestCase):
    def test_exact_source_identity(self):
        data = 'P:example-lib\nV:1.2-r0\no:example\nc:abcdef123\nL:MIT\nF:usr/lib\nR:lib.so\n'
        self.assertEqual(collector.inventory(data)[0], dict(name='example-lib', version='1.2-r0',
                         origin='example', commit='abcdef123', license='MIT', upstream=''))

    def test_missing_or_invalid_source_identity_fails(self):
        for data in ('', 'P:example\n', 'P:e\nV:1\no:../escape\nc:abcdef123\nL:MIT',
                     'P:e\nV:1\no:e\nc:not-a-commit\nL:MIT'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                collector.inventory(data)
