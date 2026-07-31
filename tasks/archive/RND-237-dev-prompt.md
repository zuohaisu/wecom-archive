# RND-237 执行提示词（开源发布包装：LICENSE / 公开 README / .gitignore 复核）

> 用途：粘贴给开发 agent（或 Haisu 自行执行）。关联 epic：RND-232。
> 不自行 commit / push。
> **2026-07-29 更新**：许可证决策已由 MIT 反转为 **AGPL-3.0**（详见阶段二）；同日 Linear 澄清本票范围仅为开源包装（LICENSE/README/CLA/gitignore），与 `.github/workflows/deploy.yml` 等生产部署管道无关，无需从 RND-232 拆出。

## 0. 任务与来源
- RND-237「开源发布包装：LICENSE / 公开 README / .gitignore 复核」，父 epic RND-232。
- 现状：仓库**无 LICENSE**；`.gitignore` 存在需复核；根 `README.md` 存在需复核是否泄露身份 / 密钥；**无 CLA 文档**；README **未说明** `backend/vendor/wecom_sdk/` 需自行获取。

## 1. 执行步骤

### 阶段一：复核现状（只读）
- 读 `.gitignore`，确认含：`.env`、`*.pem`、`*.key`、`*.crt`、`secrets*`、`.qoder/`（Linear 清单第 3 项明确列出的敏感类）；另核对 `.env.*`、`__pycache__/`、`*.pyc`、构建产物是否已覆盖；`.env.example` 应**允许**（仅占位符）。
- 读 `README.md`，检查是否含真实域名（`crowntime.cn`）、公司身份、密钥、内部地址；与 RND-233/234 协调（README 若含域名，在 RND-233 阶段一并改）。
- 确认是否已有 LICENSE 引用（如包元数据 `pyproject.toml`）；确认当前**无** CLA 文档、README 中**无** WeCom SDK 获取说明（Linear 2026-07-29 新增的两项缺口）。

### 阶段二：补齐发布包装
- **LICENSE**：新增 `LICENSE` 文件，全文为 **AGPL-3.0**（2026-07-29 Haisu 拍板反转，**放弃此前 2026-07-26 确认的 MIT**）。决策原因：项目将并行运营云托管商业版，AGPL-3.0 的网络服务条款可防止第三方直接用本项目代码另起云托管生意而不回馈代码；代价是会挡掉部分企业内嵌场景，已知悉并接受。
  - 从权威来源获取 AGPL-3.0 **全文**（如 `https://www.gnu.org/licenses/agpl-3.0.txt`），逐字写入 `LICENSE`，不要用摘要或自行改写的版本。
  - AGPL-3.0 官方文本本身不含需要替换的「版权人/年份」占位符（不同于 MIT）；若额外需要在源码文件头部加版权声明，格式与主体留待 Haisu 后续决定，本票不强制。
- **CLA（贡献者许可协议）**：**新增**（Linear 2026-07-29 新增第 4 项要求）。AGPL + 自营云的标准配置——没有 CLA，外部贡献进来后项目将失去单方面调整授权（如未来商业化再许可）的能力。
  - 新增 `CLA.md`（或 `.github/CLA.md`），内容至少包含：贡献者授予项目维护方在 AGPL-3.0 之外**额外**再许可（relicense）该贡献的权利、贡献者保证拥有贡献版权、免责声明。
  - 在 `README.md`（及 `CONTRIBUTING.md`，若创建）中加入指引：外部贡献者提交 PR 前必须签署 CLA（具体签署机制——CLA bot / 手动签名——留待 Haisu 后续决定，本票只需文档到位 + 指引清晰，不强制接入自动化工具）。
- **README**：若存在身份 / 密钥泄露，移除或泛化；重写为公开版本，至少含：项目定位、特性、架构概览、快速开始（指向 `.env.example`）、配置说明、部署指引（引用 RND-233 的 `ARCHIVE_DOMAIN`）、许可证段落（注明 AGPL-3.0）、贡献/安全政策入口（含 CLA 签署指引）。
  - **新增（Linear 2026-07-29 第 5 项）**：README 必须明确写清 `backend/vendor/wecom_sdk/` 是**腾讯专有 SDK**，已被 `.gitignore` 排除、仓库不含二进制；自托管用户必须**自行从腾讯获取**该 SDK 才能跑通归档解密链路——否则用户会在"快速开始"卡在这一步且没有任何报错提示，需在 README 显著位置提示（例如快速开始步骤中加一条前置说明 + 常见问题区）。
  - 不杜撰未实现功能。
- **.gitignore**：补齐缺失项（至少 `.env`、`*.pem`、`*.key`、`*.crt`、`secrets*`、`.qoder/`、本地密钥），确认不忽略 `.env.example`。
- **可选（Linear 第 6 项，非强制）**：`CONTRIBUTING.md`、`SECURITY.md`、`.github/` issue 模板。若时间允许可一并补齐，不做不算失败。

### 阶段三：验证
- `test -f LICENSE && echo ok`；`grep -q "GNU AFFERO GENERAL PUBLIC LICENSE" LICENSE` 确认是 AGPL-3.0 全文而非 MIT 残留。
- `test -f CLA.md && echo ok`（或实际路径）；`grep -rn "CLA" README.md` 确认有指引。
- `grep -n "wecom_sdk" README.md` 确认已有 SDK 获取说明。
- `.gitignore` 含 `.env`、`*.pem`、`*.key`、`*.crt`、`secrets*`、`.qoder/`。
- `grep -rn "crowntime\|康冠\|ICP备" README.md .gitignore` 应无（除非 RND-233/234 已处理 README 中的域名/身份）。
- 不破坏现有忽略规则（跑 `git status` 确认无意外 tracked 文件）。

## 2. 硬性约束
- 不提交真实密钥；`.env.example` 仅占位符。
- **LICENSE 必须是 AGPL-3.0 全文**（2026-07-29 拍板反转，不要沿用旧版 MIT 模板或任何提示词历史版本里的 MIT 文本）。
- **必须新增 CLA 文档**，且 README/CONTRIBUTING 中有签署指引，否则视为未完成。
- **README 必须包含 WeCom SDK 获取说明**（`backend/vendor/wecom_sdk/` 为腾讯专有、需自行获取），否则视为未完成。
- 内容需与 RND-233/234 的去标识化结果一致（不重复引入真实域名/公司身份）。
- 不改动业务代码；不提交 / 推送。

## 3. 收尾动作
- Linear 评论：LICENSE 类型（AGPL-3.0）+ CLA 文档路径 + README/.gitignore 改动点 + WeCom SDK 说明落点。保留 commit（不自行提交）。
