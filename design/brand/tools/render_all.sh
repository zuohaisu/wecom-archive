#!/bin/bash
# 批量渲染 logo PNG 与 favicon 套装
set -e
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
B=/Users/zuohaisu/Documents/code/wecom-archive-365/design/brand
PY=/Users/zuohaisu/.workbuddy/binaries/python/envs/default/bin/python

render() { # svg_path width height scale out_png
  "$CHROME" --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
    --hide-scrollbars --force-device-scale-factor=$4 --window-size=$2,$3 \
    --default-background-color=00000000 --screenshot="$5" "file://$1" 2>/dev/null
}

$PY /tmp/brand-gen/gen_logos.py

# --- logo PNG 导出 ---
render $B/logo/svg/logo-horizontal.svg         275 96 1 $B/logo/png/logo-horizontal@1x.png
render $B/logo/svg/logo-horizontal.svg         275 96 2 $B/logo/png/logo-horizontal@2x.png
render $B/logo/svg/logo-horizontal.svg         275 96 4 $B/logo/png/logo-horizontal@4x.png
render $B/logo/svg/logo-horizontal-mono.svg    275 96 2 $B/logo/png/logo-horizontal-mono@2x.png
render $B/logo/svg/logo-horizontal-reverse.svg 275 96 2 $B/logo/png/logo-horizontal-reverse@2x.png
render $B/logo/svg/logo-stacked.svg            227 189 1 $B/logo/png/logo-stacked@1x.png
render $B/logo/svg/logo-stacked.svg            227 189 2 $B/logo/png/logo-stacked@2x.png
render $B/logo/svg/logo-stacked.svg            227 189 4 $B/logo/png/logo-stacked@4x.png
render $B/logo/svg/logo-icon.svg               80 96 1  $B/logo/png/logo-icon@1x.png
render $B/logo/svg/logo-icon.svg               80 96 2  $B/logo/png/logo-icon@2x.png
render $B/logo/svg/logo-icon.svg               80 96 4  $B/logo/png/logo-icon@4x.png
render $B/logo/svg/logo-icon.svg               80 96 8  $B/logo/png/logo-icon@8x.png
render $B/logo/svg/logo-icon-reverse.svg       80 96 4  $B/logo/png/logo-icon-reverse@4x.png
render $B/logo/svg/logo-icon-mono.svg          80 96 4  $B/logo/png/logo-icon-mono@4x.png

# --- favicon 套装 ---
render $B/favicon/favicon.svg    96 96 1 /tmp/brand-gen/fav-96.png
render $B/favicon/favicon.svg    48 48 1 $B/favicon/favicon-48.png
render $B/favicon/favicon.svg    32 32 1 $B/favicon/favicon-32.png
render $B/favicon/favicon.svg    16 16 1 $B/favicon/favicon-16.png
render $B/favicon/icon-tile.svg  180 180 1 $B/favicon/apple-touch-icon.png
render $B/favicon/icon-tile.svg  192 192 1 $B/favicon/icon-192.png
render $B/favicon/icon-tile.svg  512 512 1 $B/favicon/icon-512.png

# --- favicon.ico（16+32+48 多尺寸）---
$PY - << 'EOF'
from PIL import Image
base = '/Users/zuohaisu/Documents/code/wecom-archive-365/design/brand/favicon'
imgs = [Image.open(f'{base}/favicon-{s}.png').convert('RGBA') for s in (16, 32, 48)]
imgs[1].save(f'{base}/favicon.ico', format='ICO', sizes=[(16,16),(32,32),(48,48)],
             append_images=[imgs[0], imgs[2]])
print('favicon.ico written')
EOF

ls -la $B/logo/png/ $B/favicon/