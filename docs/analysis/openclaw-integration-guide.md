# OpenClaw 集成 OpenViking 指南

> 面向 OpenClaw 开发者，说明如何在多租户场景下正确调用 OpenViking。

---

## 一、整体架构：OpenClaw 与 OpenViking 的关系

```
┌──────────────────────────────────────────────────────────────┐
│                        OpenClaw                              │
│                                                              │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                      │
│  │ 租户 A   │  │ 租户 B   │  │ 租户 C   │  ← OpenClaw 的租户  │
│  └────┬────┘  └────┬────┘  └────┬────┘                      │
│       │            │            │                            │
│       └────────────┼────────────┘                            │
│                    │                                         │
│            ┌───────▼────────┐                                │
│            │ OpenViking     │                                │
│            │ 集成层          │  ← 你需要写的代码               │
│            │ (持有 Root Key) │                                │
│            └───────┬────────┘                                │
└────────────────────┼─────────────────────────────────────────┘
                     │  HTTP
          ┌──────────▼──────────┐
          │   OpenViking Server │
          │   (独立进程)         │
          └─────────────────────┘
```

**核心思路**：OpenClaw 以 ROOT 身份管理 OpenViking，为自己的每个租户在 OpenViking 中创建对应的 Account，然后用该租户的 User Key 调用业务 API。

---

## 二、部署：启动 OpenViking Server

### 2.1 配置文件 `ov.conf`

最关键的一项：**设置 `root_api_key`**，这是 OpenClaw 管理 OpenViking 的凭证。

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 1933,
    "root_api_key": "一个足够长的随机字符串"
  },
  "storage": {
    "vectordb": {
      "backend": "local",
      "path": "./data"
    },
    "agfs": {
      "port": 1833,
      "path": "./data",
      "backend": "local"
    }
  },
  "embedding": { "..." : "..." },
  "vlm": { "..." : "..." }
}
```

### 2.2 启动

```bash
openviking-server --config ov.conf
```

验证：

```bash
curl http://localhost:1933/health
# {"status": "ok", "healthy": true}
```

---

## 三、集成步骤（按时间线）

### 步骤 1：OpenClaw 租户开通时 → 创建 OpenViking Account

当 OpenClaw 中有新租户注册时，用 Root Key 调用 Admin API：

```python
import httpx

OV_URL = "http://openviking:1933"
ROOT_KEY = "你的root_api_key"

def on_openclaw_tenant_created(tenant_id: str, admin_username: str):
    """OpenClaw 租户开通时调用"""
    resp = httpx.post(
        f"{OV_URL}/api/v1/admin/accounts",
        headers={"X-API-Key": ROOT_KEY},
        json={
            "account_id": tenant_id,           # 建议直接用 OpenClaw 的租户 ID
            "admin_user_id": admin_username,    # 该租户的第一个管理员
        },
    )
    result = resp.json()["result"]
    admin_key = result["user_key"]  # ← 保存这个 key，后续该租户的管理操作要用
    return admin_key
```

### 步骤 2：租户内新增用户时 → 注册 OpenViking User

```python
def on_openclaw_user_created(tenant_id: str, user_id: str, role: str = "user"):
    """OpenClaw 租户内新增用户时调用"""
    resp = httpx.post(
        f"{OV_URL}/api/v1/admin/accounts/{tenant_id}/users",
        headers={"X-API-Key": ROOT_KEY},   # ROOT 可以管所有账户
        json={"user_id": user_id, "role": role},
    )
    user_key = resp.json()["result"]["user_key"]  # ← 保存，该用户后续调用要用
    return user_key
```

### 步骤 3：业务调用 → 带上 User Key

拿到 User Key 后，所有业务 API 调用只需要在 Header 里带上它：

```python
user_key = "之前保存的用户key"
headers = {
    "X-API-Key": user_key,
    "X-OpenViking-Agent": "chatbot-v1",  # 可选：区分同一用户的不同 Agent
}
```

OpenViking 会自动从 Key 解析出 `account_id + user_id`，不需要在请求里重复传。

### 步骤 4：租户/用户删除时 → 清理

```python
def on_openclaw_user_removed(tenant_id: str, user_id: str):
    httpx.delete(
        f"{OV_URL}/api/v1/admin/accounts/{tenant_id}/users/{user_id}",
        headers={"X-API-Key": ROOT_KEY},
    )

def on_openclaw_tenant_removed(tenant_id: str):
    # 级联删除：自动清理该账户下所有用户、文件、向量数据
    httpx.delete(
        f"{OV_URL}/api/v1/admin/accounts/{tenant_id}",
        headers={"X-API-Key": ROOT_KEY},
    )
```

---

## 四、业务 API 使用指南

拿到 User Key 后，以下是 OpenClaw 最常用的 OpenViking 接口：

### 4.1 添加知识（Resources）

把租户的文档、文件导入 OpenViking：

```python
# 通过 URL 或服务端本地路径添加
resp = httpx.post(
    f"{OV_URL}/api/v1/resources",
    headers=headers,
    json={
        "path": "/data/uploads/report.pdf",  # 服务端可访问的路径
        "reason": "公司季度报告",
        "wait": True,                         # 等待处理完成再返回
    },
)

