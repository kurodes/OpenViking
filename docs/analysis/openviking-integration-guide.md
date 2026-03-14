# OpenViking 多租户集成指南

## 概念映射

```
上层应用            OpenViking          说明
────────           ──────────          ────
公司 (Company)  →  ROOT                持有 root_api_key，管理所有部门
部门 (Department)→ Account             部门间完全隔离
员工 (Employee) →  User                部门内员工间记忆隔离
部门共享知识库   →  viking://resources/  Account 内所有员工可访问
员工私有记忆     →  viking://user/{uid}/ 仅本人可访问
员工 AI Agent   →  viking://agent/{hash}/ 仅本人+该 Agent 可访问
```

隔离规则（全部自动，上层应用只需传对 API Key）：
- **部门间**：路径前缀 `/{account_id}/` + 向量查询 `account_id` 过滤，完全不可见
- **员工间**：`user/`、`agent/`、`session/` 按归属检查 + `owner_space` 过滤
- **部门知识库**：`resources/` 对本部门全员开放

## 认证机制

请求 Header：
- `X-API-Key: <key>` — 必须，身份从 Key 自动解析（部门+员工）
- `X-OpenViking-Agent: <agent_id>` — 可选，多 Agent 场景区分记忆空间，默认 `"default"`
- `X-OpenViking-Account` + `X-OpenViking-User` — ROOT 调试用，模拟任意身份

角色：ROOT（公司后端）、ADMIN（部门管理员）、USER（普通员工）

Key 管理：
- Root Key 通过环境变量注入后端，仅用于创建/删除部门
- User Key 在创建员工时返回，存入上层应用数据库，业务调用时查出来放 Header
- Key 轮换：`POST /admin/.../key` 后旧 Key 立即失效

## Admin API

所有管理接口前缀：`/api/v1/admin`

### 部门管理（ROOT only）

```python
import httpx

OV_URL = "http://openviking-server:1933"
ROOT_HEADERS = {"X-API-Key": ROOT_KEY}

# 创建部门，返回部门管理员的 Key
resp = httpx.post(f"{OV_URL}/api/v1/admin/accounts", headers=ROOT_HEADERS,
    json={"account_id": dept_id, "admin_user_id": admin_username})
admin_key = resp.json()["result"]["user_key"]

# 删除部门（级联删除所有员工 Key + 文件 + 向量数据）
httpx.delete(f"{OV_URL}/api/v1/admin/accounts/{dept_id}", headers=ROOT_HEADERS)

# 列出所有部门
httpx.get(f"{OV_URL}/api/v1/admin/accounts", headers=ROOT_HEADERS)
```

### 员工管理（ROOT 或本部门 ADMIN）

```python
ADMIN_HEADERS = {"X-API-Key": admin_key}

# 注册员工，role: "user"(默认) 或 "admin"
resp = httpx.post(f"{OV_URL}/api/v1/admin/accounts/{dept_id}/users",
    headers=ADMIN_HEADERS, json={"user_id": uid, "role": "user"})
user_key = resp.json()["result"]["user_key"]

# 删除员工（Key 立即失效）
httpx.delete(f"{OV_URL}/api/v1/admin/accounts/{dept_id}/users/{uid}",
    headers=ADMIN_HEADERS)

# 列出部门内员工
httpx.get(f"{OV_URL}/api/v1/admin/accounts/{dept_id}/users", headers=ADMIN_HEADERS)

# 变更角色（ROOT only）
httpx.put(f"{OV_URL}/api/v1/admin/accounts/{dept_id}/users/{uid}/role",
    headers=ROOT_HEADERS, json={"role": "admin"})

# 重新生成 Key（ROOT 或 ADMIN）
httpx.post(f"{OV_URL}/api/v1/admin/accounts/{dept_id}/users/{uid}/key",
    headers=ADMIN_HEADERS)
```

## 业务 API

所有业务调用只需员工自己的 Key，OpenViking 自动隔离。

```python
HEADERS = {"X-API-Key": user_key}
# 多 Agent 场景加: HEADERS["X-OpenViking-Agent"] = "agent-name"
```

### 知识库

```python
# 上传文件（multipart）
resp = httpx.post(f"{OV_URL}/api/v1/resources/temp_upload",
    headers=HEADERS, files={"file": open(path, "rb")})
temp_path = resp.json()["result"]["temp_path"]

# 添加资源（触发解析索引），wait=True 同步等待完成
resp = httpx.post(f"{OV_URL}/api/v1/resources",
    headers=HEADERS, json={"temp_path": temp_path, "wait": True})

# 添加技能
httpx.post(f"{OV_URL}/api/v1/skills", headers=HEADERS,
    json={"data": skill_data, "wait": True})
```

### 搜索

