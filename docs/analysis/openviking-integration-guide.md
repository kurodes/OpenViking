# OpenViking 多租户集成指南

> 面向上层应用开发者。说明如何在"公司 → 用户"两级租户模型下，正确调用 OpenViking 的多租户 API。

---

## 一、概念映射

上层应用只有两层租户：**公司**和**用户**。OpenViking 的多租户模型天然对齐：

```
上层应用                     OpenViking
────────                    ──────────
公司 (Company)          →   Account
用户 (User)             →   User
公司共享知识库           →   viking://resources/     (Account 内所有用户可访问)
用户私有记忆/对话        →   viking://user/{uid}/    (仅本人可访问)
用户的某个 AI Agent     →   viking://agent/{hash}/  (仅本人+该 Agent 可访问)
```

数据隔离规则：

| 需求 | 实现机制 | 效果 |
|------|---------|------|
| 公司间隔离 | 文件路径自动加 `/{account_id}/` 前缀；向量查询自动加 `account_id` 过滤 | 公司 A 完全看不到公司 B 的任何数据 |
| 用户间隔离 | `user/`、`agent/`、`session/` 目录按空间归属检查；向量查询按 `owner_space` 过滤 | 同公司内 alice 看不到 bob 的记忆 |
| 用户访问公司知识库 | `resources/` 目录对账户内所有用户开放；向量查询中 resource 类型包含共享空间 | alice 和 bob 都能搜到公司文档 |

**所有隔离都是 OpenViking 自动完成的，上层应用只需要传对 API Key。**

---

## 二、部署 OpenViking Server

### 配置文件 `ov.conf`

唯一必须的多租户配置项是 `root_api_key`：

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 1933,
    "root_api_key": "用 secrets.token_hex(32) 生成一个随机字符串"
  },
  "storage": {
    "vectordb": { "backend": "local", "path": "./data" },
    "agfs": { "port": 1833, "path": "./data", "backend": "local" }
  },
  "embedding": { "...": "按需配置" },
  "vlm": { "...": "按需配置" }
}
```

- 设了 `root_api_key` → **生产模式**：所有请求必须带 API Key，身份从 Key 解析
- 不设 → **开发模式**：无需认证，所有请求自动以 ROOT/default 身份执行

### 启动并验证

```bash
openviking-server --config ov.conf

curl http://localhost:1933/health
# {"status": "ok", "healthy": true}
```

---

## 三、管理面：公司和用户的生命周期

管理操作需要 ROOT Key，对应上层应用中的后台管理逻辑。

### 3.1 公司入驻 → 创建 Account

```python
import httpx

OV_URL = "http://openviking-server:1933"
ROOT_KEY = "你的 root_api_key"
ROOT_HEADERS = {"X-API-Key": ROOT_KEY}

def on_company_created(company_id: str, first_admin_username: str) -> str:
    """上层应用创建公司时调用。返回该公司管理员的 OpenViking User Key。"""
    resp = httpx.post(
        f"{OV_URL}/api/v1/admin/accounts",
        headers=ROOT_HEADERS,
        json={
            "account_id": company_id,
            "admin_user_id": first_admin_username,
        },
    )
    resp.raise_for_status()
    return resp.json()["result"]["user_key"]  # ← 保存到上层应用数据库
```

### 3.2 员工加入 → 注册 User

由公司管理员（ADMIN）创建员工，无需使用 ROOT Key：

```python
def on_user_joined(admin_key: str, company_id: str, user_id: str, role: str = "user") -> str:
    """公司管理员添加员工时调用。返回该用户的 OpenViking User Key。

    admin_key: 该公司管理员的 User Key
    role 可选值：
    - "user":  普通用户，只能访问自己的空间 + 公司共享知识库
    - "admin": 公司管理员，可以管理本公司的用户
    """
    resp = httpx.post(
        f"{OV_URL}/api/v1/admin/accounts/{company_id}/users",
        headers={"X-API-Key": admin_key},
        json={"user_id": user_id, "role": role},
    )
    resp.raise_for_status()
    return resp.json()["result"]["user_key"]  # ← 保存到上层应用数据库
```

> ROOT Key 同样可以调用此接口，但推荐由公司 ADMIN 管理自己的员工，符合最小权限原则。

### 3.3 员工离开 → 删除 User

```python
def on_user_left(admin_key: str, company_id: str, user_id: str):
    """公司管理员移除员工时调用。该用户的 Key 立即失效。"""
    resp = httpx.delete(
        f"{OV_URL}/api/v1/admin/accounts/{company_id}/users/{user_id}",
        headers={"X-API-Key": admin_key},
    )
    resp.raise_for_status()
