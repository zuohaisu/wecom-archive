#!/usr/bin/env python3
"""康冠时代 logo 矢量化精修生成器
基于原 logo 像素测量数据 + 思源黑体(SIL OFL)文字转轮廓，产出纯路径 SVG。
"""
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.boundsPen import BoundsPen
import os

FONT_PATH = '/tmp/brand-fonts/SourceHanSansSC-Bold.otf'
OUT_DIR = '/Users/zuohaisu/Documents/code/wecom-archive-365/design/brand/logo/svg'

# ---- 品牌色 ----
CROWN_BLUE = '#1F5CC4'   # 康冠蓝（主色，由原 #2660B5 精修提亮）
INK = '#1D2733'          # 墨色（文字，由原 #404040 加深）
BLACK = '#000000'
WHITE = '#FFFFFF'

# ---- 图形标几何（单位制，高 96）----
# 实测原标：横条21x29@中、梯形柱左高71右高85宽22、方块23x29@中、间隙2/10
# 精修：斜率统一 1:3、边缘对齐、间隙规整
MARK_W, MARK_H = 80, 96
BAR = (0, 32, 22, 64)            # x0,y0,x1,y1 横条
WEDGE = [(24, 8), (48, 0), (48, 96), (24, 88)]  # 梯形柱（左短右长）
SQUARE = (58, 32, 80, 64)        # 方块

def mark_paths(fill):
    b = f'M{BAR[0]},{BAR[1]} H{BAR[2]} V{BAR[3]} H{BAR[0]} Z'
    w = 'M' + ' L'.join(f'{x},{y}' for x, y in WEDGE) + ' Z'
    s = f'M{SQUARE[0]},{SQUARE[1]} H{SQUARE[2]} V{SQUARE[3]} H{SQUARE[0]} Z'
    return f'<path d="{b}" fill="{fill}"/><path d="{w}" fill="{fill}"/><path d="{s}" fill="{fill}"/>'

# ---- 字体工具 ----
font = TTFont(FONT_PATH)
glyph_set = font.getGlyphSet()
cmap = font.getBestCmap()
UPM = font['head'].unitsPerEm

def glyph_info(ch):
    gname = cmap[ord(ch)]
    glyph = glyph_set[gname]
    pen = SVGPathPen(glyph_set)
    glyph.draw(pen)
    bp = BoundsPen(glyph_set)
    glyph.draw(bp)
    return pen.getCommands(), glyph.width, bp.bounds  # bounds=(xMin,yMin,xMax,yMax)

def text_group(text, fill, target_h, x0, y_top, tracking=0.0, justify_w=None, cap_height=None):
    """把一行文字转成 <g>，返回 (svg, total_width)。
    target_h: 视觉高度（CJK=字面框高，Latin=大写高度）
    justify_w: 若给定，拉伸字距使总宽等于该值
    cap_height: Latin 缩放基准（font units），CJK 用 None（按字面框）
    """
    items = []
    for ch in text:
        d, adv, bounds = glyph_info(ch)
        items.append((ch, d, adv, bounds))
    if cap_height:  # Latin：按大写高度统一缩放
        s = target_h / cap_height
    else:           # CJK：按最大字面高统一缩放
        max_h = max(b[3] - b[1] for _, _, _, b in items if b)
        s = target_h / max_h
    advances = [adv * s for _, _, adv, _ in items]
    n_gaps = len(items) - 1
    if justify_w is not None:
        natural = sum(advances)
        tracking = (justify_w - natural) / n_gaps
    total = sum(advances) + tracking * n_gaps
    parts = [f'<g fill="{fill}">']
    x = x0
    for ch, d, adv_raw, bounds in items:
        adv = adv_raw * s   # 缩放后的字宽
        if ch == ' ':
            x += adv + tracking
            continue
        xMin, yMin, xMax, yMax = bounds
        # 基线 y：使字形视觉顶部落在 y_top
        y_base = y_top + yMax * s
        parts.append(f'<path transform="translate({x - xMin*s:.2f},{y_base:.2f}) scale({s:.4f},{-s:.4f})" d="{d}"/>')
        x += adv + tracking
    parts.append('</g>')
    return ''.join(parts), total

def svg_doc(w, h, body, comment=''):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" role="img" aria-label="康冠时代 CROWN TIME">\n'
            f'<!-- 康冠时代 CROWN TIME 注册商标第17528858号 · 精修矢量版{comment} -->\n{body}\n</svg>\n')

