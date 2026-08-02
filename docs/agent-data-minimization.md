# Agent 数据最小化清单（单一来源）

本文件是**所有**开发/验收提示词共用的敏感字段禁止清单。提示词一律写
「适用 `docs/agent-data-minimization.md` 全部条目」并只列**本票特有的例外**，
不得复制本清单——复制必然漂移，漂移出来的那一份会被 agent 当成有意的例外。

适用范围：所有由 agent 新增或修改的代码路径。

## 1. 受管面（每一个都要检查）

| 面 | 含义 |
|---|---|
| DB 列 | 新增/修改的表列、JSON 列内部的 key |
| API 响应 | HTTP JSON body 的 key 与 value，递归到任意深度 |
| 审计 detail | `AuditLog.detail` 及任何等价的结构化事件体 |
| 日志 | `logger.*` 的 message 与 extra，含异常分支 |
| 进程参数 | CLI argv、subprocess 命令行、systemd `ExecStart` |
| 前端 DOM | 渲染进页面的文本、属性、`console.*` |
| 浏览器存储 | `localStorage` / `sessionStorage` / cookie |

## 2. 禁止字段族

以下内容**不得**出现在上表任何一个面上。

**消息与内容**
- 消息正文、`content`、`structured_content`
- 解密 payload、原始密文、媒体二进制

**凭据与密钥**
- 密码、密码 hash、密码片段
- token（含 session token、invite token、password reset token）
- secret、API key、Basic Auth 凭据
- 密文、mask 后的 secret、secret 的任何派生表示（长度、前缀、指纹）

**定位与访问**
- signed URL、预签名链接
- storage key、storage path、任何文件系统路径

**身份标识**
- 原始企微身份：`sender`、`recipient`、`room`、raw `msgid`
- 内部主键在对外面上的暴露：`archive_message_id`、`run_id`、`tenant_id`
- 登录失败场景中**用户提交的**标识：username、email、手机号、源 IP

**用户输入与内部状态**
- 搜索文本、搜索词
- traceback、raw exception、异常 message 原文

## 3. 允许的替代形态

需要表达上述概念时，用这些替代物：

| 想表达 | 用 |
|---|---|
| 是谁操作的 | 已认证会话解析出的内部 `admin_user_id`（不是提交值） |
| 哪个平台管理员 | 稳定的 `platform_admin_id`，不带 email/凭据 |
| 出了什么错 | allowlist 化的 `safe_error_code` 枚举 |
| 哪条记录 | 不可猜测的 public id（与内部主键不同值） |
| 改了什么 | 排序稳定的 `changed_keys`（只有 key 名，没有值） |
| 规模有多大 | 聚合计数 |

## 4. 例外规则

各票**只能**在自己的 AC 里显式列出例外，形如「本票允许 detail 含 `platform_admin_id`」。
没有写进 AC 的一律按禁止处理。禁止用注释、commit message 或 QA Summary 主张例外。

## 5. 测试哨兵（验收必须用这一组）

新增 detail / API 响应 / 日志的测试，必须用下列哨兵值构造 fixture，然后
**递归遍历**全部 key 与 value 断言其缺失。只检查顶层 key、或只靠人工阅读，
一律判 `INSUFFICIENT_TEST_COVERAGE`。

```python
# 复制到测试文件，逐个断言不出现在受管面上
SENTINELS = (
    "SENTINEL_MESSAGE_BODY",
    "SENTINEL_STRUCTURED_CONTENT",
    "SENTINEL_PASSWORD",
    "SENTINEL_PASSWORD_HASH",
    "SENTINEL_TOKEN",
    "SENTINEL_SECRET",
    "SENTINEL_SIGNED_URL",
    "SENTINEL_STORAGE_KEY",
    "/sentinel/fs/path",
    "SENTINEL_SEARCH_TEXT",
    "SENTINEL_TRACEBACK",
    "SENTINEL_SENDER",
    "SENTINEL_RECIPIENT",
    "SENTINEL_ROOM",
    "SENTINEL_RAW_MSGID",
)
```

递归断言的形状（key 与 value 都要查）：

```python
def assert_no_sentinels(obj, path="$"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert not any(s in str(key) for s in SENTINELS), f"{path}.{key}"
            assert_no_sentinels(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            assert_no_sentinels(value, f"{path}[{i}]")
    else:
        assert not any(s in str(obj) for s in SENTINELS), path
```

前端同理：把哨兵放进 mock 的 API 响应，断言它们不出现在
`document.body.innerHTML`、任何属性值、`console` 输出和浏览器存储里。

## 6. 测试数据纪律

- fixture 只用假数据；不得从生产库导出、不得使用真实企微 ID/手机号/邮箱。
- 哨兵值本身必须是明显的假值（如上表），不要用看起来像真值的字符串。

## 沿革

- **2026-08-02**：从 RND-335~339 五张票的提示词中提取。此前同一份清单在
  单张 dev prompt 内重复 4 次（范围边界 / AC / 风险与回滚 / 硬性约束），
  dev 与 qa 之间又各自重述，共 7 处，已全部改为引用本文件。