```

### 3.4 公司注销 → 删除 Account

```python
def on_company_removed(company_id: str):
    """公司注销时调用。级联删除：所有用户 Key + 文件 + 向量数据。"""
    resp = httpx.delete(
        f"{OV_URL}/api/v1/admin/accounts/{company_id}",
        headers=ROOT_HEADERS,
    )
    resp.raise_for_status()
```

### 3.5 其他管理操作

| 操作 | 接口 | 权限 |
|------|------|------|
| 列出所有公司 | `GET /api/v1/admin/accounts` | ROOT |
| 列出公司内用户 | `GET /api/v1/admin/accounts/{company_id}/users` | ROOT, ADMIN |
| 变更用户角色 | `PUT /api/v1/admin/accounts/{company_id}/users/{uid}/role` | ROOT |
| 重新生成 Key | `POST /api/v1/admin/accounts/{company_id}/users/{uid}/key` | ROOT, ADMIN |

---

## 四、业务面：用户日常操作

业务调用只需要在 Header 中带上该用户的 Key。OpenViking 自动从 Key 解析出 company + user 身份，自动隔离数据。

```python
def make_headers(user_key: str, agent_id: str = "default") -> dict:
    """构建业务请求的 Header。"""
    headers = {"X-API-Key": user_key}
    if agent_id != "default":
        headers["X-OpenViking-Agent"] = agent_id
    return headers
```

### 4.1 导入公司知识库

公司管理员（ADMIN）把文档导入 `resources/`，全公司可搜索：

```python
def upload_company_doc(admin_key: str, file_path: str):
    """管理员上传公司文档。存入 resources/，全公司可访问。"""
    # 先上传文件
    headers = make_headers(admin_key)
    with open(file_path, "rb") as f:
        resp = httpx.post(
            f"{OV_URL}/api/v1/resources/temp_upload",
            headers=headers,
            files={"file": f},
        )
    temp_path = resp.json()["result"]["temp_path"]

    # 再添加为资源（触发解析、索引）
    resp = httpx.post(
        f"{OV_URL}/api/v1/resources",
        headers=headers,
        json={"temp_path": temp_path, "wait": True},
    )
    return resp.json()["result"]
```

### 4.2 语义搜索

用户搜索时，自动范围 = **公司共享知识库 + 用户私有记忆**：

```python
def search(user_key: str, query: str, session_id: str = None, limit: int = 5):
    """语义搜索。自动包含公司知识库和用户私有记忆。"""
    headers = make_headers(user_key)
    body = {"query": query, "limit": limit}
    if session_id:
        body["session_id"] = session_id  # 关联会话，搜索更精准

    resp = httpx.post(f"{OV_URL}/api/v1/search/search", headers=headers, json=body)
    return resp.json()["result"]
```

不带 session_id 的简单搜索用 `/api/v1/search/find`。

### 4.3 会话管理 + 记忆提取

```python
def create_session(user_key: str) -> str:
    """创建对话会话。"""
    resp = httpx.post(f"{OV_URL}/api/v1/sessions", headers=make_headers(user_key))
    return resp.json()["result"]["session_id"]

def add_message(user_key: str, session_id: str, role: str, content: str):
    """向会话中添加一条消息。role: 'user' 或 'assistant'。"""
    httpx.post(
        f"{OV_URL}/api/v1/sessions/{session_id}/messages",
        headers=make_headers(user_key),
        json={"role": role, "content": content},
    )

def commit_session(user_key: str, session_id: str):
    """对话结束后提交。自动：归档对话 + 提取长期记忆。

    提取的记忆存入用户私有空间，只有该用户能搜到。
    """
    httpx.post(
        f"{OV_URL}/api/v1/sessions/{session_id}/commit",
        headers=make_headers(user_key),
    )
```

### 4.4 浏览和读取内容

```python
headers = make_headers(user_key)

# 列出公司知识库目录
httpx.get(f"{OV_URL}/api/v1/fs/ls", headers=headers, params={"uri": "viking://resources/"})

# 读文档摘要（几句话）
httpx.get(f"{OV_URL}/api/v1/content/abstract", headers=headers, params={"uri": "viking://resources/report.pdf"})

