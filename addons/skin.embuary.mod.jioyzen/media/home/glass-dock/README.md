# Glass home dock

Native Kodi image/list controls; no Python animation loop or live GPU blur filter. Rounded translucent material, upper rim, soft shadow, colored app tiles and faint pre-rendered reflections. Lucide glyph license and SVG sources live in ../navigation/.

The wallpaper layer uses a full-screen dock stencil, decodes at 1/12 display dimensions, then magnifies using linear sampling. Wallpaper selection remains dynamic. This approximate frosted treatment is not an OS compositor backdrop blur. Desktop outside the dock remains unchanged. Original menu actions, visibility preferences and theme-colored focus marker are retained.

Refinement: neutral gray translucent glass, 1440×160 at (240,842), faint rim and lighter shadow. No icon tile edge strokes. Selected title appears outside the dock at (240,771); icon magnification is 125%, anchored at its lower center, with 150 ms Focus and 120 ms UnFocus sine easing. Normal icon/text positions are stable; no bounce, neighbor shifting or input delay. A small theme-colored focus dot replaces the previous underline and focus rectangle.

Fixed eight-slot navigation: dock width stays 1440; each item is 180 pixels. For one to seven visible entries, a zero-duration native offset of (8-count)*90 centers the entire row without widening individual slots. Eight or more entries use the full unchanged viewport; Kodi horizontal list scrolls excess entries. `onleft`/`onright` are explicit `noop` actions, disabling Kodi's implicit self-navigation wrapping at the ends. External title font is 37, remote hint removed. Glass opacity reduced slightly (52/255 to 70/255).
