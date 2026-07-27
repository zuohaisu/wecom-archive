# RND-237 执行提示词（开源发布包装：LICENSE / 公开 README / .gitignore 复核）

> 用途：粘贴给开发 agent（或 Haisu 自行执行）。关联 epic：RND-232。
> 不自行 commit / push。

## 0. 任务与来源
- RND-237「开源发布包装：LICENSE / 公开 README / .gitignore 复核」，父 epic RND-232。
- 现状：仓库**无 LICENSE**；`.gitignore` 存在需复核；根 `README.md` 存在需复核是否泄露身份 / 密钥。

## 1. 执行步骤

### 阶段一：复核现状（只读）
- 读 `.gitignore`，确认含：`.env`、`.env.*`、`.qoder/`、`__pycache__/`、`*.pyc`、构建产物、密钥文件；`.env.example` 应**允许**（仅占位符）。
- 读 `README.md`，检查是否含真实域名（`crowntime.cn`）、公司身份、密钥、内部地址；与 RND-233 协调（README 若含域名，在 RND-233 阶段一并改）。
- 确认是否已有 LICENSE 引用（如包元数据 `pyproject.toml`）。

### 阶段二：补齐发布包装
- **LICENSE**：新增 `LICENSE` 文件。**许可证类型已确认：MIT**（2026-07-26 Haisu 确认；放弃 Apache-2.0 的专利条款路线）。下方为 MIT 定稿模板，仅 `年份` 与 `版权人` 须替换（待 Haisu 提供）：
  ```
  MIT License

  Copyright (c) 2026 <版权人待 Haisu 确认>

  Permission is hereby granted, free of charge, to any person obtaining a copy
  of this software and associated documentation files (the "Software"), to deal
  in the Software without restriction, including without limitation the rights
  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  copies of the Software, and to permit persons to whom the Software is
  furnished to do so, subject to the following conditions:

  The above copyright notice and this permission notice shall be included in all
  copies or substantial portions of the Software.

  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  SOFTWARE.
  ```
- **README**：若存在身份 / 密钥泄露，移除或泛化；确保含：项目简介、架构概述、快速开始（指向 `.env.example`）、部署说明（引用 RND-233 的 `ARCHIVE_DOMAIN`）、许可证段落。不杜撰未实现功能。
- **.gitignore**：补齐缺失项（至少 `.qoder/`、`.env`、本地密钥），确认不忽略 `.env.example`。

### 阶段三：验证
- `test -f LICENSE && echo ok`；`.gitignore` 含 `.qoder/` 与 `.env`。
- `grep -rn "crowntime\|康冠\|ICP备" README.md .gitignore` 应无（除非 RND-233 已处理 README 中的域名）。
- 不破坏现有忽略规则（跑 `git status` 确认无意外 tracked 文件）。

## 2. 硬性约束
- 不提交真实密钥；`.env.example` 仅占位符。
- LICENSE 已确认为 **MIT**；仅年份与版权人留待 Haisu 提供（不要写死错误主体）。
- 不改动业务代码；不提交 / 推送。

## 3. 收尾动作
- Linear 评论：LICENSE 类型 + README / .gitignore 改动点。保留 commit。
