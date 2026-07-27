# 品牌资产生成工具

`gen_logos.py` 由字体轮廓 + 实测几何参数生成全部 SVG；`render_all.sh` 用本机 Chrome 无头模式批量导出 PNG/favicon。

## 复现步骤

1. 下载思源黑体 Bold（SIL OFL）到 `/tmp/brand-fonts/`：
   ```bash
   mkdir -p /tmp/brand-fonts
   curl -sL -o /tmp/brand-fonts/SourceHanSansSC-Bold.otf \
     "https://cdn.jsdelivr.net/gh/adobe-fonts/source-han-sans@release/OTF/SimplifiedChinese/SourceHanSansSC-Bold.otf"
   ```
2. 运行 `bash render_all.sh`（脚本内 `PY` 变量指向带 fontTools+Pillow 的 Python）。

## 微调入口（gen_logos.py）

- `CROWN_BLUE / INK`：品牌色
- `BAR / WEDGE / SQUARE`：图形标几何（单位制，标高 96）
- `build_horizontal()` 内的 `22 / 36 / 13 / 18 / 7.2`：标-文间距、中文字高、行距、英文高度、中文字距
