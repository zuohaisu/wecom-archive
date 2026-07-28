# RND-323 开发 agent 执行提示词 —— 登录后自动选中监控账号（单账号默认 / 多账号恢复上次选中，按租户+用户双重隔离）

> 面向开发 agent（单人端到端实现 RND-323）。本文件即你的完整 brief。
> 全程不执行 `git commit` / `git push`（由用户本人操作）。只改工作树，交用户 Review。
> 代码标识符一律加反引号。架构冻结 D1：SSR + 原生 JS（不引 React）。
>
> ⚠️ 本票 issue 已于 2026-07-28 修订（status=Todo），**持久化 key 已拍板为 `rnd.lastEntity.<tenantId>.<userId>`（按租户 + 按用户双重隔离）**，且含一个**小后端改动**：给 `/api/auth/me` 补 `id` 字段（`tenant_id` 已在 `auth.py:865` 返回）。以下按修订后的权威决策执行。

## 一、任务（一句话）

登录进审阅台后，对 **staff（监控账号）模式** 做智能自动选中：仅 1 个 → 默认选中并自动加载会话/时间线；多个 → 恢复「上次选中的那个」。偏好按 `rnd.lastEntity.<tenantId>.<userId>` 持久化（同浏览器多租户、同租户多用户互不串味）。**含一个极小后端改动**：`/api/auth/me` 返回体补 `id`（AdminUser.id）。纯前端 `localStorage`，无新依赖。

## 二、前置依赖（开工前必查，任一不满足 → 停下并报告）

本任务**无外部 BLOCKED**。确认落点存在：

```bash
cd /Users/zuohaisu/Documents/code/wecom-archive-365
grep -n "function renderEntityList"   backend/app/web/static/console/conversation-list.js   # 期望 L13
grep -n "function onEntityClick"     backend/app/web/static/console/conversation-list.js   # 期望 L43
grep -n "function loadEntityList"    backend/app/web/static/console/api-client.js          # 期望 L26
grep -n "function loadCurrentUser"   backend/app/web/static/console/console-state.js        # 期望 L1
grep -n "function selectEntityIfPresent" backend/app/web/static/console/console-entry.js    # 期望 L269
grep -n '"tenant_id": session.tenant_id' backend/app/routers/auth.py                        # 应已命中（L865）
grep -n "loadCurrentUser();" backend/app/web/static/console/console-entry.js                # 启动入口，期望 L559
```

`/api/auth/me` 当前（auth.py:860-867）已返回 `authenticated/wecom_user_id/display_name/tenant_id/role`，**缺 `id`** —— 本票补一行。

## 三、决策背景（已全部拍板，不要再问）

来源：Linear `RND-323`（area: frontend / type: feature），**修订后**描述。

- **复用既有钩子，不重复实现**：`console-entry.js:269 selectEntityIfPresent(wecomUserId)` 已实现「按 id 在已渲染列表中选中并返回是否命中」，自动选中与手动选中都走它。
- **OPEN DECISION #1（持久化 key）已决议 = 双重隔离 `rnd.lastEntity.<tenantId>.<userId>`**：
  - `tenantId` 来源：`/api/auth/me` **已返** `tenant_id`（auth.py:865，字段已存在，直接用）。
  - `userId` 来源：`/api/auth/me` 的 `id`（= `AdminUser.id`，全局唯一主键）。**当前不返回** → 本票小后端改动：`auth.py:860` 返回体补 `"id": user.id`（一行，不引新依赖）。
  - ⚠️ **`wecom_user_id` 不可作用户维度**：密码模式 `auth.py:857-858` 将其置 `None`，非密码模式才暴露真实企微 userid，不稳定。故必须用 `AdminUser.id`。
  - 退化（不推荐，仅备注）：若暂不改 `/api/auth/me`，可用 `window.TENANTID` + `window.USERID` 服务端注入兜底；但**优先补 `id` 字段**（本票采用）。
  - **健壮性**：`tenantId`/`userId` 缺失时「不写入也不读取」（降级为空态），见 §5.6。
- **OPEN DECISION #2（多账号 + 上次选中失效）已决议**：保持空态，**不**自动选第一个。
- **OPEN DECISION #3（contact 对称自动选中）已决议**：本期不做，contact 模式维持现状。
- **范围守门**：后端改动**仅** `routers/auth.py` 的 `/api/auth/me` 返回体（一行）；**不新增** router 文件、**不改** `main.py`、不引 React、不引新依赖；前端只动 `console/*.js`。

## 四、项目现状（精确落点）

