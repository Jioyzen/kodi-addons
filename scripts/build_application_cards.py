#!/usr/bin/env python3
"""Build seam-free landscape cards from local application icons (Pillow).

Usage: build_application_cards.py ICON_DIRECTORY OUTPUT_DIRECTORY
Input filenames are the add-on IDs with a .png/.jpg suffix. Central artwork is
preserved; edge pixels expand into the extra space and soften with distance.
This developer tool does not run during Kodi navigation or request the network.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps


def background_color(icon):
    small = icon.convert('RGBA').resize((48, 48))
    edge = [small.getpixel((x, y)) for y in range(48) for x in range(48)
            if min(x, y, 47-x, 47-y) < 5]
    solid = [p[:3] for p in edge if p[3] > 200]
    if len(solid) < len(edge) // 3:
        return (62, 74, 87)
    bucket = Counter(tuple(v // 24 for v in p) for p in solid).most_common(1)[0][0]
    values = [p for p in solid if tuple(v // 24 for v in p) == bucket]
    return tuple(round(sum(p[c] for p in values) / len(values)) for c in range(3))


def extend_icon(icon, width=640, height=360, square=280):
    if not 0 < square <= min(width, height)-2:
        raise ValueError('Central artwork must fit inside the card')
    color = background_color(icon)
    center = Image.new('RGB', (square, square), color)
    art = ImageOps.contain(icon.convert('RGBA'), (square, square), Image.Resampling.LANCZOS)
    center.paste(art, ((square-art.width)//2, (square-art.height)//2), art)
    x0, y0 = (width-square)//2, (height-square)//2
    x1, y1 = x0+square, y0+square
    canvas = Image.new('RGB', (width, height))
    canvas.paste(center, (x0, y0))
    # Clamp extension uses the exact nearest boundary pixel, not an average color.
    canvas.paste(center.crop((0, 0, 1, square)).resize((x0, square)), (0, y0))
    canvas.paste(center.crop((square-1, 0, square, square)).resize((width-x1, square)), (x1, y0))
    canvas.paste(center.crop((0, 0, square, 1)).resize((square, y0)), (x0, 0))
    canvas.paste(center.crop((0, square-1, square, square)).resize((square, height-y1)), (x0, y1))
    for rect, point in [((0, 0, x0, y0), (0, 0)),
                        ((x1, 0, width, y0), (square-1, 0)),
                        ((0, y1, x0, height), (0, square-1)),
                        ((x1, y1, width, height), (square-1, square-1))]:
        canvas.paste(center.getpixel(point), rect)
    blur = canvas.filter(ImageFilter.GaussianBlur(22))
    # Blur starts at zero at the icon boundary and grows into the extension.
    # It never changes pixels of the central artwork or creates a square seam.
    blend = Image.new('L', (width, height))
    blend.putdata([min(255, round(max(x0-x, x-(x1-1), y0-y, y-(y1-1), 0)*255/72))
                   for y in range(height) for x in range(width)])
    return Image.composite(blur, canvas, blend)


def build(source, output):
    output.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for path in sorted(source.iterdir()):
        if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            continue
        with Image.open(path) as icon:
            card = extend_icon(icon)
        name = path.stem + '.png'
        card.save(output / name, optimize=True)
        manifest[path.stem] = {'file': name, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('icons', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build(args.icons, args.output)
