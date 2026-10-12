"""Bounded systemd operations for a standalone CoreELEC service manager."""
import json
import os
import socket
import subprocess
import tempfile
import time


class ServiceController:
    def __init__(self, config):
        self.config = dict(config)
        self.unit = self.config['unit']

    def _run(self, arguments, timeout=25):
        try:
            return subprocess.run(arguments, capture_output=True,
                                  encoding='utf-8', errors='replace', timeout=timeout)
        except FileNotFoundError:
            raise RuntimeError('当前系统缺少所需的服务管理命令。') from None
        except subprocess.TimeoutExpired:
            raise RuntimeError('服务操作超时，请查看运行日志确认当前状态。') from None

    def state(self):
        result = self._run(['systemctl', 'show', self.unit,
                            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState',
                            '-p', 'UnitFileState', '-p', 'Result'], timeout=8)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines()
                      if '=' in line)
        if result.returncode and not values:
            raise RuntimeError(result.stderr.strip() or '无法读取服务状态。')
        return values

    @staticmethod
    def is_active(state):
        return state.get('ActiveState') == 'active'

    @staticmethod
    def is_enabled(state):
        return state.get('UnitFileState') in ('enabled', 'enabled-runtime')

    def endpoint(self):
        port, scheme = self.config['port'], 'http'
        if self.config.get('password_reset'):
            path = os.path.join(self.config['data_dir'], 'config.json')
            try:
                with open(path, encoding='utf-8') as source:
                    settings = json.load(source).get('scheme', {})
                tls = settings.get('force_https')
                configured = settings.get('https_port' if tls else 'http_port')
                if isinstance(configured, int) and not isinstance(configured, bool) and 0 < configured < 65536:
                    port, scheme = configured, 'https' if tls else 'http'
            except (OSError, ValueError, AttributeError):
                pass
        return scheme, port

    def ready(self):
        try:
            with socket.create_connection(('127.0.0.1', self.endpoint()[1]), timeout=.5):
                return True
        except OSError:
            return False

    def _ensure_unit(self):
        state = self.state()
        if state.get('LoadState') == 'loaded':
            return
        if state.get('LoadState') not in ('not-found', None):
            raise RuntimeError('服务配置未正常加载，请查看运行日志。')
        self.install_unit()

    def install_unit(self):
        """Install a missing unit; preserve an existing administrator's unit."""
        binary = self.config['binary']
        if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
            raise RuntimeError('后台程序尚未安装：' + self.config['name'])
        unit_directory = self.config.get('unit_directory', '/storage/.config/system.d')
        if not os.path.isdir(unit_directory):
            raise RuntimeError('当前系统未找到 CoreELEC 服务配置目录。')
        target = os.path.join(unit_directory, self.unit)
        if os.path.lexists(target):
            raise RuntimeError('已有服务配置无法加载，保留原文件，请检查配置。')
        os.makedirs(self.config['data_dir'], mode=0o700, exist_ok=True)
        addon_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        template = os.path.join(addon_root, 'resources', 'systemd', self.unit)
        with open(template, encoding='utf-8') as source:
            contents = source.read()
        fd, temporary = tempfile.mkstemp(prefix='.manager-unit-', dir=unit_directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                output.write(contents)
            # Hard-link without overwriting a unit installed concurrently.
            os.link(temporary, target)
        finally:
            os.unlink(temporary)
        result = self._run(['systemctl', 'daemon-reload'])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or '无法加载服务配置。')

    def _command(self, action):
        if action not in ('start', 'stop', 'restart', 'enable', 'disable'):
            raise ValueError('Unsupported service operation')
        result = self._run(['systemctl', action, self.unit])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or '服务操作失败，请查看运行日志。')

    def _wait_ready(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state.get('ActiveState') == 'failed':
                raise RuntimeError('后台进程启动失败，请查看运行日志。')
            if self.is_active(state) and self.ready():
                return
            time.sleep(.2)
        raise RuntimeError('后台管理端口尚未就绪，请查看运行日志。')

    def start(self):
        self._ensure_unit()
        self._command('start')
        self._wait_ready()

    def stop(self):
        self._command('stop')
        state = self.state()
        if state.get('ActiveState') not in ('inactive', 'failed'):
            raise RuntimeError('服务尚未停止，请查看运行日志。')

    def restart(self):
        self._ensure_unit()
        self._command('restart')
        self._wait_ready()

    def set_autostart(self, enabled):
        if enabled:
            self._ensure_unit()
        self._command('enable' if enabled else 'disable')
        if self.is_enabled(self.state()) != bool(enabled):
            raise RuntimeError('开机自启状态未达到预期，请检查服务配置。')

    def logs(self):
        result = self._run(['journalctl', '-u', self.unit, '-n', '50', '--no-pager'], timeout=8)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or '无法读取运行日志。')
        return result.stdout.strip() or '暂无运行日志。'

    def reset_password(self, password):
        if not self.config.get('password_reset'):
            raise RuntimeError('此服务不支持重置管理员密码。')
        if len(password) < 8:
            raise RuntimeError('密码请至少使用 8 个字符。')
        was_active = self.is_active(self.state())
        if was_active:
            self.stop()
        try:
            result = self._run([self.config['binary'], 'admin', 'set', password,
                                '--data', self.config['data_dir']])
            if result.returncode:
                # Do not expose a password in CLI output or diagnostic logs.
                raise RuntimeError('管理员密码更新失败。')
        finally:
            if was_active:
                self.start()