- `backend/app/routers/auth.py:860-867` `auth_me` 返回体：当前 `{authenticated, wecom_user_id, display_name, tenant_id, role}` —— **缺 `id`**。
- `backend/app/web/static/console/console-state.js:1-7` `loadCurrentUser()`：fetch `/api/auth/me`，仅设 `#current-user` 文本；**未捕获 `tenant_id`/`id`，未返回 promise**。启动时被 `console-entry.js:559 loadCurrentUser();` 调用。
- `backend/app/web/static/console/api-client.js:26-31` `loadEntityList()`：`url=mode==='staff'?'/api/monitored-accounts':'/api/contacts'`；`.then(items=>renderEntityList(items))`；返回 promise（search 等会 `await`）。
- `backend/app/web/static/console/conversation-list.js:13-42` `renderEntityList(items)`：L17 零项早返空态；L18-36 渲染 `.entity-item`（data-id=`item.staff_id`）；L37-41 若 `selEntityId` 已设则高亮。**在 L41 后插入 auto-select 调用（守卫 `mode==='staff' && !selEntityId`）**。
- `backend/app/web/static/console/conversation-list.js:43-55` `onEntityClick(el)`：L44 设 `selEntityId=el.dataset.id`。**L44 后（仅 staff）调用 `persistLastEntity`**。
- `backend/app/web/static/console/console-entry.js:269-277` `selectEntityIfPresent(wecomUserId)`：命中 `.entity-item[data-id===wecomUserId]` 则 `onEntityClick(el)`。**复用，不改动。**
- `backend/app/web/static/console/console-entry.js:31-48` `applyLocale()`：L38 调 `renderEntityList(lastEntityItems)`；因守卫（已选中时不触发 auto-select）保持；L41-43 仅 `!selEntityId` 显示 `conv-body` 空态。**无需改动。**

## 五、实现步骤（精确）

### 5.1 后端：`/api/auth/me` 补 `id`（auth.py:860，一行）

在返回体 `tenant_id` 之后加一行（不改动其他字段、不引依赖）：

```python
        {
            "authenticated": True,
            "wecom_user_id": exposed_wecom_id,
            "display_name": user.name or user.wecom_user_id,
            "tenant_id": session.tenant_id,
            "role": user.role,
            "id": user.id,            # RND-323: 供前端「按用户隔离」持久化 key（AdminUser.id，全局唯一）
        }
```

### 5.2 前端：`console-state.js` 捕获 `tenant_id` + `id` 并返回 promise（L1-7 改写）

```js
var currentTenantId=null, currentUserId=null, authMePromise=null;   // RND-323: 供自动选中 key 隔离
function loadCurrentUser(){
  authMePromise=fetch('/api/auth/me').then(function(r){return r.json();}).then(function(d){
    if(!d.authenticated){window.location.href='/admin/login';return;}
    currentTenantId=d.tenant_id||null;          // RND-323
    currentUserId=d.id||null;                    // RND-323
    var el=document.getElementById('current-user');
    if(el)el.textContent=d.display_name||d.wecom_user_id||'';
  }).catch(function(){});
  return authMePromise;                          // RND-323: 供 loadEntityList 等待
}
```
> 其余 `console-state.js` 全局（mode/selEntityId 等）不动。`console-entry.js:559` 的 `loadCurrentUser();` 调用无需改（返回值忽略即可）。

### 5.3 前端：`api-client.js` loadEntityList 等待 auth/me 解析（L26-31 改写开头）

确保 auto-select 读取 key 前 `tenant_id`/`id` 已就绪：

```js
function loadEntityList(){
  var url=mode==='staff'?'/api/monitored-accounts':'/api/contacts';
  document.getElementById('entity-body').innerHTML='<div class="loading">'+I18N.t('console.loading')+'</div>';
  // RND-323: 先等 /api/auth/me 解析，保证 currentTenantId/currentUserId 已设置
  var pre=(typeof authMePromise!=='undefined' && authMePromise) ? authMePromise : null;
  var chain=pre ? pre.then(function(){return fetch(url);}) : fetch(url);
  return chain.then(function(r){if(handleUnauth(r))return null;return r.json();}).then(function(items){if(items)renderEntityList(items);})
    .catch(function(){document.getElementById('entity-body').innerHTML='<div class="error-msg">'+I18N.t('console.failedToLoadEntities')+'</div>';});
}
```
> 仅此一处变更；保持其余语义（staff→`/api/monitored-accounts`，返回 promise 供 search 链 `await`）。

### 5.4 前端：`conversation-list.js` 持久化/读取 helper（放在 `renderEntityList` 之前）

