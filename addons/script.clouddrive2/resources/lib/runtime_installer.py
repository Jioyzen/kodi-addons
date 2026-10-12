"""Manual installation/update of official aarch64 runtimes, independent of Kodi."""
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _https(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        raise RuntimeError('下载地址必须使用 HTTPS。')
    return url


def _open(url):
    request = urllib.request.Request(_https(url), headers={
        'User-Agent': 'Jioyzen-CoreELEC-Manager/1.2', 'Accept': 'application/vnd.github+json'})
    try:
        response = urllib.request.urlopen(request, timeout=30)
        _https(response.geturl())
        return response
    except (OSError, urllib.error.URLError, ValueError):
        raise RuntimeError('无法连接官方发布源，请检查网络；稍后可以重新检查。') from None


def _version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value or '')
    return tuple(map(int, match.groups())) if match else None


class RuntimeInstaller:
    def __init__(self, controller):
        self.controller = controller
        self.config = controller.config
        self.binary = Path(self.config['binary'])
        self.directory = self.binary.parent
        self._cached_version = None

    def installed(self):
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            return False
        return self.config['password_reset'] or (self.directory / 'wwwroot/index.html').is_file()

    def local_version(self):
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            return None
        stat = self.binary.stat()
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if self._cached_version and self._cached_version[0] == signature:
            return self._cached_version[1]
        result = None
        if self.config['password_reset']:
            try:
                command = self.controller._run([str(self.binary), 'version'], timeout=5)
                match = re.search(r'^Version:\s*v?(\d+\.\d+\.\d+)\s*$', command.stdout, re.M)
                if command.returncode == 0 and match:
                    result = match.group(1)
            except RuntimeError:
                pass
        else:
            # CloudDrive's CLI has no dependable version-only flag. Do not start it here.
            digest = _sha256(self.binary)
            try:
                with open(self.directory / '.jioyzen-runtime.json', encoding='utf-8') as source:
                    marker = json.load(source)
                if marker.get('binary_sha256') == digest and _version(marker.get('version')):
                    result = marker['version']
            except (OSError, ValueError, AttributeError):
                pass
            if not result:
                result = self.config.get('known_versions', {}).get(digest)
        self._cached_version = (signature, result)
        return result

    def latest(self):
        repository = self.config['release_repository']
        with _open('https://api.github.com/repos/' + repository + '/releases/latest') as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise RuntimeError('官方发布信息过大，已停止处理。')
        try:
            release = json.loads(data)
            tag = release['tag_name']
            if release.get('draft') or release.get('prerelease') or not _version(tag):
                raise ValueError()
            name = self.config['asset_name'].format(version=tag.lstrip('v'))
            asset = next(a for a in release['assets'] if a['name'] == name)
            size = asset['size']
            digest = asset.get('digest', '')
            url = asset['browser_download_url']
            if (not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE
                    or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest)
                    or not url.startswith('https://github.com/' + repository + '/releases/download/')):
                raise ValueError()
        except (KeyError, TypeError, ValueError, StopIteration):
            raise RuntimeError('官方稳定版缺少可校验的 Linux aarch64 安装包，未进行安装。') from None
        return {'version': tag.lstrip('v'), 'url': url, 'size': size,
                'sha256': digest.split(':', 1)[1].lower(), 'name': name}

    def needs_update(self, release):
        current = _version(self.local_version())
        return current is None or current < _version(release['version'])

    def _download(self, release, target, progress):
        started = time.monotonic()
        total = 0
        digest = hashlib.sha256()
        with _open(release['url']) as response, open(target, 'wb') as output:
            while True:
                if time.monotonic() - started > 600:
                    raise RuntimeError('下载超时，旧程序仍保留，请稍后重试。')
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > release['size'] or total > MAX_ARCHIVE:
                    raise RuntimeError('下载文件大小不符，已停止安装。')
                output.write(chunk)
                digest.update(chunk)
                progress(10 + int(50 * total / release['size']), '正在下载官方安装包…')
        if total != release['size'] or digest.hexdigest() != release['sha256']:
            raise RuntimeError('安装包 SHA-256 校验失败，旧程序未替换。')

    def _extract(self, archive, destination):
        destination.mkdir()
        expected = self.binary.name
        total = 0
        seen = set()
        with tarfile.open(archive, 'r:gz') as package:
            members = package.getmembers()
            if len(members) > 20000:
                raise RuntimeError('安装包文件数量异常。')
            files = [m for m in members if m.isfile()]
            roots = [Path(m.name).parent for m in files if Path(m.name).name == expected]
            if len(roots) != 1:
                raise RuntimeError('安装包中未找到唯一的主程序。')
            prefix = roots[0]
            for member in members:
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or not (member.isdir() or member.isfile()):
                    raise RuntimeError('安装包包含不允许的文件路径或链接。')
                if member.isdir():
                    continue
                try:
                    relative = path.relative_to(prefix)
                except ValueError:
                    raise RuntimeError('安装包目录结构异常。') from None
                if relative != Path(expected) and not (
                        not self.config['password_reset'] and relative.parts[0] == 'wwwroot'):
                    raise RuntimeError('安装包包含非预期程序文件，已停止安装。')
                if str(relative) in seen:
                    raise RuntimeError('安装包存在重复文件。')
                seen.add(str(relative))
                total += member.size
                if member.size < 0 or total > MAX_EXPANDED:
                    raise RuntimeError('安装包解压大小异常。')
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, open(target, 'wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if relative == Path(expected) else 0o644)
        binary = destination / expected
        with open(binary, 'rb') as source:
            header = source.read(20)
        if len(header) != 20 or header[:6] != b'\x7fELF\x02\x01' or struct.unpack('<H', header[18:20])[0] != 183:
            raise RuntimeError('主程序不是适配 CoreELEC 的 64 位 ARM Linux 程序。')
        if not self.config['password_reset'] and not (destination / 'wwwroot/index.html').is_file():
            raise RuntimeError('CloudDrive2 安装包缺少配套网页文件。')

    def _snapshot_data(self, source, destination):
        # Never traverse mounted cloud directories or symbolic links in user data.
        if os.path.ismount(source):
            raise RuntimeError('配置目录是挂载点，无法安全建立回退副本。')
        for folder, dirs, files in os.walk(source, followlinks=False):
            for name in dirs + files:
                path = Path(folder) / name
                if path.is_symlink() or os.path.ismount(path):
                    raise RuntimeError('配置目录包含挂载点或链接，无法安全建立回退副本。')
        shutil.copytree(source, destination)

    def install(self, release, progress=lambda percent, message: None):
        if platform.system() != 'Linux' or platform.machine().lower() not in ('aarch64', 'arm64'):
            raise RuntimeError('主程序安装仅支持 aarch64 Linux / CoreELEC。')
        if self.directory.is_symlink() or Path(self.config['data_dir']).is_symlink():
            raise RuntimeError('程序或配置目录为链接，保留现有安装，请手动处理。')
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        state_directory = Path(self.config.get('state_directory',
                               '/storage/.kodi/userdata/addon_data/' + self.config['addon_id']))
        state_directory.mkdir(parents=True, exist_ok=True)
        with open(state_directory / 'runtime-update.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('已有安装或更新正在进行，请等待完成。') from None
            with tempfile.TemporaryDirectory(prefix='.jioyzen-runtime-', dir=self.directory.parent) as temporary:
                temporary = Path(temporary)
                if shutil.disk_usage(temporary).free < MAX_EXPANDED + release['size']:
                    raise RuntimeError('存储空间不足，请至少留出约 1.3 GB 空间再安装。')
                archive = temporary / 'package.tgz'
                self._download(release, archive, progress)
                staged = temporary / 'program'
                self._extract(archive, staged)
                progress(65, '安装包校验完成，准备保留配置…')
                if self.config['password_reset']:
                    result = self.controller._run([str(staged / self.binary.name), 'version'], timeout=8)
                    if result.returncode or not re.search(
                            r'^Version:\s*v?' + re.escape(release['version']) + r'\s*$', result.stdout, re.M):
                        raise RuntimeError('新主程序无法运行或版本不符，旧程序未替换。')
                marker = dict(version=release['version'], binary_sha256=_sha256(staged / self.binary.name),
                              archive_sha256=release['sha256'], source=release['url'])
                (staged / '.jioyzen-runtime.json').write_text(json.dumps(marker), encoding='utf-8')
                state = self.controller.state()
                was_active = self.controller.is_active(state)
                data = Path(self.config['data_dir'])
                unit = Path(self.config.get('unit_directory', '/storage/.config/system.d')) / self.config['unit']
                existed = self.directory.exists()
                data_existed = data.exists()
                unit_existed = os.path.lexists(unit)
                old = temporary / 'old-program'
                snapshot = temporary / 'data-snapshot'
                swapped = False
                snapshotted = False
                try:
                    if was_active:
                        self.controller.stop()
                    if data_existed:
                        self._snapshot_data(data, snapshot)
                        snapshotted = True
                    progress(80, '正在替换程序并检查启动…')
                    if existed:
                        os.replace(self.directory, old)
                    try:
                        os.replace(staged, self.directory)
                        swapped = True
                    except Exception:
                        if existed:
                            os.replace(old, self.directory)
                        raise
                    self.controller.start()
                    progress(95, '启动与管理端口检查通过…')
                    if not was_active:
                        self.controller.stop()
                except Exception:
                    try:
                        if swapped:
                            self.controller.stop()
                            shutil.rmtree(self.directory)
                        if old.exists():
                            os.replace(old, self.directory)
                        if swapped and snapshotted:
                            if data.exists():
                                shutil.rmtree(data)
                            shutil.copytree(snapshot, data)
                        elif swapped and not data_existed and data.exists():
                            shutil.rmtree(data)
                        if swapped and not unit_existed and unit.exists():
                            unit.unlink()
                            self.controller._run(['systemctl', 'daemon-reload'])
                        if was_active:
                            self.controller.start()
                    except Exception:
                        recovery = state_directory / ('runtime-recovery-' + str(time.time_ns()))
                        shutil.move(str(temporary), str(recovery))
                        raise RuntimeError('自动恢复未完成，程序与配置回退文件已保留：' + str(recovery)) from None
                    raise
                self._cached_version = None
        return release['version']
