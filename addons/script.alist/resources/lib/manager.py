"""Kodi UI for one independently packaged CoreELEC service."""
import os
import sys
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
from service_config import CONFIG
from service_control import ServiceController


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


def run():
    addon = xbmcaddon.Addon(CONFIG['addon_id'])
    controller = ServiceController(CONFIG)
    dialog = xbmcgui.Dialog()
    while not xbmc.Monitor().abortRequested():
        try:
            state = controller.state()
            active = controller.is_active(state)
            enabled = controller.is_enabled(state)
            running = '运行中' if active else '启动失败' if state.get('ActiveState') == 'failed' else '未启动'
            if active and not controller.ready():
                running += '（管理端口未就绪）'
            title = '%s · %s · 自启%s' % (CONFIG['name'], running, '开启' if enabled else '关闭')
            entries = ['管理后台 / 登录', '停止服务' if active else '启动服务',
                       '关闭开机自启' if enabled else '开启开机自启', '重启服务', '查看运行日志']
            if CONFIG['password_reset']:
                entries.append('重置管理员密码')
            choice = dialog.select(title, entries)
            if choice < 0:
                return
            if choice == 0:
                _show_admin(controller, addon)
            elif choice == 1:
                _operation(controller, '正在停止服务…' if active else '正在启动服务…',
                           controller.stop if active else controller.start)
            elif choice == 2:
                _operation(controller, '正在更新开机自启设置…',
                           lambda: controller.set_autostart(not enabled))
            elif choice == 3:
                _operation(controller, '正在重启服务…', controller.restart)
            elif choice == 4:
                dialog.textviewer(CONFIG['name'] + ' 运行日志', controller.logs())
            elif choice == 5 and CONFIG['password_reset']:
                password = dialog.input('新的管理员密码', option=xbmcgui.ALPHANUM_HIDE_INPUT)
                if password:
                    _operation(controller, '正在更新管理员密码…',
                               lambda: controller.reset_password(password))
                    dialog.ok(CONFIG['name'], '管理员密码已更新。账号：admin')
        except Exception as error:
            dialog.ok(CONFIG['name'], str(error))