```js
// RND-323: 登录后自动选中监控账号（staff only）——按租户+用户双重隔离持久化
function _lastEntityStorageKey(){
  // tenant_id/user_id 缺一不可：缺失则退化为非隔离 key（读写均视为「无上次选中」→ 降级空态）
  if(currentTenantId && currentUserId) return 'rnd.lastEntity.'+currentTenantId+'.'+currentUserId;
  return 'rnd.lastEntity';
}
function persistLastEntity(id){
  if(!id)return;
  try{localStorage.setItem(_lastEntityStorageKey(),id);}catch(e){/* storage 不可用：静默跳过 */}
}
function readLastEntity(){
  try{return localStorage.getItem(_lastEntityStorageKey());}catch(e){return null;}
}
```

### 5.5 在 `renderEntityList` 末尾、L41 之后插入 auto-select 触发

```js
  // RND-323: 仅 staff 模式、且本会话尚未选中任何实体时尝试自动选中
  if(mode==='staff' && !selEntityId){ maybeAutoSelectEntity(items); }
```

### 5.6 新增 `maybeAutoSelectEntity(items)`（紧接 `renderEntityList` 之后）

```js
// RND-323: 自动选中逻辑（复用 selectEntityIfPresent，不重复实现）
function maybeAutoSelectEntity(items){
  if(mode!=='staff' || selEntityId)return;          // contact 模式 / 已选中 → 跳过
  if(!items || !items.length)return;               // 0 项 → 保持空态
  if(items.length===1){                            // 单账号 → 默认选中
    selectEntityIfPresent(items[0].staff_id);
    return;
  }
  // 多账号 → 恢复上次选中（须仍存在）。tenant/user 缺失 → 降级：不读取、不自动选，保持空态
  if(!(currentTenantId && currentUserId))return;
  var last=readLastEntity();
  if(last && items.some(function(it){return it.staff_id===last;})){
    selectEntityIfPresent(last);
  }
}
```

### 5.7 在 `onEntityClick` 持久化（仅 staff 模式）

`conversation-list.js:44` 之后追加一行（保持原 L44-55 其余不变）：

```js
  selEntityId=el.dataset.id; selEntityName=el.dataset.name; selConvId=null; selConvName=null;
  if(mode==='staff')persistLastEntity(selEntityId);   // RND-323: 记录上次选中（按租户+用户隔离）
  timelineConvId=null; timelineMsgs=[]; timelineHasOlder=false; timelineNextBefore=null;
```
> 单账号自动选中时 `selectEntityIfPresent`→`onEntityClick` 也会走到这里，自然持久化；手动点击同理。幂等。

### 5.8 不改动清单（务必遵守）

- `backend/app/web/static/console/console-entry.js` 的 `selectEntityIfPresent` / `applyLocale` 语义不变。
- `backend/app/routers/auth.py` 仅 `/api/auth/me` 返回体加一行 `id`；**不**改登录/WeCom/登出/其他端点、**不**新增 router、**不**改 `main.py`。
- 不新建 Alembic 迁移、不引新依赖、不引 React。

## 六、RED → GREEN（量化验收信号）

- **RED（当前/未实现）**：登录后 `selEntityId` 恒 `null`，`localStorage` 无 `rnd.lastEntity.*` 写入；`/api/auth/me` 无 `id` 字段。
- **GREEN（实现后）**：见 QA 提示词「验收映射」9 项全绿 + node harness 测试（见 QA 提示词 §五）通过。

## 七、硬约束

- **不 `git commit` / `git push`**（用户本人操作）。
- 后端改动仅 `auth.py:860` 返回体一行（补 `id`）；不改动鉴权/登录/其他端点/路由注册。
- 不改动 `selectEntityIfPresent` / `applyLocale` 既有语义。
- `localStorage` 读写必须 `try/catch` 静默（隐私模式 / 存储禁用时不报错、不阻塞）。
- 仅 **staff 模式** 生效；contact 模式行为与现状完全一致。
- 不引入新依赖；不引 React（D1 冻结）。
- 代码标识符一律加反引号（Linear markdown `_` 解析问题）。

## 八、收尾

- 不 commit；交用户 Review。
- 确保 QA 能跑 node harness（见 QA 提示词 §五）：新增 `backend/tests/test_rnd323_entity_autoselect.py`。
- 建议本地 `python -m uvicorn app.main:app` 起服务真实验证：① 单账号登录自动加载 ② 多账号手动选 A 后刷新自动恢复 ③ 同浏览器切租户 X/Y 互不影响 ④ 同租户不同账号 U1/U2 各自恢复 ⑤ lang 切换保持 ⑥ 搜索后选中不被抢占。