# 通过先上传临时文件再添加（适合远程客户端）
with open("report.pdf", "rb") as f:
    upload_resp = httpx.post(
        f"{OV_URL}/api/v1/resources/temp_upload",
        headers=headers,
        files={"file": f},
    )
    temp_path = upload_resp.json()["result"]["temp_path"]

resp = httpx.post(
    f"{OV_URL}/api/v1/resources",
    headers=headers,
    json={"temp_path": temp_path, "wait": True},
)
```

### 4.2 语义搜索（Search）

这是最核心的能力——用自然语言搜索知识库：

```python
# 无上下文搜索
resp = httpx.post(
    f"{OV_URL}/api/v1/search/find",
    headers=headers,
    json={
        "query": "Q3 营收增长了多少？",
        "limit": 5,
    },
)
results = resp.json()["result"]

# 带会话上下文搜索（推荐，效果更好）
resp = httpx.post(
    f"{OV_URL}/api/v1/search/search",
    headers=headers,
    json={
        "query": "Q3 营收增长了多少？",
        "session_id": "conv-123",   # 关联到一个会话
        "limit": 5,
    },
)
```

### 4.3 会话管理（Sessions）

用于追踪对话历史，让搜索更精准，并自动提取记忆：

```python
# 创建会话
resp = httpx.post(f"{OV_URL}/api/v1/sessions", headers=headers)
session_id = resp.json()["result"]["session_id"]

# 添加消息
httpx.post(
    f"{OV_URL}/api/v1/sessions/{session_id}/messages",
    headers=headers,
    json={"role": "user", "content": "帮我总结一下 Q3 报告"},
)
httpx.post(
    f"{OV_URL}/api/v1/sessions/{session_id}/messages",
    headers=headers,
    json={"role": "assistant", "content": "根据报告，Q3 营收同比增长 15%..."},
)

# 对话结束后，提交会话 → 自动归档 + 提取长期记忆
httpx.post(
    f"{OV_URL}/api/v1/sessions/{session_id}/commit",
    headers=headers,
)
```

### 4.4 文件浏览与内容读取

```python
# 列目录
resp = httpx.get(
    f"{OV_URL}/api/v1/fs/ls",
    headers=headers,
    params={"uri": "viking://resources/"},
)

# 读取摘要（L0 abstract，几句话）
resp = httpx.get(
    f"{OV_URL}/api/v1/content/abstract",
    headers=headers,
    params={"uri": "viking://resources/report.pdf"},
)

# 读取概览（L1 overview，详细结构化总结）
resp = httpx.get(
    f"{OV_URL}/api/v1/content/overview",
    headers=headers,
    params={"uri": "viking://resources/report.pdf"},
)

# 读取原始内容
resp = httpx.get(
    f"{OV_URL}/api/v1/content/read",
    headers=headers,
    params={"uri": "viking://resources/report.pdf"},
)
```

### 4.5 导入导出（Pack）

迁移或备份租户数据：

```python
# 导出
resp = httpx.post(
    f"{OV_URL}/api/v1/pack/export",
    headers=headers,
    json={"uri": "viking://resources/", "to": "/tmp/backup.ovpack"},
)

# 导入
resp = httpx.post(
    f"{OV_URL}/api/v1/pack/import",
    headers=headers,
    json={"file_path": "/tmp/backup.ovpack", "parent": "viking://resources/"},
)
```

---

## 五、用 Python SDK（推荐）

除了直接 HTTP 调用，OpenViking 提供了 Python SDK，更简洁：

```python
import openviking as ov

async def demo():
    # 初始化客户端（带用户 Key）
    client = ov.AsyncHTTPClient(
        url="http://openviking:1933",
        api_key="用户的key",
        agent_id="chatbot-v1",
    )
    await client.initialize()

    # --- 添加资源 ---
    await client.add_resource("/data/docs/report.pdf", reason="季度报告", wait=True)

    # --- 搜索 ---
    results = await client.search(query="Q3 营收", limit=5)

    # --- 会话 ---
    session = client.session()
    await session.add_message(role="user", content="总结 Q3 报告")
    await session.add_message(role="assistant", content="Q3 营收增长 15%...")
    results = await client.search(query="对比 Q2", session=session, limit=5)
    await session.commit()  # 归档 + 提取记忆

    # --- 文件操作 ---
    entries = await client.ls("viking://resources/")
    content = await client.read("viking://resources/report.pdf")
    abstract = await client.abstract("viking://resources/report.pdf")

    # --- 管理操作（需要 ROOT Key 的客户端）---
    admin = ov.AsyncHTTPClient(url="http://openviking:1933", api_key=ROOT_KEY)
    await admin.initialize()
    await admin.admin_create_account("new-tenant", "admin-user")
    key = await admin.admin_register_user("new-tenant", "bob", "user")

    await client.close()
    await admin.close()
