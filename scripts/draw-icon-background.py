"""Draw an independent rounded-square background using a superellipse."""
from math import cos, sin, pi, copysign
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
icon = root / 'assets' / 'brand' / 'app-icon.svg'
exponent = 4.2
points = []
for step in range(512):
    angle = 2 * pi * step / 512
    def coordinate(value):
        value = 0 if abs(value) < 1e-12 else value
        return 256 + 256 * copysign(abs(value) ** (2 / exponent), value)
    points.append(f'{coordinate(cos(angle)):.4f},{coordinate(sin(angle)):.4f}')
background = '<path fill="#74c184" d="M' + ' L'.join(points) + ' Z"/>'
svg = icon.read_text(encoding='utf-8')
svg, count = re.subn(r'(<svg[^>]*>)(?:<path fill="#74c184"[^>]*/>|<rect[^>]*/>)',
                     lambda match: match[1] + background, svg, count=1)
if count != 1:
    raise RuntimeError('Cannot locate app-icon background')
icon.write_text(svg, encoding='utf-8')
print('Drew independent superellipse: exponent 4.2, 512 samples')
