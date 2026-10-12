"""Exercise installation transactions without Kodi, downloads or real services."""
import hashlib
import importlib.util
import io
import json
import os
import struct
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_installer', ROOT / 'addons/script.clouddrive2/resources/lib/runtime_installer.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def elf(payload=b''):
    header = bytearray(20)
    header[:6] = b'\x7fELF\x02\x01'
    header[18:20] = struct.pack('<H', 183)
    return bytes(header) + payload


def archive_bytes(items=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, data in (items or [('release/clouddrive', elf(b'new')), ('release/wwwroot/index.html', b'new page')]):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Controller:
    def __init__(self, root, active=False, fail=False):
        self.config = {'addon_id': 'script.clouddrive2', 'name': 'CloudDrive2',
                       'binary': str(root / 'program/clouddrive'), 'data_dir': str(root / 'data'),
                       'state_directory': str(root / 'state'), 'unit_directory': str(root / 'units'),
                       'unit': 'clouddrive2.service', 'password_reset': False,
                       'release_repository': 'cloud-fs/cloud-fs.github.io',
                       'asset_name': 'clouddrive-2-linux-aarch64-{version}.tgz'}
        (root / 'units').mkdir()
        self.active, self.enabled, self.fail, self.actions = active, False, fail, []

    def state(self):
        return {'ActiveState': 'active' if self.active else 'inactive', 'UnitFileState': 'disabled'}

    def is_active(self, state):
        return state['ActiveState'] == 'active'

    def stop(self):
        self.actions.append('stop')
        self.active = False

    def start(self):
        self.actions.append('start')
        data = Path(self.config['data_dir'])
        data.mkdir(exist_ok=True)
        (data / 'config.json').write_bytes(b'newly migrated config')
        unit = Path(self.config['unit_directory']) / self.config['unit']
        unit.write_text('test unit') if not unit.exists() else None
        self.active = True
        if self.fail:
            self.fail = False
            raise RuntimeError('New program startup failed')

    def _run(self, args, **kwargs):
        self.actions.append(args[1])
        return SimpleNamespace(returncode=0, stdout='')


class RuntimeTests(unittest.TestCase):
    def make(self, directory, existing=True, active=False, fail=False):
        root = Path(directory)
        controller = Controller(root, active, fail)
        installer = module.RuntimeInstaller(controller)
        if existing:
            installer.directory.mkdir()
            installer.binary.write_bytes(elf(b'old'))
            installer.binary.chmod(0o755)
            (installer.directory / 'wwwroot').mkdir()
            (installer.directory / 'wwwroot/index.html').write_bytes(b'old page')
            (root / 'data').mkdir()
            (root / 'data/config.json').write_bytes(b'original config')
            (root / 'units/clouddrive2.service').write_bytes(b'original unit')
        data = archive_bytes()
        release = {'version': '1.1.2', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                   'url': 'https://github.com/cloud-fs/cloud-fs.github.io/releases/download/v1.1.2/a.tgz'}
        return installer, controller, release, data

    def install(self, installer, release, data, progress=lambda *args: None):
        with patch.object(module.platform, 'machine', return_value='aarch64'), patch.object(module.platform, 'system', return_value='Linux'), patch.object(module, '_open', return_value=io.BytesIO(data)):
            return installer.install(release, progress)

    def test_first_install_restores_stopped_and_does_not_enable_autostart(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, data = self.make(d, existing=False)
            self.install(i, r, data)
            self.assertTrue(i.installed())
            self.assertEqual(i.local_version(), '1.1.2')
            self.assertFalse(c.active)
            self.assertFalse(c.enabled)
            self.assertEqual(c.actions, ['start', 'stop'])

    def test_success_preserves_active_state_and_existing_unit(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, data = self.make(d, active=True)
            self.install(i, r, data)
            self.assertTrue(c.active)
            self.assertEqual((Path(d) / 'units/clouddrive2.service').read_bytes(), b'original unit')
            self.assertEqual(i.binary.read_bytes(), elf(b'new'))

    def test_failed_start_restores_program_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, data = self.make(d, fail=True)
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                self.install(i, r, data)
            self.assertEqual(i.binary.read_bytes(), elf(b'old'))
            self.assertEqual((Path(d) / 'data/config.json').read_bytes(), b'original config')
            self.assertEqual((i.directory / 'wwwroot/index.html').read_bytes(), b'old page')
            self.assertFalse(c.active)

    def test_failed_first_install_removes_created_program_config_and_unit(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, data = self.make(d, existing=False, fail=True)
            with self.assertRaises(RuntimeError):
                self.install(i, r, data)
            self.assertFalse(i.directory.exists())
            self.assertFalse((Path(d) / 'data').exists())
            self.assertFalse((Path(d) / 'units/clouddrive2.service').exists())

    def test_bad_digest_never_stops_current_service(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, data = self.make(d, active=True)
            r['sha256'] = '0' * 64
            with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
                self.install(i, r, data)
            self.assertEqual(c.actions, [])
            self.assertEqual(i.binary.read_bytes(), elf(b'old'))

    def test_cancel_after_swap_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, data = self.make(d)
            def progress(percent, message):
                if percent == 95:
                    raise RuntimeError('cancelled')
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                self.install(i, r, data, progress)
            self.assertEqual(i.binary.read_bytes(), elf(b'old'))
            self.assertEqual((Path(d) / 'data/config.json').read_bytes(), b'original config')

    def test_rollback_failure_keeps_recovery_files(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, data = self.make(d, fail=True)
            with patch.object(c, 'stop', side_effect=RuntimeError('cannot stop')):
                with self.assertRaisesRegex(RuntimeError, '回退文件已保留'):
                    self.install(i, r, data)
            recovery = list((Path(d) / 'state').glob('runtime-recovery-*'))
            self.assertEqual(len(recovery), 1)
            self.assertEqual((recovery[0] / 'old-program/clouddrive').read_bytes(), elf(b'old'))
            self.assertEqual((recovery[0] / 'data-snapshot/config.json').read_bytes(), b'original config')

    def test_archive_rejects_traversal_links_wrong_arch_and_missing_web(self):
        variants = [archive_bytes([('release/clouddrive', elf()), ('release/../evil', b'evil')]),
                    archive_bytes([('release/clouddrive', b'wrong architecture'), ('release/wwwroot/index.html', b'page')]),
                    archive_bytes([('release/clouddrive', elf())])]
        for data in variants:
            with tempfile.TemporaryDirectory() as d:
                i, c, r, _ = self.make(d)
                p = Path(d) / 'bad.tgz'
                p.write_bytes(data)
                with self.assertRaises(RuntimeError):
                    i._extract(p, Path(d) / 'staged')
        with tempfile.TemporaryDirectory() as d:
            i, c, r, _ = self.make(d)
            p = Path(d) / 'link.tgz'
            with tarfile.open(p, 'w:gz') as archive:
                program = tarfile.TarInfo('clouddrive')
                program.size = len(elf())
                archive.addfile(program, io.BytesIO(elf()))
                link = tarfile.TarInfo('wwwroot/evil')
                link.type = tarfile.SYMTYPE
                link.linkname = '/tmp/evil'
                archive.addfile(link)
            with self.assertRaises(RuntimeError):
                i._extract(p, Path(d) / 'staged')

    def test_latest_selects_linux_asset_and_requires_sha256(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, _ = self.make(d)
            metadata = {'tag_name': 'v1.1.2', 'assets': [
                {'name': 'clouddrive-2-linux-aarch64-1.1.2.tgz', 'size': 100,
                 'digest': 'sha256:' + '1' * 64, 'browser_download_url': r['url']}]}
            with patch.object(module, '_open', return_value=io.BytesIO(json.dumps(metadata).encode())):
                self.assertEqual(i.latest()['version'], '1.1.2')
            metadata['assets'][0]['digest'] = None
            with patch.object(module, '_open', return_value=io.BytesIO(json.dumps(metadata).encode())):
                with self.assertRaises(RuntimeError):
                    i.latest()

    def test_manual_change_invalidates_recorded_version(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, data = self.make(d)
            self.install(i, r, data)
            i.binary.write_bytes(elf(b'manually replaced'))
            self.assertIsNone(i.local_version())

    def test_other_architecture_is_rejected_before_network_or_changes(self):
        with tempfile.TemporaryDirectory() as d:
            i, c, r, _ = self.make(d)
            with patch.object(module.platform, 'machine', return_value='x86_64'):
                with self.assertRaisesRegex(RuntimeError, 'aarch64'):
                    i.install(r)
            self.assertEqual(c.actions, [])

class MenuTests(unittest.TestCase):
    def load_manager(self, dialog, installed=True, current='1.1.2'):
        import sys
        from unittest.mock import Mock
        runtime = Mock()
        runtime.installed.return_value = installed
        runtime.local_version.return_value = current
        runtime.latest.return_value = {'version': '1.1.2', 'size': 1024}
        runtime.needs_update.return_value = not installed
        controller = Mock()
        controller.state.return_value = {}
        controller.is_active.return_value = False
        controller.is_enabled.return_value = False
        config = {'addon_id': 'script.clouddrive2', 'name': 'CloudDrive2', 'password_reset': False}
        modules = {'xbmc': SimpleNamespace(Monitor=lambda: SimpleNamespace(abortRequested=lambda: False)),
                   'xbmcaddon': SimpleNamespace(Addon=lambda aid: Mock()),
                   'xbmcgui': SimpleNamespace(Dialog=lambda: dialog, WindowXMLDialog=object, DialogProgressBG=lambda: Mock()),
                   'xbmcvfs': SimpleNamespace(), 'service_config': SimpleNamespace(CONFIG=config),
                   'service_control': SimpleNamespace(ServiceController=lambda cfg: controller),
                   'runtime_installer': SimpleNamespace(RuntimeInstaller=lambda controller: runtime)}
        spec = importlib.util.spec_from_file_location('manager_menu_test', ROOT / 'addons/script.clouddrive2/resources/lib/manager.py')
        manager = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(manager)
        return manager, runtime

    def test_missing_program_menu_has_install_and_does_not_download_on_open(self):
        from unittest.mock import Mock
        dialog = Mock()
        dialog.select.return_value = -1
        manager, runtime = self.load_manager(dialog, installed=False, current=None)
        manager.run()
        self.assertEqual(dialog.select.call_args.args[1], ['安装主程序', '查看运行日志'])
        runtime.latest.assert_not_called()
        runtime.install.assert_not_called()

    def test_existing_menu_has_update_entry_and_shows_version(self):
        from unittest.mock import Mock
        dialog = Mock()
        dialog.select.return_value = -1
        manager, runtime = self.load_manager(dialog)
        manager.run()
        self.assertIn('检查主程序更新', dialog.select.call_args.args[1])
        self.assertIn('1.1.2', dialog.select.call_args.args[0])
        runtime.latest.assert_not_called()

    def test_current_version_does_not_request_install(self):
        from unittest.mock import Mock
        dialog = Mock()
        manager, runtime = self.load_manager(dialog)
        manager._check_runtime(runtime, dialog)
        dialog.yesno.assert_not_called()
        runtime.install.assert_not_called()

    def test_declining_install_leaves_program_untouched(self):
        from unittest.mock import Mock
        dialog = Mock()
        dialog.yesno.return_value = False
        manager, runtime = self.load_manager(dialog, installed=False, current=None)
        manager._check_runtime(runtime, dialog)
        runtime.install.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
