#!/usr/bin/env python3
"""Require a version bump whenever an existing add-on changes."""
import argparse
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--base', required=True)
args = parser.parse_args()
if not args.base or set(args.base) == {'0'}:
    raise SystemExit(0)
if not re.fullmatch(r'[a-fA-F0-9]{7,40}', args.base):
    raise SystemExit('Invalid base revision')
root = Path(__file__).resolve().parents[1]
changes = subprocess.check_output(['git', 'diff', '--name-only', args.base, 'HEAD', '--', 'addons/'], cwd=root, text=True)
ids = {p.split('/')[1] for p in changes.splitlines() if len(p.split('/')) > 2}
for aid in sorted(ids):
    relative = f'addons/{aid}/addon.xml'
    old = subprocess.run(['git', 'show', f'{args.base}:{relative}'], cwd=root, capture_output=True)
    if old.returncode:
        continue
    current = root / relative
    if not current.is_file():
        raise SystemExit(f'Do not silently remove a published add-on: {aid}')
    before = ET.fromstring(old.stdout).get('version')
    after = ET.parse(current).getroot().get('version')
    numeric = lambda v: tuple(int(n) for n in v.split('.'))
    if numeric(after) <= numeric(before):
        raise SystemExit(f'{aid} changed: bump addon.xml version above {before}')
print('Changed add-on versions are increasing')
