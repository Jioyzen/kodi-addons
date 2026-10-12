import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from build_repository import ROOT, build, validate


class RepositoryTests(unittest.TestCase):
    def test_packages_index_hashes_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / 'one', Path(directory) / 'two'
            rows = build(ROOT / 'addons', first)
            again = build(ROOT / 'addons', second)
            self.assertEqual(rows, again)
            data = (first / 'addons.xml').read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), (first / 'addons.xml.sha256').read_text().strip())
            self.assertEqual(hashlib.md5(data).hexdigest(), (first / 'addons.xml.md5').read_text().strip())
            self.assertEqual(len(ET.fromstring(data)), 6)
            for row in rows:
                with zipfile.ZipFile(first / row['path']) as package:
                    self.assertIsNone(package.testzip())
                    self.assertTrue(all(name.startswith(row['id'] + '/') for name in package.namelist()))
                    xml = ET.fromstring(package.read(row['id'] + '/addon.xml'))
                    self.assertEqual(xml.get('version'), row['version'])
                    self.assertFalse(any(d.get('addon') == 'script.module.cebox' for d in xml.findall('./requires/import')))
                self.assertEqual(hashlib.sha256((first / row['path']).read_bytes()).hexdigest(), row['sha256'])
            repo = ET.parse(ROOT / 'addons/repository.jioyzen/addon.xml').getroot()
            self.assertEqual(repo.findtext("extension[@point='xbmc.addon.repository']/dir/hashes"), 'sha256')

    def test_rejects_private_token_and_broken_python(self):
        with tempfile.TemporaryDirectory() as directory:
            addon = Path(directory) / 'test.addon'
            addon.mkdir()
            (addon / 'addon.xml').write_text('<addon id="test.addon" version="1.0.0"/>')
            source = addon / 'main.py'
            source.write_text('token = "sk-' + 'a' * 32 + '"')
            with self.assertRaisesRegex(ValueError, 'Private API token'):
                validate(addon)
            source.write_text('def broken(:')
            with self.assertRaises(SyntaxError):
                validate(addon)


if __name__ == '__main__':
    unittest.main(verbosity=2)
