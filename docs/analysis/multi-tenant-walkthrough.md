# OpenViking 多租户实现讲解

> 面向团队内部分享，降低理解门槛。以代码为准。

---

## 一、一句话概括

OpenViking 的多租户核心思路：**用一把 API Key 确定"你是谁"，然后在请求的整个生命周期里带着这个身份，让文件系统和向量数据库自动隔离数据。**

---

## 二、关键概念只有 4 个

```
┌─────────────────────────────────────────────────────┐
│  Account (账户/工作空间)                              │
│  ├── User A  (用户)                                  │
│  │   ├── Agent 1  (智能体)                           │
│  │   └── Agent 2                                     │
│  └── User B                                          │
│      └── Agent 1                                     │
└─────────────────────────────────────────────────────┘
```

| 概念 | 类比 | 代码位置 |
|------|------|---------|
| **Account** | 一家公司 / 一个租户 | `UserIdentifier.account_id` |
| **User** | 公司里的一个员工 | `UserIdentifier.user_id` |
| **Agent** | 员工使用的一个 AI 助手 | `UserIdentifier.agent_id` |
| **Role** | 员工的权限等级 | `identity.py` 中的 `Role` 枚举 |

角色只有三种，权限递减：

```
ROOT  ──  系统超级管理员，跨所有账户
  │
ADMIN ──  某个账户的管理员，管本账户的用户
  │
USER  ──  普通用户，只能看自己的数据
```

定义在 `openviking/server/identity.py:12-15`：

```python
class Role(str, Enum):
    ROOT  = "root"
    ADMIN = "admin"
    USER  = "user"
```

---

## 三、一个请求的完整生命周期

用一张图看整个流程，**从左到右**就是一次 API 调用的旅程：

```
客户端                    服务端
  │                        │
  │  X-API-Key: abc123     │
  │───────────────────────>│
  │                        │
  │               ┌────────┴────────┐
  │               │ 1. 认证中间件     │  auth.py: resolve_identity()
  │               │ "abc123 是谁？"   │
  │               │                  │
  │               │  查 APIKeyManager │  api_keys.py: resolve()
  │               │  ↓ 先比 root key  │
  │               │  ↓ 再查 user key  │
  │               │                  │
  │               │  得到:            │
  │               │  role=USER       │
  │               │  account=acme    │
  │               │  user=alice      │
  │               └────────┬────────┘
  │                        │
  │               ┌────────┴────────┐
  │               │ 2. 构建上下文     │  auth.py: get_request_context()
  │               │                  │
  │               │  RequestContext(  │
  │               │    user=UserID(  │
  │               │      acme,       │
  │               │      alice,      │
  │               │      default),   │
  │               │    role=USER     │
  │               │  )               │
  │               └────────┬────────┘
  │                        │
  │               ┌────────┴────────┐
  │               │ 3. 权限门卫      │  auth.py: require_role()
  │               │                  │
  │               │  这个接口要求     │
  │               │  ROOT 才能调？    │
  │               │  → 403 拒绝      │
  │               │                  │
  │               │  USER 可以调？    │
  │               │  → 放行 ✓        │
  │               └────────┬────────┘
  │                        │
  │               ┌────────┴────────┐
  │               │ 4. 路由 / 业务    │  routers/filesystem.py 等
  │               │                  │
  │               │  ctx 一路透传     │
  │               │  到 VikingFS     │
  │               │  和 VectorDB    │
  │               └────────┬────────┘
  │                        │
  │               ┌────────┴────────┐
  │               │ 5. 数据隔离      │  viking_fs.py / vector_index
  │               │                  │
  │               │  文件: 自动加     │
  │               │  /acme/ 前缀     │
  │               │                  │
  │               │  向量: 自动加     │
  │               │  account_id 过滤 │
  │               └────────┬────────┘
  │                        │
  │  ← 200 OK (只有 acme   │
  │     账户的数据)         │
  │<───────────────────────│
```

---

## 四、每一层怎么实现的（附代码）

### 4.1 认证：API Key → 身份

入口在 `openviking/server/auth.py:14`。

**开发模式**（没配 root_api_key）：直接返回 ROOT，不需要密钥：

```python
if api_key_manager is None:
    return ResolvedIdentity(
        role=Role.ROOT,
        account_id=x_openviking_account or "default",
        ...
    )
```

**生产模式**：`APIKeyManager.resolve()` 做两步匹配（`api_keys.py:100`）：

```python
def resolve(self, api_key: str) -> ResolvedIdentity:
    # 第一步：是不是 root key？（时间安全比较，防侧信道）
    if hmac.compare_digest(api_key, self._root_key):
        return ResolvedIdentity(role=Role.ROOT)

    # 第二步：在用户密钥索引里查（O(1) 哈希表）
    entry = self._user_keys.get(api_key)
    if entry:
        return ResolvedIdentity(
            role=entry.role,
            account_id=entry.account_id,
            user_id=entry.user_id,
        )

    raise UnauthenticatedError("Invalid API Key")
```

密钥存储结构：