# 读文档概览（结构化详细总结）
httpx.get(f"{OV_URL}/api/v1/content/overview", headers=headers, params={"uri": "viking://resources/report.pdf"})

# 读原始内容
httpx.get(f"{OV_URL}/api/v1/content/read", headers=headers, params={"uri": "viking://resources/report.pdf"})
```

### 4.5 Python SDK 方式（推荐）

以上所有操作也可以用 `openviking` Python 包，更简洁：

```python
import openviking as ov

async def example(user_key: str):
    client = ov.AsyncHTTPClient(
        url="http://openviking-server:1933",
        api_key=user_key,
        agent_id="default",
    )
    await client.initialize()

    # 搜索
    results = await client.search(query="报销流程", limit=5)

    # 会话
    session = client.session()
    await session.add_message(role="user", content="怎么报销差旅费？")
    await session.add_message(role="assistant", content="根据公司规定...")
    results = await client.search(query="报销上限", session=session, limit=5)
    await session.commit()

    # 文件操作
    entries = await client.ls("viking://resources/")
    abstract = await client.abstract("viking://resources/handbook.pdf")

    await client.close()
```

同步版本用 `ov.SyncHTTPClient`，方法名一致。

---

## 五、数据空间详解

一个公司（Account）内部的数据布局：

```
viking://                              可见性
├── resources/                         公司全员可读写
│   ├── 公司手册.pdf                   ← 公司共享知识库
│   ├── 产品文档/
│   └── FAQ.md
│
├── user/
│   ├── alice/                         仅 alice 可见
│   │   └── memories/                  ← alice 的私有记忆
│   └── bob/                           仅 bob 可见
│       └── memories/                  ← bob 的私有记忆
│
├── agent/
│   ├── {md5(alice+bot1)[:12]}/        仅 alice 使用 bot1 时可见
│   │   └── memories/                  ← alice + bot1 的 Agent 记忆
│   └── {md5(bob+bot1)[:12]}/         仅 bob 使用 bot1 时可见
│       └── memories/
│
└── session/
    ├── alice/                         仅 alice 可见
    │   └── {session_id}/              ← alice 的对话记录
    └── bob/                           仅 bob 可见
        └── {session_id}/
