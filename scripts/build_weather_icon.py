#!/usr/bin/env python3
"""Render the original flat weather icon from code-native vector shapes."""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / 'addons/weather.jioyzen/resources'
SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#287FDB"/><stop offset="1" stop-color="#47B7DC"/></linearGradient></defs>
<rect width="512" height="512" fill="url(#bg)"/>
<circle cx="45" cy="58" r="220" fill="white" opacity=".04"/>
<circle cx="488" cy="474" r="224" fill="white" opacity=".035"/>
<g stroke="#FFD16B" stroke-width="10" stroke-linecap="round">
<path d="M165 96V72 M165 260V284 M83 178H59 M247 178H271 M107 120L90 103 M223 236L240 253 M107 236L90 253 M223 120L240 103"/>
</g><circle cx="165" cy="178" r="61" fill="#FFD16B"/>
<g fill="white"><circle cx="190" cy="286" r="57"/><circle cx="266" cy="244" r="75"/><circle cx="337" cy="289" r="56"/><rect x="133" y="286" width="260" height="63" rx="31"/></g>
</svg>\n'''


def render():
    OUT.mkdir(parents=True, exist_ok=True)
    size, scale = 512, 4
    image = Image.new('RGB', (size, size))
    image.putdata([tuple(round(a+(b-a)*(x+y)/(2*(size-1))) for a,b in zip((40,127,219),(71,183,220)))
                   for y in range(size) for x in range(size)])
    image = image.convert('RGBA').resize((size*scale, size*scale), Image.Resampling.BICUBIC)
    def box(values): return tuple(round(v*scale) for v in values)
    layer = Image.new('RGBA', image.size);draw = ImageDraw.Draw(layer)
    draw.ellipse(box((-175,-162,265,278)),fill=(255,255,255,10))
    draw.ellipse(box((264,250,712,698)),fill=(255,255,255,9))
    image = Image.alpha_composite(image, layer);draw=ImageDraw.Draw(image)
    for x0,y0,x1,y1 in [(165,96,165,72),(165,260,165,284),(83,178,59,178),
                         (247,178,271,178),(107,120,90,103),(223,236,240,253),
                         (107,236,90,253),(223,120,240,103)]:
        draw.line(box((x0,y0,x1,y1)),fill='#FFD16B',width=10*scale)
        for x,y in [(x0,y0),(x1,y1)]:draw.ellipse(box((x-5,y-5,x+5,y+5)),fill='#FFD16B')
    draw.ellipse(box((104,117,226,239)),fill='#FFD16B')
    for x,y,r in [(190,286,57),(266,244,75),(337,289,56)]:
        draw.ellipse(box((x-r,y-r,x+r,y+r)),fill='white')
    draw.rounded_rectangle(box((133,286,393,349)),radius=31*scale,fill='white')
    image.resize((size,size),Image.Resampling.LANCZOS).convert('RGB').save(OUT/'icon.png',optimize=True)
    (OUT/'icon.svg').write_text(SVG)


if __name__ == '__main__':render()
