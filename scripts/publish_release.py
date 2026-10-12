#!/usr/bin/env python3
"""Publish built packages; never replace assets on an existing release."""
import argparse
import json
import os
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--directory', type=Path, required=True)
args = parser.parse_args()
revision = os.environ['GITHUB_SHA']
tag = 'addons-' + revision[:12]
repository = os.environ['GITHUB_REPOSITORY']
rows = json.loads((args.directory / 'manifest.json').read_text(encoding='utf-8'))
notes = args.directory / 'release-notes.md'
notes.write_text('适配 Kodi 22 / CoreELEC Amlogic-no 的维护版插件。\n\n'
                 + '\n'.join(f'- {r["name"]}：{r["version"]}' for r in rows)
                 + '\n\n首次使用请安装 repository.jioyzen 的 ZIP，再通过“从仓库安装”安装或更新插件。'
                 + '\n\nAlist / CloudDrive2 为管理插件，后台程序需另行安装；不包含个人配置和 API 密钥。\n', encoding='utf-8')
command = ['gh', 'release', 'view', tag, '--repo', repository]
if subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
    print('Release already exists; leaving published assets unchanged:', tag)
    raise SystemExit(0)
files = sorted(args.directory.rglob('*.zip')) + sorted(args.directory.rglob('*.zip.sha256'))
files += [args.directory / 'manifest.json', args.directory / 'addons.xml',
          args.directory / 'addons.xml.sha256', args.directory / 'addons.xml.md5']
subprocess.run(['gh', 'release', 'create', tag, '--repo', repository, '--target', revision,
                '--title', 'Kodi 插件发布 ' + revision[:12], '--notes-file', str(notes),
                '--latest'] + [str(p) for p in files], check=True)