```python
# 语义搜索（带会话上下文，更精准）
httpx.post(f"{OV_URL}/api/v1/search/search", headers=HEADERS,
    json={"query": "报销流程", "session_id": sid, "limit": 5})

# 语义搜索（无上下文）
httpx.post(f"{OV_URL}/api/v1/search/find", headers=HEADERS,
    json={"query": "报销流程", "limit": 5})

# 正则搜索
httpx.post(f"{OV_URL}/api/v1/search/grep", headers=HEADERS,
    json={"pattern": "报销.*流程", "uri": "viking://resources/"})

# 文件名匹配
httpx.post(f"{OV_URL}/api/v1/search/glob", headers=HEADERS,
    json={"pattern": "*.pdf", "uri": "viking://resources/"})
```

### 会话与记忆

```python
# 创建会话
resp = httpx.post(f"{OV_URL}/api/v1/sessions", headers=HEADERS)
session_id = resp.json()["result"]["session_id"]

# 添加消息（role: "user" 或 "assistant"）
httpx.post(f"{OV_URL}/api/v1/sessions/{session_id}/messages",
    headers=HEADERS, json={"role": "user", "content": "怎么报销？"})

# 提交会话（归档对话 + 提取长期记忆到员工私有空间）
httpx.post(f"{OV_URL}/api/v1/sessions/{session_id}/commit", headers=HEADERS)

# 列出会话 / 获取详情 / 删除
httpx.get(f"{OV_URL}/api/v1/sessions", headers=HEADERS)
httpx.get(f"{OV_URL}/api/v1/sessions/{session_id}", headers=HEADERS)
httpx.delete(f"{OV_URL}/api/v1/sessions/{session_id}", headers=HEADERS)
```

### 文件系统

```python
httpx.get(f"{OV_URL}/api/v1/fs/ls", headers=HEADERS, params={"uri": "viking://resources/"})
httpx.get(f"{OV_URL}/api/v1/fs/tree", headers=HEADERS, params={"uri": "viking://resources/"})
httpx.get(f"{OV_URL}/api/v1/fs/stat", headers=HEADERS, params={"uri": "viking://resources/doc.pdf"})
httpx.post(f"{OV_URL}/api/v1/fs/mkdir", headers=HEADERS, json={"uri": "viking://resources/subdir/"})
httpx.post(f"{OV_URL}/api/v1/fs/mv", headers=HEADERS, json={"from_uri": "viking://resources/a.pdf", "to_uri": "viking://resources/b.pdf"})
httpx.delete(f"{OV_URL}/api/v1/fs", headers=HEADERS, params={"uri": "viking://resources/old.pdf"})
```

### 内容读取

```python
httpx.get(f"{OV_URL}/api/v1/content/read", headers=HEADERS, params={"uri": uri})       # 原始内容
httpx.get(f"{OV_URL}/api/v1/content/abstract", headers=HEADERS, params={"uri": uri})   # 几句话摘要
httpx.get(f"{OV_URL}/api/v1/content/overview", headers=HEADERS, params={"uri": uri})   # 结构化概览
httpx.get(f"{OV_URL}/api/v1/content/download", headers=HEADERS, params={"uri": uri})   # 下载原始文件
```

### 导入导出

```python
httpx.post(f"{OV_URL}/api/v1/pack/export", headers=HEADERS, json={"uri": "viking://resources/", "to": "/tmp/backup.ovpack"})
httpx.post(f"{OV_URL}/api/v1/pack/import", headers=HEADERS, json={"file_path": "/tmp/backup.ovpack", "parent": "viking://resources/"})
```

### 系统

```python
httpx.get(f"{OV_URL}/health")                                          # 健康检查（无需认证）
httpx.get(f"{OV_URL}/ready")                                           # 就绪探针（无需认证）
httpx.get(f"{OV_URL}/api/v1/system/status", headers=HEADERS)           # 系统状态
httpx.post(f"{OV_URL}/api/v1/system/wait", headers=HEADERS)            # 等待处理完成
```

## Python SDK

```python
import openviking as ov

# 异步客户端
client = ov.AsyncHTTPClient(url="http://openviking-server:1933", api_key=user_key)
await client.initialize()

results = await client.search(query="报销流程", limit=5)           # 搜索
session = client.session()                                          # 创建会话
await session.add_message(role="user", content="怎么报销？")
await session.commit()                                              # 提交会话
entries = await client.ls("viking://resources/")                    # 列目录
abstract = await client.abstract("viking://resources/doc.pdf")      # 读摘要

await client.close()

# 同步客户端：ov.SyncHTTPClient，方法名一致
```

## 响应格式

所有接口统一返回：

```json
{"status": "ok", "result": {...}}
{"status": "error", "error": {"code": "NOT_FOUND", "message": "..."}}
```

常见错误码：`ALREADY_EXISTS`(409)、`NOT_FOUND`(404)、`PERMISSION_DENIED`(403)、`UNAUTHENTICATED`(401)

## 注意事项

- 业务 API 不需要传 dept_id 或 user_id，Key 里已包含身份信息
- 部门间完全隔离，没有跨部门共享机制
- 重复创建同名 Account/User 返回 409 `AlreadyExistsError`
- 不配 `root_api_key` 时为开发模式，跳过认证
