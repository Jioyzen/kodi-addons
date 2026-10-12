"""Kodi UI for one independently packaged CoreELEC service."""
import os
import sys
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
from service_config import CONFIG
from service_control import ServiceController
from runtime_installer import RuntimeInstaller


def _operation(controller, name, function):
    progress = xbmcgui.DialogProgressBG()
    progress.create(CONFIG['name'], name)
    try:
        function()
    finally:
        progress.close()


def _show_admin(controller, addon):
    if not controller.is_active(controller.state()):
        xbmcgui.Dialog().ok(CONFIG['name'], '请先启动服务，再打开管理后台。')
        return
    if not controller.ready():
        xbmcgui.Dialog().ok(CONFIG['name'], '管理端口尚未就绪，请稍后重试或查看运行日志。')
        return
    ip = xbmc.getIPAddress()
    if not ip or ip in ('0.0.0.0', '127.0.0.1'):
        xbmcgui.Dialog().ok(CONFIG['name'], '尚未获取局域网地址，请检查网络连接。')
        return
    scheme, port = controller.endpoint()
    url = '%s://%s:%d' % (scheme, '[' + ip + ']' if ':' in ip else ip, port)
    profile = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    os.makedirs(profile, exist_ok=True)
    vendor = os.path.join(os.path.dirname(__file__), 'vendor')
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    try:
        import segno
        png = os.path.join(profile, 'local-qr.png')
        segno.make(url, micro=False).save(png, scale=8, border=3)
    except Exception:
        xbmcgui.Dialog().ok(CONFIG['name'], '管理后台地址：\n' + url)
        return

    class AdminDialog(xbmcgui.WindowXMLDialog):
        def onInit(self):
            self.getControl(100).setImage(png)
            labels = {
                101: CONFIG['name'] + ' 管理后台', 102: '后台地址：' + url,
                103: '请使用手机浏览器扫码进入',
                104: '账号：admin' if CONFIG['password_reset'] else '请登录自己的 CloudDrive 账号',
                105: '可在管理菜单中重置管理员密码' if CONFIG['password_reset'] else '',
                109: '注意事项：', 110: '手机与盒子需要位于同一局域网',
                111: '升级管理插件不会清除云盘配置',
            }
            for identifier, label in labels.items():
                self.getControl(identifier).setLabel(label)

        def onAction(self, action):
            if action.getId() in (9, 10, 92):
                self.close()

        def onClick(self, control):
            if control == 120:
                self.close()

    # Unique filename avoids loading the legacy root-level test window.
    window = AdminDialog('manager_qr.xml', addon.getAddonInfo('path'), 'Default', '1080i')
    try:
        window.doModal()
    finally:
        del window



def _runtime_operation(installer, release):
    progress = xbmcgui.DialogProgress()
    progress.create(CONFIG['name'], '正在准备安装主程序…')
    monitor = xbmc.Monitor()
    def update(percent, message):
        if monitor.abortRequested() or progress.iscanceled():
            raise RuntimeError('安装已取消；若已开始替换，会先恢复旧版本。')
        progress.update(percent, message)
    try:
        installer.install(release, update)
    finally:
        progress.close()


def _check_runtime(installer, dialog):
    _progress = xbmcgui.DialogProgressBG()
    _progress.create(CONFIG['name'], '正在查询官方稳定版…')
    try:
        release = installer.latest()
    finally:
        _progress.close()
    current = installer.local_version()
    installed = installer.installed()
    if installed and not installer.needs_update(release):
        dialog.ok(CONFIG['name'], '当前主程序：' + current + '\n官方稳定版：' + release['version'] + '\n当前版本已是最新或更新。')
        return
    detail = ('当前主程序：' + (current or '版本未知') + '\n') if installed else '尚未安装完整主程序。\n'
    detail += '官方稳定版：' + release['version'] + '\n下载大小：%.1f MB\n' % (release['size'] / 1024 / 1024)
    detail += '保留现有配置与自启选择；完成后保持原来的运行状态。'
    if not dialog.yesno(CONFIG['name'], detail, nolabel='取消', yeslabel='更新主程序' if installed else '安装主程序'):
        return
    _runtime_operation(installer, release)
    dialog.ok(CONFIG['name'], '主程序 ' + release['version'] + ' 安装完成。\n原先未运行的服务仍保持停止，可在管理菜单中启动。')


def run():
    addon = xbmcaddon.Addon(CONFIG['addon_id'])
    controller = ServiceController(CONFIG)
    installer = RuntimeInstaller(controller)
    dialog = xbmcgui.Dialog()
    while not xbmc.Monitor().abortRequested():
        try:
            state = controller.state()
            active = controller.is_active(state)
            enabled = controller.is_enabled(state)
            installed = installer.installed()
            version = installer.local_version()
            running = '运行中' if active else '启动失败' if state.get('ActiveState') == 'failed' else '未启动'
            if active and not controller.ready():
                running += '（管理端口未就绪）'
            title = '%s · %s · 自启%s' % (CONFIG['name'], running, '开启' if enabled else '关闭')
            title += ' · 主程序 ' + (version or '版本未知') if installed else ' · 主程序未安装'
            if installed:
                entries = [('admin', '管理后台 / 登录'), ('toggle', '停止服务' if active else '启动服务'),
                           ('autostart', '关闭开机自启' if enabled else '开启开机自启'),
                           ('restart', '重启服务'), ('update', '检查主程序更新'), ('logs', '查看运行日志')]
                if CONFIG['password_reset']:
                    entries.append(('password', '重置管理员密码'))
            else:
                entries = [('install', '安装主程序'), ('logs', '查看运行日志')]
            choice = dialog.select(title, [label for action, label in entries])
            if choice < 0:
                return
            action = entries[choice][0]
            if action == 'admin':
                _show_admin(controller, addon)
            elif action == 'toggle':
                _operation(controller, '正在停止服务…' if active else '正在启动服务…',
                           controller.stop if active else controller.start)
            elif action == 'autostart':
                _operation(controller, '正在更新开机自启设置…',
                           lambda: controller.set_autostart(not enabled))
            elif action == 'restart':
                _operation(controller, '正在重启服务…', controller.restart)
            elif action in ('install', 'update'):
                _check_runtime(installer, dialog)
            elif action == 'logs':
                dialog.textviewer(CONFIG['name'] + ' 运行日志', controller.logs())
            elif action == 'password':
                password = dialog.input('新的管理员密码', option=xbmcgui.ALPHANUM_HIDE_INPUT)
                if password:
                    _operation(controller, '正在更新管理员密码…',
                               lambda: controller.reset_password(password))
                    dialog.ok(CONFIG['name'], '管理员密码已更新。账号：admin')
        except Exception as error:
            dialog.ok(CONFIG['name'], str(error))
