# wecom-archive-365 生产运维文档 — 媒体存储

## 媒体存储架构

当前生产环境采用**混合媒体存储架构**：

- **新媒体写入** → `qiniu_kodo`（七牛 Kodo 对象存储）
- **历史媒体读取** → `local`（本地文件系统，已存储 122 个文件，33MB）
- **读取时**：每条 media_files 记录的 `storage_backend` 字段决定使用哪个 Provider，而非 MEDIA_STORAGE_PROVIDER 全局设置
- Provider 决议路径：`storage_backend + storage_ref` → 优先；兼容 `local_path` 回退

## MEDIA_STORAGE_PROVIDER 可选值

| 值 | 说明 |
|----|------|
| `local` | 本地文件系统，写入 `STORAGE_LOCAL_PATH` |
| `qiniu_kodo` | 七牛 Kodo 对象存储，需 QINIU_* 配置 |
| 未设置 | 代码缺省为 `local` |

## Qiniu 必填配置

```ini
QINIU_ACCESS_KEY=<access_key>
QINIU_SECRET_KEY=<secret_key>
QINIU_BUCKET=365-wecom-media
QINIU_REGION=z2
QINIU_DOMAIN=https://media.example.com
MEDIA_STORAGE_PROVIDER=qiniu_kodo

# RND-187 — 可选，Qiniu Signed URL 有效期（秒）。未设置时默认 900（15 分钟），
# 允许范围 60~3600，超出范围/非整数会在请求时报配置错误，不会静默 clamp。
MEDIA_SIGNED_URL_TTL_SECONDS=900
```

## Secret 文件位置

```
/srv/apps/wecom-archive-365/current/backend/.env
```

## 配置权限

```
Owner: wecomarchive
Group: wecomarchive
Mode: 600 (owner 读写，group 和 other 无权限)
```

**安全要求**：.env 文件必须保持 `600` 权限，严禁扩大读取范围。

## 切换步骤

### local → qiniu_kodo

```bash
# 1. 修改配置
sed -i 's/^MEDIA_STORAGE_PROVIDER=.*/MEDIA_STORAGE_PROVIDER=qiniu_kodo/' backend/.env

# 2. 重启服务
sudo systemctl restart wecom-archive-365.service

# 3. 验证
systemctl is-active wecom-archive-365.service
curl -fsS http://127.0.0.1:8035/health
```

## 健康检查

```bash
# 内部
curl http://127.0.0.1:8035/health

# 外部
curl https://archive.example.com/health
```

预期响应：`{"status":"ok"}`

## 新媒体验证步骤

```bash
# 确认写入 backend
.venv/bin/python -c "
import os
for line in open('.env'):
    if '=' in line and not line.startswith('#'):
        k, v = line.strip().split('=', 1)
        os.environ[k.strip()] = v.strip()
from app.media_storage import get_configured_write_backend_name
print(get_configured_write_backend_name())
"

# 检查最新一条媒体记录
# 记录的 storage_backend 应为 qiniu_kodo
```

## 本地历史媒体兼容说明

- 切换 provider 不影响已有 `local` 记录的读取
- 历史 132 条 local 媒体记录仍然通过 `LocalStorageProvider` 读取
- 切换后新增的媒体写入 `qiniu_kodo`
- 两种 backend 在同一部署中同时可用

## 回滚步骤

```bash
# 1. 回滚配置
sed -i 's/^MEDIA_STORAGE_PROVIDER=.*/MEDIA_STORAGE_PROVIDER=local/' backend/.env

# 2. 重启
sudo systemctl restart wecom-archive-365.service

# 3. 验证
systemctl is-active wecom-archive-365.service
curl -fsS http://127.0.0.1:8035/health

# 4. 验证本地历史媒体
curl -b 'session_id=<valid_session>' \
  http://127.0.0.1:8035/api/conversations/<conv_id>/messages/<msgid>/media

# 5. 验证七牛媒体仍可访问（backend 从行记录解析）
curl -b 'session_id=<valid_session>' \
  http://127.0.0.1:8035/api/conversations/<conv_id>/messages/<msgid>/media
```

## 七牛写入失败排查

| 现象 | 可能原因 | 排查步骤 |
|------|---------|---------|
| 媒体文件 status=failed | Qiniu 上传失败 | 检查 journalctl -u wecom-archive-media-download.service |
| /health 正常但新媒体无法写入 | AK/SK 过期或 Bucket 配置错误 | 验证 QINIU_ACCESS_KEY / QINIU_SECRET_KEY |
| 新媒体标记 downloaded 但读取 503 | Provider 不可用（网络/认证/Bucket 错误） | 检查日志中的 MediaStorageUnavailable |
| 媒体读取返回 404 | 对象在七牛中缺失 | 确认 storage_ref 正确 |