# ---- 横版组合 ----
# 布局：标 80x96 | 间隙22 | 文本块（康冠时代36 + 间13 + CROWN TIME 18 = 67，垂直居中）
def build_horizontal(mark_fill, text_fill):
    line1_h, line2_h, vgap = 36, 18, 13
    block_h = line1_h + vgap + line2_h
    y1 = (MARK_H - block_h) / 2          # 14.5
    y2 = y1 + line1_h + vgap             # 63.5
    x_text = MARK_W + 22                 # 102
    g1, w1 = text_group('康冠时代', text_fill, line1_h, x_text, y1, tracking=7.2)
    g2, w2 = text_group('CROWN TIME', text_fill, line2_h, x_text, y2,
                        justify_w=w1, cap_height=font['OS/2'].sCapHeight)
    total_w = round(x_text + w1)         # 268
    body = mark_paths(mark_fill) + g1 + g2
    return total_w, MARK_H, body

def build_stacked(mark_fill, text_fill):
    line1_h, line2_h = 36, 18
    gap_m, gap_l = 26, 13
    g1_tmp, w1 = text_group('康冠时代', text_fill, line1_h, 0, 0, tracking=7.2)
    W = round(w1 + 54)                   # 220
    H = MARK_H + gap_m + line1_h + gap_l + line2_h   # 189
    x_mark = (W - MARK_W) / 2
    x_text = (W - w1) / 2
    y1 = MARK_H + gap_m
    y2 = y1 + line1_h + gap_l
    g1, _ = text_group('康冠时代', text_fill, line1_h, x_text, y1, tracking=7.2)
    g2, _ = text_group('CROWN TIME', text_fill, line2_h, x_text, y2,
                       justify_w=w1, cap_height=font['OS/2'].sCapHeight)
    body = f'<g transform="translate({x_mark:.0f},0)">{mark_paths(mark_fill)}</g>' + g1 + g2
    return W, H, body

variants = {}
w, h, body = build_horizontal(CROWN_BLUE, INK)
variants['logo-horizontal.svg'] = svg_doc(w, h, body, ' · 彩色横版')
variants['logo-horizontal-mono.svg'] = svg_doc(*build_horizontal(BLACK, BLACK)[:2], build_horizontal(BLACK, BLACK)[2], ' · 单色黑横版（对应注册证形态）')
variants['logo-horizontal-reverse.svg'] = svg_doc(*build_horizontal(WHITE, WHITE)[:2], build_horizontal(WHITE, WHITE)[2], ' · 反白横版（深色底专用）')
w2, h2, body2 = build_stacked(CROWN_BLUE, INK)
variants['logo-stacked.svg'] = svg_doc(w2, h2, body2, ' · 彩色竖版')
variants['logo-icon.svg'] = svg_doc(MARK_W, MARK_H, mark_paths(CROWN_BLUE), ' · 图形标')
variants['logo-icon-mono.svg'] = svg_doc(MARK_W, MARK_H, mark_paths(BLACK), ' · 图形标单色黑')
variants['logo-icon-reverse.svg'] = svg_doc(MARK_W, MARK_H, mark_paths(WHITE), ' · 图形标反白')

# favicon 优化版：横条与柱间隙 2→4，小尺寸更清晰
def mark_paths_favicon(fill):
    b = 'M0,32 H22 V64 H0 Z'
    w_ = 'M26,6.75 L50,0 V96 L26,89.25 Z'
    s = 'M60,32 H82 V64 H60 Z'
    return f'<path d="{b}" fill="{fill}"/><path d="{w_}" fill="{fill}"/><path d="{s}" fill="{fill}"/>'
# favicon 用正方形 viewBox（横向各补 7 单位净空），gap 优化为 4
fav_svg = svg_doc(96, 96, mark_paths_favicon(CROWN_BLUE), ' · favicon')
fav_svg = fav_svg.replace('viewBox="0 0 96 96"', 'viewBox="-7 0 96 96"')
variants['../../favicon/favicon.svg'] = fav_svg

# 应用图标底板（apple-touch-icon / PWA）：品牌蓝底 + 白色图形标居中
tile_mark = mark_paths_favicon(WHITE)
variants['../../favicon/icon-tile.svg'] = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">\n'
    f'<!-- 康冠时代 · 应用图标底板 -->\n'
    f'<rect width="96" height="96" rx="21" fill="{CROWN_BLUE}"/>\n'
    f'<g transform="translate(24,19.9) scale(0.585)">{tile_mark}</g>\n</svg>\n')

os.makedirs(OUT_DIR, exist_ok=True)
for name, content in variants.items():
    path = os.path.join(OUT_DIR, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(content)
    print(f'{name}: {len(content)} bytes')
print('cap height:', font['OS/2'].sCapHeight, 'upm:', UPM)
