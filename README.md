# 数据洞察与自助分析平台

面向业务分析师的自助式数据洞察平台。覆盖数据集市构建、自助SQL查询、可视化拖拽分析、分析看板协作、指标异动归因、分析模型沉淀、查询优化建议与数据故事编排的完整洞察链路。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据集市构建 | `mart` | 集市定义 | 集市编号、集市名称、数据源表 |
| 自助SQL查询 | `sqlpad` | 查询脚本 | 脚本编号、脚本名称、SQL语句 |
| 拖拽可视化 | `vizdrag` | 可视化方案 | 方案编号、方案名称、数据源绑定 |
| 看板协作管理 | `collab` | 协作看板 | 看板编号、看板名称、协作成员 |
| 指标异动归因 | `anomaly` | 异动记录 | 记录编号、指标名称、异动方向 |
| 分析模型沉淀 | `model` | 模型注册 | 模型编号、模型名称、模型类型 |
| 查询优化建议 | `opt` | 优化建议 | 建议编号、目标查询、优化类型 |
| 数据故事编排 | `story` | 故事模板 | 模板编号、故事标题、分析主题 |
| 数据准备流水线 | `etl` | 准备任务 | 任务编号、任务名称、输入源 |
| 数据目录检索 | `catalog` | 目录条目 | 条目编号、数据集名称、业务域 |
| 数据采样管理 | `sample` | 采样配置 | 配置编号、数据源名称、采样算法 |
| 同环比分析 | `compare` | 对比方案 | 方案编号、对比指标、基准周期 |
| 分析导出中心 | `export` | 导出任务 | 任务编号、数据源名称、导出格式 |
| 分析告警规则 | `alarm` | 告警规则 | 规则编号、监控指标、告警条件 |
| 数据定时刷新 | `refresh` | 刷新策略 | 策略编号、目标数据集、刷新周期 |
| 查询审计日志 | `audit` | 审计记录 | 记录编号、查询语句、执行人 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 分析告警触发窗口

同一监控指标连续异动时，触发窗口按「监控指标 + 告警条件」归并，静默窗口内只落
一条记录，并保留最近一次触发依据。判定标准统一收在
`backend/app/services/alarm_rules.py`：

- 增长阈值统一折算成百分数，允许 `(0%, 100%]`；
- 静默周期统一折算成秒，允许 `[1, 3600]` 秒（上限 1 小时）；
- 生效范围支持 `全天` / `工作日 09:00-18:00` 等写法，边界越界即拒绝；
- 触发口径：实际增长 `>=` 增长阈值即触发，且事件时间必须落在生效范围内。

进入生效状态（草稿 → 已启用）前会统一校验监控条件、静默阈值与生效范围，任一越界
都拒绝并返回原因。触发记录号形如 `规则编号-T{序号}`，序号是触发依据事件在该指标
事件序列中的位置，因此阈值调整后记录号保持稳定；`T0` 表示尚无触发记录（例如规则
仍处于暂停状态）。

阈值变化通过版本快照处理：`POST /api/alarm/{id}/versions` 登记新版本后，旧版本
按原条件保留可读，新触发窗口按新条件重算，被替换的记录进入失效历史。

| 接口 | 说明 |
| --- | --- |
| `GET /api/alarm/trigger-records` | 触发记录清单（附 `核对编号`，静默窗口内只一条） |
| `GET /api/alarm/trigger-records/export` | 导出触发记录 |
| `GET /api/alarm/trigger-windows` | 按规则/指标读取触发窗口 |
| `POST /api/alarm/trigger-windows/recalculate` | 重算触发窗口（别名 `POST /api/alarm/recalculate`） |
| `GET /api/alarm/{id}/trigger-window` | 单条规则当前版本的触发窗口 |
| `POST /api/alarm/{id}/trigger` | 重算单条规则的触发窗口 |
| `GET /api/alarm/{id}/versions` | 单条规则版本快照（旧条件可读） |
| `POST /api/alarm/{id}/versions` | 登记新版本（阈值变化触发重算） |
| `GET /api/alarm?rule_version=N` | 按历史版本读取旧告警规则清单 |
| `GET` / `POST /api/alarm/events` | 监控事件清单 / 登记（重复事件只生效一次） |

核对样例：`ALAR-0002` 增长阈值 15%、静默 1800 秒，静默截止后只生成一条
`ALAR-0002-T2`；`ALAR-0003` 保持暂停，核对编号 `ALAR-0003-T0`。指标异动归因页
的触发结果按当前规则版本实时判定，不再复用旧条件的结论。

领域：数据工程
子方向：数仓与数据湖