```

搜索时 OpenViking 根据身份自动决定范围：

```
alice 搜索 "报销流程"
  → 文件系统: resources/* ✓  user/alice/* ✓  user/bob/* ✗
  → 向量数据库: WHERE account_id='公司A'
                 AND (
                   (context_type='resource' AND owner_space IN ('alice', 'agent_hash', ''))
                   OR
                   (context_type='memory'   AND owner_space IN ('alice', 'agent_hash'))
                 )
```

---

## 六、Key 管理建议

```
┌────────────────────────────────────────────────────────────┐
│                     上层应用后端                            │
│                                                            │
│  环境变量: OPENVIKING_ROOT_KEY=xxx                         │
│                                                            │
│  数据库表: openviking_keys                                 │
│  ┌──────────────┬───────────┬──────────────────────┐       │
│  │ company_id   │ user_id   │ ov_user_key          │       │
│  ├──────────────┼───────────┼──────────────────────┤       │
│  │ acme         │ alice     │ a3f8...              │       │
│  │ acme         │ bob       │ 7c2d...              │       │
│  │ beta         │ charlie   │ e91a...              │       │
│  └──────────────┴───────────┴──────────────────────┘       │
│                                                            │
│  调用 OpenViking 时:                                        │
│    key = db.get_ov_key(company_id, user_id)                │
│    headers = {"X-API-Key": key}                            │
└────────────────────────────────────────────────────────────┘
```

- **Root Key**: 通过环境变量注入，仅后端管理逻辑使用，不暴露给前端
- **User Key**: 创建用户时拿到，存入数据库；每次调 OpenViking 时查出来放 Header
- **Key 轮换**: 调 `POST /admin/.../key` 后旧 Key 立即失效，需同步更新数据库

---

## 七、Agent ID 的使用场景

如果同一个用户有多个独立的 AI 场景（如"客服助手"和"数据分析助手"），用 `X-OpenViking-Agent` Header 区分：

```python
# 客服场景 — 有独立的 agent 记忆空间
headers_cs = {"X-API-Key": alice_key, "X-OpenViking-Agent": "customer-service"}

# 数据分析场景 — 有另一套独立的 agent 记忆空间
headers_da = {"X-API-Key": alice_key, "X-OpenViking-Agent": "data-analysis"}
```

两个 Agent 各自拥有**独立的 agent 记忆**，但共享：
- alice 的 user 级私有记忆
- 公司的 resources 知识库

如果上层应用没有多 Agent 场景，不传此 Header 即可（默认为 `"default"`）。

---

## 八、完整生命周期一览

```
时间线 ──────────────────────────────────────────────────────────────>

1. 部署
   启动 OpenViking Server，配好 root_api_key

2. 公司 A 入驻
   POST /admin/accounts  {"account_id": "A", "admin_user_id": "admin_a"}
   → 拿到 admin_a_key，存入数据库

3. 管理员为公司 A 添加员工 alice
   POST /admin/accounts/A/users  {"user_id": "alice", "role": "user"}
   (用 admin_a_key，公司管理员创建员工)
   → 拿到 alice_key，存入数据库

4. 管理员导入公司文档
   POST /resources  (用 admin_a_key)
   → 文档进入 resources/，公司全员可搜

5. alice 日常使用
   POST /search/search  (用 alice_key, query="报销流程")
   → 搜到公司文档 + alice 的私有记忆，搜不到 bob 的

   POST /sessions + /messages + /commit  (用 alice_key)
   → 对话记忆存入 alice 私有空间

6. alice 离开公司 A
   DELETE /admin/accounts/A/users/alice
   → alice_key 立即失效，私有数据清理

7. 公司 A 注销
   DELETE /admin/accounts/A
   → 级联清理所有数据（文件 + 向量 + 全部 Key）
```

---

## 九、注意事项

1. **隔离是自动的**：业务 API 不需要传 company_id 或 user_id，Key 里已经包含身份信息
2. **开发模式**：不配 `root_api_key` 时跳过认证，方便本地开发调试
3. **幂等性**：重复创建同名 Account/User 返回 409 `AlreadyExistsError`，上层需处理
4. **ROOT 调试**：ROOT Key 可以通过 `X-OpenViking-Account` + `X-OpenViking-User` Header 模拟任意身份，便于排查问题

---

## 附：Admin API 速查表

```
# 公司管理（ROOT only）
POST   /api/v1/admin/accounts                              创建公司
GET    /api/v1/admin/accounts                              列出所有公司
DELETE /api/v1/admin/accounts/{company_id}                  删除公司（级联）

# 用户管理（ROOT 或本公司 ADMIN）
POST   /api/v1/admin/accounts/{company_id}/users            注册用户
GET    /api/v1/admin/accounts/{company_id}/users            列出用户
DELETE /api/v1/admin/accounts/{company_id}/users/{uid}      删除用户
PUT    /api/v1/admin/accounts/{company_id}/users/{uid}/role 变更角色 (ROOT only)
POST   /api/v1/admin/accounts/{company_id}/users/{uid}/key  重新生成 Key
```

## 附：业务 API 速查表

```
# 知识库
POST   /api/v1/resources/temp_upload    上传文件（multipart）
POST   /api/v1/resources                添加资源（触发解析索引）
POST   /api/v1/skills                   添加技能

# 搜索
POST   /api/v1/search/find              语义搜索（无上下文）
POST   /api/v1/search/search            语义搜索（带会话上下文）
POST   /api/v1/search/grep              正则搜索
POST   /api/v1/search/glob              文件名匹配

# 会话
POST   /api/v1/sessions                 创建会话
GET    /api/v1/sessions                 列出会话
GET    /api/v1/sessions/{id}            获取会话详情
DELETE /api/v1/sessions/{id}            删除会话
POST   /api/v1/sessions/{id}/messages   添加消息
POST   /api/v1/sessions/{id}/commit     提交（归档 + 提取记忆）

# 文件系统
GET    /api/v1/fs/ls                    列目录
GET    /api/v1/fs/tree                  目录树
GET    /api/v1/fs/stat                  文件元信息
POST   /api/v1/fs/mkdir                 创建目录
DELETE /api/v1/fs                       删除
POST   /api/v1/fs/mv                    移动/重命名

# 内容
GET    /api/v1/content/read             读取内容
GET    /api/v1/content/abstract         读取摘要
GET    /api/v1/content/overview         读取概览
GET    /api/v1/content/download         下载原始文件

# 导入导出
POST   /api/v1/pack/export              导出 .ovpack
POST   /api/v1/pack/import              导入 .ovpack

# 系统
GET    /health                          健康检查（无需认证）
GET    /ready                           就绪探针
GET    /api/v1/system/status            系统状态
```
