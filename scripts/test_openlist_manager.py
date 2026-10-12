"""Check independent OpenList identity, port setup and official release selection."""
import ast
import importlib.util
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / 'addons/script.openlist'
CONFIG = ast.literal_eval((ADDON / 'resources/lib/service_config.py').read_text().split('=', 1)[1])
spec = importlib.util.spec_from_file_location('openlist_installer', ADDON / 'resources/lib/runtime_installer.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OpenListTests(unittest.TestCase):
    def installer(self, directory):
        controller = Mock()
        controller.config = dict(CONFIG, data_dir=str(Path(directory) / 'data'))
        return module.RuntimeInstaller(controller)

    def test_independent_identity_unit_binary_data_and_port(self):
        self.assertEqual(CONFIG['addon_id'], 'script.openlist')
        self.assertEqual(CONFIG['unit'], 'openlist.service')
        self.assertEqual(CONFIG['port'], 5245)
        self.assertTrue(CONFIG['binary'].endswith('/service.openlist/openlist'))
        self.assertTrue(CONFIG['data_dir'].endswith('/service.openlist'))
        self.assertEqual(ET.parse(ADDON / 'addon.xml').getroot().get('id'), CONFIG['addon_id'])
        unit = (ADDON / 'resources/systemd/openlist.service').read_text()
        self.assertNotIn('service.alist', unit)
        self.assertIn(CONFIG['binary'], unit)
        self.assertIn(CONFIG['data_dir'], unit)
        for path in ADDON.rglob('*.py'):
            self.assertNotIn('script.module.cebox', path.read_text())
            self.assertNotIn('from script.alist', path.read_text())

    def test_first_config_uses_separate_port(self):
        with tempfile.TemporaryDirectory() as d:
            installer = self.installer(d)
            installer._prepare_first_configuration()
            config = json.loads((Path(d) / 'data/config.json').read_text())
            self.assertEqual(config, {'scheme': {'http_port': 5245}})

    def test_existing_config_is_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as d:
            installer = self.installer(d)
            data = Path(d) / 'data'
            data.mkdir()
            original = b'{"scheme":{"http_port":55245},"jwt_secret":"existing-test-setting"}'
            (data / 'config.json').write_bytes(original)
            installer._prepare_first_configuration()
            self.assertEqual((data / 'config.json').read_bytes(), original)

    def test_selects_full_official_linux_arm64_asset(self):
        with tempfile.TemporaryDirectory() as d:
            installer = self.installer(d)
            release = {'tag_name': 'v4.2.6', 'assets': [
                {'name': 'openlist-linux-arm64-lite.tar.gz', 'size': 10, 'digest': 'sha256:' + '1' * 64,
                 'browser_download_url': 'https://github.com/OpenListTeam/OpenList/releases/download/v4.2.6/openlist-linux-arm64-lite.tar.gz'},
                {'name': 'openlist-linux-arm64.tar.gz', 'size': 20, 'digest': 'sha256:' + '2' * 64,
                 'browser_download_url': 'https://github.com/OpenListTeam/OpenList/releases/download/v4.2.6/openlist-linux-arm64.tar.gz'}]}
            with patch.object(module, '_open', return_value=io.BytesIO(json.dumps(release).encode())):
                selected = installer.latest()
            self.assertEqual(selected['name'], 'openlist-linux-arm64.tar.gz')
            self.assertEqual(selected['sha256'], '2' * 64)

    def test_version_command_is_safe_and_matches_official_output(self):
        with tempfile.TemporaryDirectory() as d:
            installer = self.installer(d)
            installer.binary = Path(d) / 'openlist'
            installer.binary.write_bytes(b'test executable')
            installer.binary.chmod(0o755)
            installer.controller._run.return_value = Mock(returncode=0, stdout='Version: v4.2.6\nWebVersion: 4.2.6\n')
            self.assertEqual(installer.local_version(), '4.2.6')
            installer.controller._run.assert_called_once_with([str(installer.binary), 'version'], timeout=5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