## 421 / 401 / 403 排查

| HTTP 状态 | 含义 | 处理 |
|-----------|------|------|
| 401 | 认证失败（无有效 session） | 用户需重新登录 |
| 403 | 七牛私有 Bucket 直访（正常） | 无需处理，后端代理读取 |
| 404 | 媒体不存在（未下载/已删除/错误会话） | 检查 media_files 行状态 |
| 421 | Misdirected Request（HTTPS Host 不匹配） | 确认域名/证书配置 |
| 500 | 未知或未配置的 storage_backend | 检查 media_files.storage_backend 值 |
| 503 | Provider 不可用（网络/认证/Bucket 超时） | 检查七牛服务状态和凭证 |

## 日志查看命令

```bash
# 应用日志
journalctl -u wecom-archive-365.service --since '5 min ago'

# 媒体下载日志
journalctl -u wecom-archive-media-download.service --since '5 min ago'

# 同步+解密日志
journalctl -u wecom-archive-worker.service --since '5 min ago'
```

## ⚠️ 安全红线

1. **不得删除本地历史文件**（仍被 132 条 local 媒体记录引用）
2. **不得修改数据库历史媒体记录的 storage_backend**
3. **不得开放 Bucket 公共读**（Bucket 必须保持私有；无论 local 还是 qiniu_kodo，
   对象读取只能通过后端受控代理，或本文档下方 RND-187 描述的、经过完整登录
   + tenant + 媒体归属校验后才签发的短期 Signed URL）
4. **Signed URL 只能由后端在完成登录 + tenant + 媒体归属校验之后签发**
   （RND-187 上线后已更新：Qiniu 媒体的 Signed URL *会* 交给前端浏览器直连
   CDN 读取，这是 RND-187 的设计目标，不再是禁止项；但生成时机、权限校验顺序、
   TTL 上限、日志脱敏必须严格遵守下方"RND-187 Signed URL 访问流程"一节。
   任何情况下都不得跳过权限校验直接签发，也不得让 Signed URL 长期有效或被
   共享代理/CDN 缓存）
5. **.env 权限必须保持 600**
6. **日志中不得记录完整 Signed URL 或 token 查询参数**（见下方"日志脱敏"）
7. **前端不得保存 Signed URL 到 localStorage，也不得写入任何 analytics/上报**
   （见下方"前端过期恢复行为"）
8. **前端不得自行拼接七牛 URL，也不得向后端提交任意 object key / storage_ref**
   ——`GET .../media/access` 不接受任何 object key 类参数，签发所用的 key
   完全由后端从已授权的 media_files 行解析

## RND-187 Signed URL 访问流程

RND-187 为 Qiniu 私有空间媒体新增了短期 Signed URL 直连能力，Local 媒体行为不变。

### 统一媒体访问描述接口

```
GET /api/conversations/{conversation_id}/messages/{msgid}/media/access
```

- 鉴权与权限校验顺序与既有 `.../media` 代理路由完全一致，且共享同一份权限
  校验代码（`resolve_authorized_media` / `_resolve_servable_backend_and_ref`，
  `backend/app/services/media_access.py`——RND-221 从 `conversations.py` 抽出并合并）：
  登录校验 → 解析 session tenant_id → tenant 范围内查找 message/media_files
  行 → 归属校验，全部通过后才会触碰任何 storage provider 或签名逻辑。
- Qiniu 媒体额外增加一次 object key tenant 前缀校验
  （`object_key_tenant_prefix_matches`）：即使 media_files 行的 `tenant_id`
  列被误标，只要该行的 `storage_ref`（对象 key）自身的 `tenants/{tenant}/`
  前缀与当前登录 tenant 不一致，也会拒绝签发（404）。
- 响应体（统一访问描述，Local/Qiniu 共用同一结构）：

  ```json
  {
    "media_id": 207,
    "storage_backend": "qiniu_kodo",
    "access_type": "signed_url",
    "url": "<short-lived-url>",
    "expires_at": "<iso8601>",
    "content_type": "image/jpeg",
    "size_bytes": 333287
  }
  ```

  - Local 媒体：`access_type="proxy"`，`url` 就是既有 `.../media` 代理路由，
    `expires_at=null`（该 URL 本身不携带时效凭证，仍由 session cookie 鉴权）。
  - Qiniu 媒体：`access_type="signed_url"`，`url` 是七牛官方 SDK
    （`qiniu.Auth.private_download_url`）签发的短期私有下载 URL，浏览器直连
    `media.example.com`，图片内容不再经过 FastAPI 转发。
