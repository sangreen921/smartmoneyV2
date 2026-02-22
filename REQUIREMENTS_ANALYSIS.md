# SmartMoneyV2 代码与需求分析

## 1. 产品定位（从前端行为反推）
- 产品是一个“币安交易员监控 + 订阅分层 + 支付升级 + Telegram通知”的 Web 应用。
- 主要页面由 4 个静态入口组成：
  - `index.html`：主监控看板
  - `detail.html`：单交易员详情
  - `login.html`：登录/注册/Google OAuth
  - `admin.html`：支付确认后台

## 2. 核心业务流（已实现）

### 2.1 游客模式与登录态
- 首页启动时会先检查 URL token（Google 回调）并写入本地，再调用 `/api/auth/me` 校验登录。
- 未登录或 token 失效时，会进入“样例数据模式”并请求 `/api/sample-data` 展示演示内容。

### 2.2 用户认证
- 登录页支持三种认证入口：Google OAuth、邮箱登录、邮箱注册。
- 登录/注册成功后，把 `token` 与 `user` 写到 `localStorage`，然后跳转首页。

### 2.3 交易员监控
- 首页拉取交易员列表、统计信息、持仓和交易流水；并且每 30 秒轮询刷新。
- 添加交易员要求登录；后端返回 quota 超限时前端会引导升级。
- 点击交易员会打开详情页，详情页会加载交易员、持仓、交易、转账及通知配置。

### 2.4 套餐与支付
- 首页会加载套餐与当前使用量。
- 升级到 `annual` 时前端直接打开支付弹窗；降级 free 需要确认。
- 支付订单通过 `/api/payment/create` 创建，展示订单号、钱包地址、金额等信息。
- 管理端可登录后查看待确认订单并执行“确认支付”，触发用户升级。

### 2.5 通知功能
- 详情页支持保存 Telegram Chat ID、启用/禁用余额/交易通知、发送测试消息、删除订阅。
- 若后端返回“未启用”，前端显示 Telegram 未启用状态。

## 3. 推导出的后端接口契约（按页面聚合）

### 3.1 鉴权
- `POST /api/auth/login`
- `POST /api/auth/register`
- `GET /api/auth/google/url`
- `GET /api/auth/me`

### 3.2 首页/监控
- `GET /api/traders`
- `POST /api/traders`
- `GET /api/stats`
- `GET /api/positions/:traderId`
- `GET /api/trades/:traderId?limit=...`
- `GET /api/sample-data`

### 3.3 套餐/支付
- `GET /api/pricing/plans`
- `GET /api/pricing/usage`
- `POST /api/pricing/upgrade`
- `POST /api/payment/create`

### 3.4 详情页扩展
- `GET /api/traders/:traderId`
- `GET /api/transfers/:traderId`
- `GET /api/notifications/:traderId`
- `POST /api/notifications/:traderId`
- `POST /api/notifications/:traderId/test`
- `DELETE /api/notifications/:traderId`
- `DELETE /api/traders/:traderId`

### 3.5 管理后台
- `GET /api/admin/payments/pending`
- `POST /api/admin/payment/confirm`

## 4. 当前代码暴露的“真实需求”与隐含约束
- **必须支持 token 鉴权与本地持久化**：所有受限接口均依赖 `Authorization: Bearer <token>`。
- **必须支持游客演示链路**：未登录场景并非错误，而是产品漏斗的一部分（样例数据 + 登录引导）。
- **必须支持配额模型**：免费版（2 个交易员）与年付版（100 个交易员）在前端逻辑中被硬编码依赖。
- **支付流程需要人工审核环节**：`admin.html` 明确存在“待确认 -> 已支付”的人工确认步骤。
- **通知功能依赖可选基础设施**：Telegram 能力可被后端禁用，前端需兼容降级。

## 5. 风险与改进建议（需求层）
- 建议补充统一 API 文档（错误码、字段、权限范围），当前前端对错误字符串有业务分支依赖，易碎。
- 建议把“free/annual、2/100”等策略值下沉到后端配置返回，避免前端硬编码。
- 建议把 admin 登录与普通登录接口区分角色或独立端点，减少误用风险。
- 建议将支付链路状态机显式化（pending/paid/expired/canceled）并统一前端展示。
- 建议补充 E2E 冒烟（登录、添加交易员、升级、通知保存）以验证跨页面联动。