```
AGFS 存储
├── /_system/accounts.json          ← 所有账户列表
├── /acme/_system/users.json        ← acme 账户的所有用户+密钥
└── /beta/_system/users.json        ← beta 账户的所有用户+密钥
```

### 4.2 上下文：身份 → RequestContext

认证完成后，`get_request_context()`（`auth.py:55`）把原始身份打包成 `RequestContext`：

```python
RequestContext(
    user=UserIdentifier(account_id, user_id, agent_id),
    role=Role.USER,
)
```

`RequestContext` 是整个系统的"身份证"，后续所有操作都带着它。

### 4.3 权限门卫：require_role()

路由上用 `require_role()` 声明"这个接口需要什么角色"（`auth.py:69`）：

```python
# Admin API：只有 ROOT 能创建账户
@router.post("/accounts")
async def create_account(
    ctx: RequestContext = require_role(Role.ROOT),    # ← 门卫
): ...

# 注册用户：ROOT 或 ADMIN 都行
@router.post("/accounts/{account_id}/users")
async def register_user(
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),  # ← 门卫
): ...
```

ADMIN 还有额外限制——只能管自己账户的用户（`admin.py:44`）：

```python
def _check_account_access(ctx, account_id):
    if ctx.role == Role.ADMIN and ctx.account_id != account_id:
        raise PermissionDeniedError(...)
```

### 4.4 文件隔离：URI → 物理路径自动加 account 前缀

这是数据隔离的核心。当业务代码操作 `viking://user/memories/...` 时，VikingFS 自动把 account_id 注入路径（`viking_fs.py:895`）：

```python
def _uri_to_path(self, uri, ctx=None):
    real_ctx = self._ctx_or_default(ctx)
    account_id = real_ctx.account_id
    _, parts = self._normalized_uri_parts(uri)
    return f"/local/{account_id}/{'/'.join(parts)}"
```

**具体效果**：

```
业务代码看到的 URI（虚拟路径）         实际存储路径（物理路径）
─────────────────────────────────────────────────────────────
viking://user/memories/notes     →  /local/acme/user/memories/notes
viking://agent/memories/cases    →  /local/acme/agent/memories/cases
viking://resources/docs          →  /local/acme/resources/docs
```

**不同账户写同一个 URI，数据完全隔离**：

```
acme 的 alice 写 viking://user/memories/notes
  → /local/acme/user/memories/notes        ← acme 的目录

beta 的 bob 写 viking://user/memories/notes
  → /local/beta/user/memories/notes        ← beta 的目录，互不干扰
```

反向转换（`_path_to_uri`，`viking_fs.py:925`）在返回结果时把 account_id 剥掉，业务代码完全感知不到多租户前缀。

### 4.5 用户/Agent 空间隔离

同一个账户内，不同用户的数据也隔离。靠 `_is_accessible()` 在文件遍历时过滤（`viking_fs.py:972`）：

```python
def _is_accessible(self, uri, ctx):
    if ctx.role == Role.ROOT:
        return True       # ROOT 看一切

    scope = parts[0]      # uri 的第一段，如 "user", "agent", "resources"

    if scope in {"resources", "temp", "transactions"}:
        return True       # 共享区域，所有人可见

    if scope == "_system":
        return False      # 系统目录，普通用户不可见

    # 关键：检查空间归属
    space = self._extract_space_from_uri(uri)
    if scope in {"user", "session"}:
        return space == ctx.user.user_space_name()    # user_id
    if scope == "agent":
        return space == ctx.user.agent_space_name()   # md5(user_id+agent_id)[:12]
```

**空间名计算**（`user_id.py:48-54`）：

```python
def user_space_name(self):       # 用户空间 = user_id 本身
    return self._user_id         # 如 "alice"

def agent_space_name(self):      # Agent 空间 = md5(user_id + agent_id) 取前 12 位
    return hashlib.md5(
        (self._user_id + self._agent_id).encode()
    ).hexdigest()[:12]           # 如 "a1b2c3d4e5f6"
```

### 4.6 向量数据库隔离

VectorDB 的每条记录都带 `account_id` 和 `owner_space` 字段。查询时自动注入过滤条件（`viking_vector_index_backend.py:501`）：

```python
@staticmethod
def _tenant_filter(ctx, context_type=None):
    if ctx.role == Role.ROOT:
        return None              # ROOT 不过滤

    user_spaces = [ctx.user.user_space_name(), ctx.user.agent_space_name()]
    account_filter = Eq("account_id", ctx.account_id)

    if context_type == "resource":
        # resource 额外包含空 owner_space（账户共享资源）
        return And([account_filter, In("owner_space", [*user_spaces, ""])])
    if context_type in {"memory", "skill"}:
        # memory/skill 只看自己的空间
        return And([account_filter, In("owner_space", user_spaces)])
    ...
```

**效果**：USER 角色的 alice 搜索 memory 时，VectorDB 查询自动变成：

```
WHERE account_id = 'acme'
  AND owner_space IN ('alice', 'a1b2c3d4e5f6')
```

