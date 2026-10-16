"""Cache lifecycle, scan/cache exclusion and visible task feedback."""
import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'addons/script.ai.movie.scanner/python'
sys.path.insert(0, str(SOURCE))
from task_state import task_lock, cache_task, TaskBusy


class CacheTaskTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.profile = Path(self.folder.name)
        self.notices = []
        self.progress = Mock()
        self.addon = types.SimpleNamespace(
            getAddonInfo=lambda key: str(self.profile) if key == 'profile' else 'AI Scanner',
            getLocalizedString=lambda key: '')
        self.xbmc = types.SimpleNamespace(LOGINFO=1, LOGWARNING=2, LOGERROR=3,
            log=lambda *a: None, Monitor=lambda: types.SimpleNamespace(abortRequested=lambda: False))
        self.gui = types.SimpleNamespace(NOTIFICATION_INFO=1, NOTIFICATION_WARNING=2,
            DialogProgressBG=lambda: self.progress,
            Dialog=lambda: types.SimpleNamespace(notification=lambda *a: self.notices.append(a)))
        stubs = {'xbmc': self.xbmc, 'xbmcgui': self.gui,
                 'xbmcaddon': types.SimpleNamespace(Addon=lambda: self.addon),
                 'xbmcvfs': types.SimpleNamespace(translatePath=lambda p: p),
                 'ai_database': types.SimpleNamespace(current_database=lambda _: ':memory:')}
        spec = importlib.util.spec_from_file_location('cache_task_image', SOURCE/'image_cacher.py')
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(self.module)
        # Use real fallback strings, independent of other tests' Kodi stubs.
        spec = importlib.util.spec_from_file_location('cache_task_strings', SOURCE/'localization.py')
        strings = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'xbmc': self.xbmc}):
            spec.loader.exec_module(strings)
        self.module.L = lambda key: strings.get_string(self.addon, key)
        self.image = self.module.ImageCacher()
        self.addCleanup(self.image.session.close)

    def competitor(self):
        code = ('from task_state import task_lock, TaskBusy\n'
                'try:\n with task_lock(%r):pass\n'
                'except TaskBusy:raise SystemExit(17)' % str(self.profile))
        return subprocess.run([sys.executable, '-c', code],
                              env=dict(os.environ, PYTHONPATH=str(SOURCE))).returncode

    def test_scan_owns_lock_through_cache_and_reports_actual_counts(self):
        def results(urls, cancelled=lambda: False):
            self.assertEqual(len(urls), 3)
            self.assertEqual(self.competitor(), 17)
            self.assertTrue((self.profile/'.caching').exists())
            yield from ('success', 'skipped', 'failed')
        self.image._bounded_results = results
        art = [{'poster': [{'url': 'https://image.test/poster.jpg'}],
                'fanart': [{'url': 'https://image.test/background.jpg'}],
                'cast': [{'url': 'https://image.test/actor.jpg'}]}]
        with task_lock(str(self.profile)):
            report = self.image.prefetch_art(art)
            self.assertEqual(self.competitor(), 17)
        self.assertEqual(self.competitor(), 0)
        self.assertEqual(report, dict(success=1, skipped=1, failed=1, canceled=False))
        self.assertFalse((self.profile/'.caching').exists())
        self.assertIn('Caching 3 images', self.notices[0][1])
        self.assertIn('downloaded 1, already cached 1, failed 1', self.notices[-1][1])
        self.progress.close.assert_called_once()

    def test_cancel_holds_lock_until_inflight_worker_exits(self):
        started = threading.Event()
        release = threading.Event()
        submitted = []
        reports = []
        def download(url):
            submitted.append(url)
            started.set()
            release.wait(3)
            return 'canceled'
        self.image.thread_count = 1
        self.image._download_image = download
        art = [{'poster': [{'url': 'https://image.test/%d.jpg' % n}]} for n in range(10)]
        worker = threading.Thread(target=lambda: reports.append(self.image.prefetch_art(art)))
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            (self.profile/'.stop_requested').touch()
            self.assertTrue(self.image.cancel.wait(2))
            self.assertTrue(worker.is_alive())
            self.assertEqual(self.competitor(), 17)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(submitted), 1)
        self.assertTrue(reports[0]['canceled'])
        self.assertIn('stopped', self.notices[-1][1])
        self.assertFalse((self.profile/'.caching').exists())
        self.assertFalse((self.profile/'.stop_requested').exists())
        self.assertEqual(self.competitor(), 0)

    def test_cache_exception_reports_failure_and_releases_state(self):
        self.image._bounded_results = Mock(side_effect=RuntimeError('download failed'))
        with self.assertRaises(RuntimeError):
            self.image.prefetch_art([{'poster': [{'url': 'https://image.test/poster.jpg'}]}])
        self.assertIn('did not finish', self.notices[-1][1])
        self.assertFalse((self.profile/'.caching').exists())
        self.assertEqual(self.competitor(), 0)
        self.progress.close.assert_called_once()

    def test_no_art_explains_that_no_automatic_cache_is_needed(self):
        self.image._bounded_results = Mock()
        self.image.prefetch_art([])
        self.image._bounded_results.assert_not_called()
        self.assertIn('not needed', self.notices[-1][1])
        self.progress.create.assert_not_called()

    def test_all_task_types_share_cache_lock_across_threads(self):
        outcomes = []
        def request():
            try:
                with task_lock(str(self.profile)):
                    outcomes.append('started')
            except TaskBusy:
                outcomes.append('rejected')
        with cache_task(str(self.profile)):
            threads = [threading.Thread(target=request) for _ in range(5)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        self.assertEqual(outcomes, ['rejected'] * 5)
        request()
        self.assertEqual(outcomes[-1], 'started')

    def test_busy_ui_rejects_duplicate_but_allows_stop_immediately(self):
        tree = ast.parse((SOURCE/'standalone.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef))
        action = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_action')
        ns = {'threading': Mock()}
        exec(compile(ast.Module(body=[action], type_ignores=[]), 'action', 'exec'), ns)
        backend = types.SimpleNamespace(notify_task_busy=Mock(), stop_scan=Mock())
        owner = types.SimpleNamespace(_backend=backend, _action_busy=True,
                                      navigation=types.SimpleNamespace(closed=False))
        for command in ('scan_all', 'scan_directory', 'cache_images', 'cache_actor_images'):
            ns['_action'](owner, {'action': command})
        self.assertEqual(backend.notify_task_busy.call_count, 4)
        ns['_action'](owner, {'action': 'stop_scan'})
        backend.stop_scan.assert_called_once()
        ns['threading'].Thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
