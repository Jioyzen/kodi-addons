#!/usr/bin/env python3
"""Render the flat AI media-library icon using code-native vector geometry."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'addons/script.ai.movie.scanner/resources'

SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#258BCC"/><stop offset="1" stop-color="#29B8BA"/></linearGradient></defs>
<rect width="512" height="512" fill="url(#bg)"/>
<circle cx="52" cy="50" r="248" fill="white" opacity=".045"/>
<circle cx="472" cy="460" r="228" fill="white" opacity=".035"/>
<rect x="88" y="139" width="274" height="200" rx="28" fill="white" opacity=".23"/>
<rect x="117" y="183" width="300" height="210" rx="28" fill="none" stroke="white" stroke-width="18"/>
<path d="M240 244 Q235 241 235 248 V327 Q235 334 241 330 L301 293 Q308 288 301 284 Z" fill="white"/>
<path d="M398 80 C405 112 415 126 448 134 C415 141 405 155 398 188 C391 155 381 141 348 134 C381 126 391 112 398 80Z" fill="white"/>
</svg>\n'''


def render():
    OUT.mkdir(parents=True, exist_ok=True)
    size, scale = 512, 4
    bg = Image.new('RGB', (size, size))
    start, end = (37,139,204), (41,184,186)
    bg.putdata([tuple(round(a+(b-a)*(x+y)/(2*(size-1))) for a,b in zip(start,end))
                for y in range(size) for x in range(size)])
    image = bg.convert('RGBA').resize((size*scale,size*scale), Image.Resampling.BICUBIC)
    layer = Image.new('RGBA', image.size)
    draw = ImageDraw.Draw(layer)
    def box(rect):
        return tuple(round(v*scale) for v in rect)
    draw.ellipse(box((-196,-198,300,298)), fill=(255,255,255,11))
    draw.ellipse(box((244,232,700,688)), fill=(255,255,255,9))
    image = Image.alpha_composite(image,layer)
    layer = Image.new('RGBA', image.size);draw=ImageDraw.Draw(layer)
    draw.rounded_rectangle(box((88,139,362,339)),radius=28*scale,fill=(255,255,255,59))
    image=Image.alpha_composite(image,layer)
    draw=ImageDraw.Draw(image)
    # Stroke centered around the same vector rectangle coordinates.
    draw.rounded_rectangle(box((108,174,426,402)),radius=37*scale,outline='white',width=18*scale)
    # Rounded triangle from a densely sampled quadratic SVG path.
    def quadratic(p0,p1,p2):
        return [((1-t)**2*p0[0]+2*(1-t)*t*p1[0]+t*t*p2[0],
                 (1-t)**2*p0[1]+2*(1-t)*t*p1[1]+t*t*p2[1]) for t in [i/16 for i in range(17)]]
    points=quadratic((240,244),(235,241),(235,248))
    points+= [(235,327)] + quadratic((235,327),(235,334),(241,330))
    points+= [(301,293)] + quadratic((301,293),(308,288),(301,284))
    draw.polygon([(round(x*scale),round(y*scale)) for x,y in points],fill='white')
    def cubic(a,b,c,d):
        points=[]
        for i in range(33):
            t=i/32;u=1-t
            points.append((u**3*a[0]+3*u*u*t*b[0]+3*u*t*t*c[0]+t**3*d[0],
                           u**3*a[1]+3*u*u*t*b[1]+3*u*t*t*c[1]+t**3*d[1]))
        return points
    star=[]
    for curve in [((398,80),(405,112),(415,126),(448,134)),
                  ((448,134),(415,141),(405,155),(398,188)),
                  ((398,188),(391,155),(381,141),(348,134)),
                  ((348,134),(381,126),(391,112),(398,80))]:
        star+=cubic(*curve)
    draw.polygon([(round(x*scale),round(y*scale)) for x,y in star],fill='white')
    image.resize((size,size),Image.Resampling.LANCZOS).convert('RGB').save(OUT/'icon-library.png',optimize=True)
    (OUT/'icon-library.svg').write_text(SVG)


if __name__ == '__main__':
    render()
