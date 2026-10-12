#!/usr/bin/env python3
"""Validate source and build a reproducible Kodi repository (stdlib only)."""
import argparse
import hashlib
import html
import json
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TOKEN = re.compile(r'(?:sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,})')
SKIP_DIRS = {'__pycache__', '.git', '.idea', '.vscode', 'test', 'tests'}
SKIP_FILES = {'dev_deploy.py', 'deploy_dev.ps1', 'dev_deploy.ps1', '.DS_Store'}


def source_files(folder):
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Symlink not allowed: {path}')
        parts = path.relative_to(folder).parts
        if any(p in SKIP_DIRS or p.startswith('.') for p in parts):
            continue
        if path.is_file() and path.name not in SKIP_FILES and path.suffix not in ('.pyc', '.pyo'):
            yield path


def validate(folder):
    manifest = ET.parse(folder / 'addon.xml').getroot()
    if manifest.tag != 'addon' or manifest.get('id') != folder.name:
        raise ValueError(f'Addon ID/directory mismatch: {folder}')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', manifest.get('version', '')):
        raise ValueError(f'Use a numeric release version: {folder.name}')
    for dependency in manifest.findall('./requires/import'):
        if dependency.get('addon') == 'script.module.cebox':
            raise ValueError('Removed CEBOX dependency reintroduced')
    for path in source_files(folder):
        data = path.read_bytes()
        if path.suffix == '.py':
            compile(data, str(path), 'exec')
        if path.suffix == '.xml':
            parsed = ET.fromstring(data)
            if path.name == 'settings.xml':
                for setting in parsed.findall('.//setting'):
                    if setting.get('id') == 'ai_api_key' and setting.findtext('default'):
                        raise ValueError(f'AI API-key default must be empty: {path}')
        if path.suffix in ('.py', '.xml', '.json', '.md', '.txt', '.sh', '.service'):
            text = data.decode('utf-8-sig')
            if PRIVATE_TOKEN.search(text):
                raise ValueError(f'Private API token found: {path}')
            if re.search(r'(?:192\.168\.2\.|/home/zeng/)', text):
                raise ValueError(f'Private environment value found: {path}')
            if 'cebox_db' in text or 'cebox_tools' in text:
                raise ValueError(f'Legacy common-module import found: {path}')
        if path.name in ('scan_history.json', 'sources.xml', 'advancedsettings.xml', 'guisettings.xml'):
            raise ValueError(f'Userdata must not be packaged: {path}')
        if path.name == 'settings.xml' and path.parent == folder:
            raise ValueError(f'User settings must not be packaged: {path}')
    for ext in manifest.findall('extension'):
        library = ext.get('library')
        if library and not (folder / library).exists():
            raise ValueError(f'Missing entry point: {folder.name}/{library}')
    for asset in manifest.findall("extension[@point='xbmc.addon.metadata']/assets/*"):
        if asset.text and not (folder / asset.text).is_file():
            raise ValueError(f'Missing artwork: {folder.name}/{asset.text}')
    return manifest


def build(source, output):
    folders = sorted(p for p in source.iterdir() if p.is_dir())
    manifests = [(p, validate(p)) for p in folders]
    if not manifests:
        raise ValueError('No add-ons to publish')
    # Validation happens before deleting the previous generated site.
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / '.nojekyll').touch()
    (output / '.gitkeep').touch()
    index = ET.Element('addons')
    rows = []
    for folder, manifest in manifests:
        aid, version = manifest.get('id'), manifest.get('version')
        target = output / aid
        target.mkdir()
        archive = target / f'{aid}-{version}.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as package:
            for path in source_files(folder):
                info = zipfile.ZipInfo(str(Path(aid) / path.relative_to(folder)), (2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                package.writestr(info, path.read_bytes())
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix('.zip.sha256').write_text(digest + '\n', encoding='ascii')
        for asset in manifest.findall("extension[@point='xbmc.addon.metadata']/assets/*"):
            if asset.text:
                destination = target / asset.text
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(folder / asset.text, destination)
        index.append(manifest)
        rows.append({'id': aid, 'name': manifest.get('name'), 'version': version,
                     'path': str(archive.relative_to(output)), 'sha256': digest})
    ET.indent(index, space='  ')
    data = ET.tostring(index, encoding='utf-8', xml_declaration=True) + b'\n'
    (output / 'addons.xml').write_bytes(data)
    for algorithm in ('md5', 'sha256'):
        digest = hashlib.new(algorithm, data).hexdigest()
        (output / ('addons.xml.' + algorithm)).write_text(digest + '\n', encoding='ascii')
    (output / 'manifest.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    table = ''.join(f'<tr><td>{html.escape(r["name"])}</td><td>{r["version"]}</td>'
                    f'<td><a href="{r["path"]}">下载 ZIP</a> · <a href="{r["id"]}/">浏览目录</a></td></tr>' for r in rows)
    repo = next(r for r in rows if r['id'] == 'repository.jioyzen')
    (output / 'index.html').write_text('''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jioyzen Kodi 插件仓库</title>
<style>body{font:18px/1.7 system-ui;max-width:960px;margin:40px auto;padding:0 20px;background:#141b26;color:#eee}a{color:#ff8b99}table{width:100%;border-collapse:collapse}td,th{padding:12px;text-align:left;border-bottom:1px solid #445}code{overflow-wrap:anywhere}</style>
<h1>Jioyzen Kodi 插件仓库</h1><p>适配并验证于 Kodi 22 / CoreELEC Amlogic-no。</p>
<p><a href="''' + repo['path'] + '''">下载仓库插件</a>，在 Kodi 中通过“从 ZIP 文件安装”安装，然后进入“从仓库安装 → Jioyzen Kodi Add-ons”。</p>
<p>也可添加文件源：<code>https://jioyzen.github.io/kodi-addons/</code>，浏览下方插件目录安装 ZIP。</p>
<table><tr><th>插件</th><th>版本</th><th>下载</th></tr>''' + table + '''</table>
<p>Alist / OpenList / CloudDrive2 管理插件可手动安装、检查并更新官方 Linux aarch64 主程序。已有配置继续保留。</p>
<p><a href="https://github.com/Jioyzen/kodi-addons">源码与使用说明</a> · <a href="manifest.json">版本与 SHA-256 清单</a></p></html>''', encoding='utf-8')
    # Kodi's HTTP file-source browser needs folder listings as well.
    for row in rows:
        (output / row['id'] / 'index.html').write_text(
            f'<!doctype html><meta charset="utf-8"><a href="{Path(row["path"]).name}">{Path(row["path"]).name}</a>', encoding='utf-8')
    print(f'Validated and packaged {len(rows)} add-ons into {output}')
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'docs')
    arguments = parser.parse_args()
    build(ROOT / 'addons', arguments.output.resolve())