```

同步版本用 `ov.SyncHTTPClient`，接口完全一致。

---

## 六、OpenClaw 集成架构建议

### 6.1 Key 的管理策略

```
┌──────────────────────────────────────────┐
│             OpenClaw 后端                 │
│                                          │
│  ┌──────────────────────────────────┐    │
│  │ OpenViking 集成模块               │    │
│  │                                  │    │
│  │  ROOT_KEY (环境变量/密钥管理)      │    │  ← 只有这个模块持有 Root Key
│  │                                  │    │
│  │  tenant_keys_cache: {            │    │  ← 缓存租户 Key 的映射
│  │    "tenant_A": "key_xxx",        │    │
│  │    "tenant_B": "key_yyy",        │    │
│  │  }                               │    │
│  └──────────────────────────────────┘    │
└──────────────────────────────────────────┘
```

建议：
- **Root Key** 只在 OpenClaw 后端的集成模块中持有，通过环境变量注入，不要硬编码
- **User Key** 可以存在 OpenClaw 的数据库中，按 `(tenant_id, user_id) → ov_user_key` 映射
- 如果 OpenClaw 只做后端代理（用户不直接调 OpenViking），可以简化为每个租户一个 ADMIN Key

### 6.2 租户映射模型

两种方式，按场景选择：

**方案 A：1:1 映射（推荐）**

```
OpenClaw Tenant  ←→  OpenViking Account
OpenClaw User    ←→  OpenViking User

每个 OpenClaw 用户有自己的 OpenViking User Key
```

适用于：用户需要独立的记忆空间、Agent 空间，数据隔离到用户级别。

**方案 B：租户级代理**

```
OpenClaw Tenant  ←→  OpenViking Account
OpenClaw 后端    ←→  OpenViking 中的一个 "service" 用户（ADMIN 角色）

OpenClaw 后端用一个 ADMIN Key 代理该租户所有用户的请求
```

适用于：OpenClaw 自己管理用户权限，OpenViking 只做租户级隔离。
注意：此方案下同一租户内的用户数据不隔离（都用同一个 OpenViking User）。

### 6.3 Agent ID 的使用

如果 OpenClaw 的一个用户有多个独立的 AI 助手（如"客服 Bot"和"分析 Bot"），用 `X-OpenViking-Agent` 区分：

```python
# 客服场景
headers_cs = {"X-API-Key": user_key, "X-OpenViking-Agent": "customer-service"}

# 分析场景
headers_analytics = {"X-API-Key": user_key, "X-OpenViking-Agent": "analytics"}
```

两个 Agent 各自拥有**独立的记忆空间**（agent_space = `md5(user_id + agent_id)[:12]`），但共享该用户的 user 空间和账户级 resources。

---

## 七、完整生命周期示意

```
时间线 ──────────────────────────────────────────────────────────>

1. 部署
   OpenClaw 启动 OpenViking Server（配好 root_api_key）

2. 租户开通
   OpenClaw 租户 "acme" 注册
     → POST /admin/accounts  {"account_id":"acme", "admin_user_id":"admin"}
     → 保存返回的 admin_key

3. 用户注册
   acme 租户下 alice 注册
     → POST /admin/accounts/acme/users  {"user_id":"alice", "role":"user"}
     → 保存返回的 alice_key

4. 日常使用
   alice 使用 OpenClaw 的 AI 功能
     → OpenClaw 后端用 alice_key 调 OpenViking:
       - 添加文档:    POST /resources
       - 搜索知识:    POST /search/find
       - 管理会话:    POST /sessions, POST /sessions/{id}/messages
       - 提取记忆:    POST /sessions/{id}/commit

5. 用户离开
   alice 从 acme 离开
     → DELETE /admin/accounts/acme/users/alice
     → alice_key 立即失效

6. 租户注销
   acme 整体注销
     → DELETE /admin/accounts/acme
     → 级联删除所有文件 + 向量数据 + 密钥
```

---

## 八、注意事项

1. **数据自动隔离**：业务 API（search、sessions、fs 等）的数据隔离是**自动的**，OpenClaw 不需要在请求参数里传 tenant_id，只要 Key 对了，数据就隔离了

2. **ROOT Key 的特殊能力**：ROOT 用户可以通过 `X-OpenViking-Account` 和 `X-OpenViking-User` Header 指定以任意账户/用户身份操作，这在调试和数据迁移时有用

3. **Key 轮换**：`POST /admin/accounts/{id}/users/{uid}/key` 可以重新生成密钥，旧密钥**立即失效**，需同步更新 OpenClaw 侧的缓存

4. **开发模式**：不配 `root_api_key` 时无需认证，所有请求自动以 ROOT/default 身份执行，方便本地开发

5. **幂等性**：重复创建同名 Account 或 User 会返回 `AlreadyExistsError`，OpenClaw 需处理这种情况（或先检查再创建）
