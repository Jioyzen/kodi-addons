#!/usr/bin/env python3
"""Check C++ fmt placeholders even when PO entries lack format flags."""
import argparse
import ast
import re
import string
from collections import Counter
from pathlib import Path


def catalog(path):
    entry = {}
    current = None
    for line in path.read_text(encoding='utf-8-sig').splitlines() + ['']:
        if not line.strip():
            if 'msgctxt' in entry:
                yield entry
            entry, current = {}, None
        elif line.startswith('#'):
            continue
        elif line.startswith('"') and current:
            entry[current] += ast.literal_eval(line)
        else:
            match = re.match(r'(msgctxt|msgid|msgid_plural|msgstr(?:\[\d+\])?)\s+(".*")$', line)
            if match:
                current = match.group(1)
                entry[current] = ast.literal_eval(match.group(2))


def tokens(text):
    result = []
    automatic = 0
    for literal, field, spec, conversion in string.Formatter().parse(text):
        if field is not None:
            if field == '':
                field = str(automatic)
                automatic += 1
            result.append((field, spec, conversion))
            if '{' in spec:
                result.extend(tokens(spec))
    return Counter(result)


def errors(path):
    failures = []
    for entry in catalog(path):
        source = entry.get('msgid', '')
        if not re.search(r'\{(?:\d+[^}]*|)\}', source):
            continue
        try:
            expected = tokens(source)
        except ValueError:
            continue
        for key, translation in entry.items():
            if not key.startswith('msgstr') or not translation:
                continue
            try:
                actual = tokens(translation)
            except ValueError as error:
                failures.append((entry['msgctxt'], str(error), source, translation))
                continue
            if actual != expected:
                failures.append((entry['msgctxt'], 'placeholder mismatch', source, translation))
    return failures


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('catalog', type=Path)
    args = parser.parse_args()
    failures = errors(args.catalog)
    for identifier, reason, source, translation in failures:
        print(f'{identifier}: {reason}\n  source: {source}\n  translation: {translation}')
    if failures:
        raise SystemExit(1)
    print('C++ fmt placeholders checked successfully')