---

## 五、路由层如何透传上下文

所有业务路由都通过 FastAPI 的 `Depends` 机制注入 `RequestContext`，然后传给服务层：

```python
# filesystem.py — 每个接口都是这个模式
@router.get("/ls")
async def ls(
    uri: str = Query(...),
    _ctx: RequestContext = Depends(get_request_context),  # ← 自动注入
):
    service = get_service()
    result = await service.fs.ls(uri, ctx=_ctx, ...)      # ← 透传给服务
    return Response(status="ok", result=result)
```

不只是 filesystem，**所有路由模块**（content, search, sessions, resources, relations, pack, observer, debug）都遵循同样的模式。

---

## 六、Admin API：账户和用户管理

管理操作通过 `/api/v1/admin/` 下的接口完成：

```
操作             接口                                           谁能调
───────────────────────────────────────────────────────────────────────
创建账户          POST   /admin/accounts                        ROOT
列出账户          GET    /admin/accounts                        ROOT
删除账户          DELETE /admin/accounts/{id}                   ROOT
注册用户          POST   /admin/accounts/{id}/users             ROOT, ADMIN
列出用户          GET    /admin/accounts/{id}/users             ROOT, ADMIN
删除用户          DELETE /admin/accounts/{id}/users/{uid}       ROOT, ADMIN
改角色            PUT    /admin/accounts/{id}/users/{uid}/role  ROOT
重新生成密钥      POST   /admin/accounts/{id}/users/{uid}/key   ROOT, ADMIN
```

**创建账户时自动初始化目录结构**（`admin.py:62-68`）：

```python
user_key = await manager.create_account(body.account_id, body.admin_user_id)
await service.initialize_account_directories(account_ctx)   # 创建共享目录
await service.initialize_user_directories(account_ctx)      # 创建用户目录
```

**删除账户时级联清理**（`admin.py:106-129`）：

```
1. 删 AGFS 文件    →  rm viking://user/, agent/, session/, resources/
2. 删 VectorDB 记录 →  delete_account_data(account_id)
3. 删密钥索引       →  manager.delete_account(account_id)
```

---

## 七、一张总览图

```
                          ┌──────────────────────────────────────┐
                          │           客户端请求                  │
                          │  Header: X-API-Key: <key>            │
                          │  Header: X-OpenViking-Agent: bot1    │
                          └──────────────┬───────────────────────┘
                                         │
                          ┌──────────────▼───────────────────────┐
                          │  auth.py: resolve_identity()         │
                   ┌──────│  APIKeyManager.resolve(key)          │
                   │      │  → ResolvedIdentity                  │
                   │      └──────────────┬───────────────────────┘
                   │                     │
                   │      ┌──────────────▼───────────────────────┐
     ┌─────────┐   │      │  auth.py: get_request_context()      │
     │ api_keys│   │      │  → RequestContext(                   │
     │  .py    │◄──┘      │      user=(acme, alice, bot1),       │
     │         │          │      role=USER                       │
     │ 密钥存储 │          │    )                                 │
     │ + 解析   │          └──────────────┬───────────────────────┘
     └─────────┘                         │
                          ┌──────────────▼───────────────────────┐
                          │  require_role() 权限检查              │
                          └──────────────┬───────────────────────┘
                                         │
                   ┌─────────────────────┼──────────────────────┐
                   │                     │                      │
        ┌──────────▼─────┐    ┌──────────▼─────┐    ┌──────────▼──────┐
        │   VikingFS     │    │   VectorDB     │    │  Service 层     │
        │                │    │                │    │                 │
        │ _uri_to_path() │    │ _tenant_filter │    │ 业务逻辑        │
        │ 注入 /acme/    │    │ 注入 WHERE     │    │ ctx 透传        │
        │ 前缀           │    │ account_id=    │    │                 │
        │                │    │ 'acme'         │    │                 │
        │ _is_accessible │    │                │    │                 │
        │ 检查空间归属    │    │ owner_space    │    │                 │
        │                │    │ 检查           │    │                 │
        └────────────────┘    └────────────────┘    └─────────────────┘
```

---

## 八、常见问题

**Q: 开发时不想配密钥怎么办？**
A: 不在 `ov.conf` 里配 `root_api_key` 即可。此时所有请求自动获得 ROOT 权限，account/user/agent 都是 "default"。

**Q: 业务代码需要感知多租户吗？**
A: 不需要。业务代码只操作 `viking://` 虚拟 URI，VikingFS 在底层自动注入 account 前缀，上层完全透明。

**Q: 一个账户内，用户能看到其他用户的数据吗？**
A: USER 角色不能。`_is_accessible()` 确保 USER 只能访问自己的 user space 和 agent space。但 `resources/` 目录是账户内共享的，所有用户都能访问。

**Q: ROOT 用户能指定以哪个账户身份操作吗？**
A: 能。ROOT 用户可以通过 `X-OpenViking-Account` 和 `X-OpenViking-User` Header 指定目标账户和用户（`auth.py:49-51`）。