- 响应头 `Cache-Control: no-store`——该响应是逐用户、短时效的，不得被共享
  代理/CDN 缓存。
- 既有 `.../media` 原始代理路由**未删除**：Local 媒体仍然只走这条路由；
  Qiniu 媒体也仍可通过它访问（向后兼容、无其他已知调用方也不受影响），只是
  前端默认不再用它加载图片内容。

### TTL 配置

| 变量 | 默认值 | 允许范围 |
|------|--------|----------|
| `MEDIA_SIGNED_URL_TTL_SECONDS` | 900（15 分钟） | 60 ~ 3600 秒 |

超出范围或非整数值会在请求时抛出配置错误（HTTP 500，`MediaStorageConfigurationError`），
不会静默 clamp。读取逻辑见 `backend/app/media_storage.py:get_signed_url_ttl_seconds`。

自 RND-207 起，签发的 `expires_at` 并非简单的“签发时刻 + TTL”，而是通过
`MEDIA_SIGNED_URL_WINDOW_SECONDS`（未设置时默认等于 TTL，范围同为 60~3600 秒，
读取逻辑见 `get_signed_url_window_seconds`）把过期时间对齐到固定窗口
（`compute_signed_url_deadline`），使同一窗口内的重复请求得到字节相同的 URL，
从而被浏览器 HTTP 缓存复用；实际剩余有效期落在 `(window, 2*window]` 区间内，
而非精确等于 TTL。

### 前端过期恢复行为

管理台（`backend/app/web/static/console/*.js`，RND-216 从 `main.py` 内嵌
JS 外置、RND-217 拆分为 8 个模块，无独立前端工程）不再直接把
`media_url` 设为 `<img src>`，而是先请求 `.../media/access` 拿到统一访问
描述，再据此设置真实 `src`（`hydrateMediaImages` / `loadMediaImage`）。
图片加载失败（含 Signed URL 已过期）时最多自动重新请求一次访问描述并重试，
仍失败则显示占位错误文案，不会无限重试。前端不解析 `storage_ref`，不缓存
Signed URL 到 localStorage，也不写入任何日志/分析上报。

### 日志脱敏

- 签发成功/失败日志只记录 `media_id`、`tenant_id`、`storage_backend`、TTL、
  错误类别，**从不**记录完整 Signed URL、token 查询参数、AK/SK 或完整
  object key。
- `backend/app/qiniu_storage.py:redact_signed_url_for_log` 提供 URL 脱敏
  兜底（把查询参数值替换为 `[REDACTED]`），供未来任何确需在日志中提及 URL
  的诊断路径使用；当前所有调用点都直接不记录 URL，这是首选做法。
- 异常处理统一走 `_sanitized()`（`qiniu_storage.py`），从不把原始 SDK 异常
  文本（可能内嵌签名 URL/响应体）透出。

### 回滚方式

RND-187 是纯新增能力（新路由 `.../media/access`、Provider 新增
`get_download_url(storage_ref, expires_in)` 签名、前端改为先取访问描述再
设置图片 src），**没有引入需要手动回滚的数据库变更或破坏性配置项**，既有
`.../media` 代理路由与响应结构保持不变。若上线后需要回退：

1. 回滚本次部署（发布回上一版本），Local/Qiniu 媒体会立刻恢复为纯后端代理
   读取（RND-174 行为），前端也会回退为直接使用 `media_url`。
2. 无需修改 `MEDIA_STORAGE_PROVIDER`、Bucket 权限或 DNS/证书配置——这些均
   不受 RND-187 影响。
3. 如仅需临时降级但保留本次代码：客户端仍可继续访问既有
   `.../media` 代理路由（未删除），效果等同于回滚前端行为，无需改后端。

### 与 RND-186 的关系

RND-186（历史 Local → Qiniu 迁移工具）已开发完成（见下方"历史媒体迁移工具"），
但**尚未在生产环境执行实际迁移**。RND-187 不依赖 RND-186：RND-187 只处理
"如何把已经写入 Qiniu 的媒体安全地签发给浏览器"，与"是否/何时把历史 Local
媒体迁移到 Qiniu"是两个独立问题。当前 132 条历史 Local 媒体记录继续通过既有
代理路由访问，行为完全不变；一旦运维执行 RND-186 迁移工具，被迁移记录的
`storage_backend` 变为 `qiniu_kodo` 后会自动开始享有 RND-187 的 Signed URL
直连能力，无需再改代码。

