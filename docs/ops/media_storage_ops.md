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
QINIU_DOMAIN=https://media.crowntime.cn
MEDIA_STORAGE_PROVIDER=qiniu_kodo
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
curl https://qwhhcd.crowntime.cn/health
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
3. **不得开放 Bucket 公共读**（保持私有，后端代理读取）
4. **不得将 Signed URL 直接暴露给前端**
5. **.env 权限必须保持 600**

## 待办任务（未在本部署中执行）

- **RND-186**：历史媒体迁移（local → qiniu_kodo），尚未执行
- **RND-187**：Signed URL 策略，尚未执行
- 当前部署生产配置时间戳: 2026-07-12 01:58 CST
- 最后更新: 2026-07-12
