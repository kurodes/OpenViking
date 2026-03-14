# OpenViking 多租户支持分析报告

> 分析日期: 2026-03-14
> 基准: main 分支代码 vs `docs/design/multi-tenant-design.md`

## 总结

OpenViking 的 main 分支**已具备生产级多租户能力**。Phase 1 完整实现，Phase 2 大部分完成。核心隔离机制（账户/用户/Agent 三维隔离 + RBAC）从认证层到存储层完整贯通。

---

## 已实现的多租户功能

### 1. API Key 两级密钥体系

**文件:** `openviking/server/api_keys.py`

- **Root Key**: 存储在 `ov.conf`，授予 ROOT 角色
- **User Key**: 按账户存储在 `/{account_id}/_system/users.json`
- 全局账户索引: `/_system/accounts.json`
- `APIKeyManager` 提供完整密钥生命周期管理（创建/删除/轮换）

### 2. RBAC 三角色权限模型

**文件:** `openviking/server/identity.py`, `openviking/server/auth.py`

| 角色 | 权限范围 |
|------|---------|
| ROOT | 系统管理员，无账户限制 |
| ADMIN | 账户管理员，管理本账户内用户 |
| USER | 普通用户，仅访问自己的空间 |

- `require_role()` 装饰器在路由层强制执行角色检查

### 3. 认证中间件

**文件:** `openviking/server/auth.py`

- 从 `X-API-Key` / `Authorization` 头提取身份
- 开发模式：无密钥时返回 ROOT + 默认身份
- `get_request_context()` 依赖注入，贯穿整个请求链路

### 4. 账户管理 Admin API

**文件:** `openviking/server/routers/admin.py`

| 端点 | 功能 |
|------|------|
| `POST /api/v1/admin/accounts` | 创建账户 + 首个管理员 |
| `GET /api/v1/admin/accounts` | 列出账户 |
| `DELETE /api/v1/admin/accounts/{account_id}` | 删除账户（级联清理） |
| `POST .../accounts/{id}/users` | 注册用户 |
| `GET .../accounts/{id}/users` | 列出用户 |
| `DELETE .../accounts/{id}/users/{uid}` | 删除用户 |
| `PUT .../accounts/{id}/users/{uid}/role` | 变更角色 |
| `POST .../accounts/{id}/users/{uid}/key` | 重新生成密钥 |

### 5. 三维数据隔离

| 维度 | AGFS 路径隔离 | VectorDB 过滤 |
|------|-------------|--------------|
| 账户级 | `/{account_id}/...` 前缀 | `account_id` 字段过滤 |
| 用户级 | `/{account_id}/user/{user_space}/...` | `owner_space` 字段过滤 |
| Agent级 | `/{account_id}/agent/{md5(uid+aid)[:12]}/...` | 同 owner_space |

**关键实现文件:**
- `viking_fs.py` — `_uri_to_path()` 注入 account_id; `_is_accessible()` 做空间归属检查
- `viking_vector_index_backend.py` — `_tenant_filter()` 按角色构建查询过滤条件
- `collection_schemas.py` — VectorDB schema 包含 `account_id` 和 `owner_space` 索引字段

### 6. 全栈 Context 透传

- **路由层**: 所有 router 通过 `Depends(get_request_context)` 注入上下文
- **存储层**: VikingFS 所有方法接受 `ctx` 参数
- **检索层**: `hierarchical_retriever.py` 以 `RequestContext` 为必要参数
- **向量层**: tenant-aware 的 `search_global_roots_in_tenant()` 和 `search_children_in_tenant()`

### 7. 账户/用户目录初始化

**文件:** `openviking/service/core.py`, `openviking/core/directories.py`

- 创建账户时自动初始化共享目录
- 注册用户时自动初始化用户专属目录

---

## 代码有但设计文档未提及的功能

1. **`contextvars.ContextVar` 线程本地绑定** (`viking_fs.py`) — 允许隐式上下文访问
2. **`delete_account_data()`** (`viking_vector_index_backend.py`) — VectorDB 批量清理
3. **确定性向量 ID** (`collection_schemas.py`) — 使用 account_id + seed_uri 哈希

---

## 尚未实现的部分

| 缺失项 | 设计文档引用 | 说明 |
|--------|------------|------|
| 数据迁移脚本 `python -m openviking migrate` | T15 | 未找到实现 |
| ACL 扩展框架（资源共享） | 五.7 | 基础已铺设，ACL 表未实现 |
| 部分 Service 层 ctx 透传 | Phase 2 | 需逐一验证完整性 |