## 历史媒体迁移工具（RND-186）

`scripts/migrate_local_media_to_qiniu.py` — 手动执行、可重复、可恢复的
Local → Qiniu 历史媒体迁移工具。**迁移范围已从"仅图片"修订为通用媒体**：
凡是 `media_files.file_type` 属于 `image`/`video`/`voice`/`file`
（`app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES`）、`storage_backend=local`
且 `download_status=downloaded` 的记录都是候选，字节内容会用
`detect_media_signature_from_bytes` 二次校验（从不只信任扩展名/`file_type`
声明）。详细设计见 `docs/research/rnd_186_local_qiniu_migration.md`。

**RND-186 撰写时的生产实际情况（历史记录，已被 RND-199 更新）**：当时系统只有
图片下载能力（`download_wecom_image_media_once.py`），因此现存 132 条历史记录
仍然全部是 `file_type=image`。视频/语音/文件的迁移能力当时是**面向未来的通用
化**——一旦有新的下载 worker 开始写入 `file_type=video/voice/file` 的记录，
本工具无需改动即可迁移它们。

**RND-199 更新**：上述"面向未来"的场景已经发生。`download_wecom_image_media_once.py`
已退役，统一由 `scripts/download_wecom_media_once.py`（`app/media_download.py`
单一实现）下载 image/voice/video/file/emotion 五种消息类型的媒体，`media_files`
不再只有 `file_type=image` 的记录。媒体访问接口（`.../media`、`.../media/access`）
也已同步扩展——不再是 RND-186 时的"仅服务 `msgtype=="image"`"限制，现在
image/voice/video/file 的记录都能通过这两个接口读取（emotion 记录也可按 msgid
直接读取，但时间线不会为其展示预览链接，行为对齐既有的"不支持表情消息"占位符，
不属于本次改动范围）。本迁移工具自身不受影响，仍按
`app.media_storage.SUPPORTED_MIGRATION_MEDIA_TYPES` 圈定候选范围。

```bash
# 仅统计候选数量，不做任何读写
.venv/bin/python scripts/migrate_local_media_to_qiniu.py --count-only

# 演练：读取本地文件并做字节内容校验，但不上传七牛、不写数据库
.venv/bin/python scripts/migrate_local_media_to_qiniu.py --dry-run --limit 20

# 正式执行（小批量、可重复运行）
.venv/bin/python scripts/migrate_local_media_to_qiniu.py --limit 20 --batch-size 10

# 重试此前失败的记录
.venv/bin/python scripts/migrate_local_media_to_qiniu.py --limit 50 --retry
```

**安全特性**：单条记录失败不影响其本地可访问性（`storage_backend`/
`storage_ref` 失败时保持不变）；重复执行对已迁移记录零读写（非幂等覆盖，
而是真正的 no-op）；执行目标始终是七牛，与 `MEDIA_STORAGE_PROVIDER` 无关；
迁移成功后完整持久化 `bucket`/`mime_type`/`checksum_sha256`（均从实际上传
字节重新计算，从不信任历史 `file_size` 字段）；本地文件此阶段**不会被删除**
（仅迁移工具已预留清理扩展点，未实现删除动作）。

执行前必须确认 `QINIU_*` 全部配置项已就绪（见上方"Qiniu 必填配置"），且
`STORAGE_LOCAL_PATH` 指向的历史文件仍然可读。

## 待办任务（未在本部署中执行）

- **RND-186 生产执行**：迁移工具已按通用媒体类型（image/video/voice/file）
  重新开发完成并通过自测，尚未在生产环境实际运行（工具默认建议先
  `--dry-run` 演练，确认无误后再正式执行）
- **后续 ticket（未规划）**：Media Access API / Signed URL 扩展到
  video/voice/file，使已迁移的非图片媒体可被前端读取——RND-186 明确不含此项
- ~~RND-187：Signed URL 策略~~ 已完成（见上方"RND-187 Signed URL 访问流程"）
- 当前部署生产配置时间戳: 2026-07-12 01:58 CST
- 最后更新: 2026-07-13（RND-186 迁移工具完成媒体范围修订：image/video/voice/file
  通用化 + 完整存储元数据持久化，尚未部署生产/尚未执行迁移）
