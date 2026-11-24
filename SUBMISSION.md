# SUBMISSION

## 交付信息

- Fork 仓库：<https://github.com/yllls/AI-Engineer-Quiz>
- 开发分支：`feat/filter-sort-export`
- 基线：上游 `main` 的 `3aa137c`
- 最终 commit SHA：以该分支最终 HEAD 为准，交付消息中同时给出完整 SHA
- 实现与测试提交按模块拆分，完整历史可通过 `git log --oneline` 查看，过程中未使用 squash、amend 或 rebase

## 完成内容

### 组合筛选

`GET /api/assets` 和 `GET /api/exports/assets.csv` 共享同一套规范化筛选条件：

- 关键词沿用原有 `asset_fts` 字段、porter unicode61 分词和前缀查询方式
- 关键词、媒体类型、集合、包含标签、排除标签取交集
- 包含标签必须全部命中，排除标签命中任意一个即排除
- 标签按完整名称匹配并沿用现有小写化处理，重复标签去重
- 同一标签同时包含和排除时返回空结果
- 只返回 `active` 素材
- 筛选后仍按用户选择的 `newest`、`oldest`、`name` 或 `largest` 排序
- 关键词为空或清洗后没有有效词元时都有明确行为，特殊字符不会触发 FTS 语法错误

请求示例：

```http
GET /api/assets?q=harbor&collection_id=1&tags=approved&tags=campaign&exclude_tags=internal&sort=oldest&limit=10
```

媒体类型保持单选。旧版单个 `tag` 参数仍作为包含标签兼容别名。

### 排序与分页

- 四种排序都使用确定性的二级键 `id`
- 相同 `created_at` 跨页不会重复或遗漏
- 使用 HMAC 签名的双向键集游标
- 游标绑定规范化筛选、排序、页大小和方向，篡改或条件变化返回 `400 invalid_cursor`
- 响应保留 `total`、`items`、`facets`、`mode`，新增嵌套 `pagination`
- 保留旧 `limit` 和 `offset` 模式，游标与 offset 互斥
- 页面支持上一页、下一页、当前页和匹配总数，不提供任意页码跳转
- 服务器默认每页 60 条、上限 200 条；新界面默认 10 条，可选 10、20、50 条并保存在浏览器
- 桌面端使用五列网格，默认 10 条正好两整行；窄屏按响应式断点减少列数

分页响应示例：

```json
{
  "total": 128,
  "items": [],
  "pagination": {
    "page": 2,
    "limit": 10,
    "prev_cursor": "...",
    "next_cursor": "..."
  }
}
```

### CSV 导出

```http
GET /api/exports/assets.csv?q=harbor&tags=approved&columns=alt_text&columns=size&sanitize_formulas=true
Authorization: Bearer ...
```

- 导出全部匹配结果，不受当前页限制
- 与列表使用同一筛选、排序和计数路径
- 在单个只读事务快照中完成预检计数和逐行流式输出
- 默认上限 100000 行，可用 `KEEPSTACK_EXPORT_MAX_ROWS` 调整；超限在响应头发出前返回 `413 export_too_large`
- 必选列：`uuid`、`title`、`description`、`filename`、`media_type`、`tags`、`created_at`
- 可选白名单：`alt_text`、`mime_type`、`size`、`width`、`height`、`updated_at`、`dc_creator`、`dc_subject`、`dc_publisher`、`dc_date`、`license`、`rights_statement`、`credit`、`usage_expiry`、`retention_until`
- 未知列返回 422
- UTF-8 BOM 和 CRLF 换行，中文、逗号、双引号和多行字段由标准 CSV 规则处理
- `tags` 输出按名称排序的 JSON 数组，例如 `["approved","campaign"]`
- 时间字段保持 API 使用的 ISO 8601 UTC 原值
- 零结果仍输出表头
- 默认启用公式注入防护，对以 `=`、`+`、`-`、`@`、制表符或回车开头的字符串加前导单引号；可通过 `sanitize_formulas=false` 关闭
- 成功发起导出时写入 `asset.export` 审计日志，记录筛选摘要、排序列、匹配行数、列配置和 IP
- 页面使用携带 Bearer Token 的 fetch 读取 Blob，能区分 401、413、参数错误和网络失败并提示

### 页面与修改一致性

- 顶部组合筛选栏：媒体类型、集合、可搜索标签、包含/排除标签
- 已生效条件以可删除 chip 展示，支持单项修改、单项删除和清除全部
- 清除全部只清筛选并回到第一页，保留排序和每页条数
- 筛选和排序使用 `replaceState` 同步到 URL，刷新和分享可恢复，分页游标不写入 URL
- 标签候选由服务端搜索并支持 Load more
- 素材详情中的新增标签提供显式 Add 按钮，回车仍可提交同一表单
- 使用 AbortController 和请求序号，快速切换条件时旧响应不会覆盖新结果
- 加载失败时清空旧结果，显示错误和重试；空结果有独立提示
- 失效游标提示后自动回到第一页
- 素材标题、描述或标签修改后刷新当前页，下一次查询和导出读取数据库最新值
- 修复了素材原文件下载链接未携带 Bearer Token 的既有问题
- 修复了 JSON 请求未显式指定方法时使用 GET 导致的部分既有操作失败
- 语义搜索入口按题意隐藏，后端旧 API 模式保留；与新筛选、游标组合时返回明确 400

## 数据库与关键取舍

- 引入 `schema_meta.schema_version` 和按顺序执行的幂等迁移
- 迁移 v1 增加排序和标签反向索引
- 迁移 v2 增加 `(status, media_type)` 索引、`tag_usage` 计数表及维护触发器
- FTS 查询改为 `asset.id IN (SELECT rowid FROM asset_fts ...)`，避免数据库中逐行探测虚拟表
- 全局标签 facet 从 `tag_usage` 读取，不再在每次列表请求中聚合完整 `asset_tags`
- `tag_usage` 触发器覆盖标签关联增删、素材状态切换和硬删除
- 当前迁移版本为 2；比运行程序更新的数据库会被拒绝启动

## 运行与验证

需要 Python 3.10+。以下命令均在仓库根目录执行。

### 安装

```bash
python -m pip install -e ".[dev,e2e]"
python -m playwright install chromium    # 仅运行浏览器测试需要
```

也可以按 README 使用 uv：

```bash
uv venv --python 3.12
uv pip install -e ".[dev,e2e]"
```

### 启动

```bash
export KEEPSTACK_AI_ENABLED=false
export KEEPSTACK_OCR_ENABLED=false
python -m keepstack seed
python -m keepstack
```

打开 <http://localhost:8000>，账号 `admin` / `admin`。`seed` 在干净数据目录中生成 24 个素材和 3 个集合。

### 自动化测试

```bash
# 后端单元、集成和迁移测试；浏览器测试默认跳过
python -m pytest -q

# 浏览器测试
KEEPSTACK_E2E=1 python -m pytest -q tests/e2e
```

本次实测：

- 后端与集成：45 passed，13 skipped（2 个可选的 OCR/CLIP 测试，11 个未启用浏览器的 E2E）
- Playwright Chromium：11 passed
- 覆盖组合筛选、标签包含/排除、素材标签确认按钮、相同创建时间跨页、上一页、URL 恢复、竞态、导出列与下载格式、导出超限提示、列表失败重试、审计和素材原文件鉴权下载

### 测试数据

- 后端测试使用 `tests/conftest.py` 创建的临时数据目录，测试之间共享目录但使用唯一前缀
- 浏览器测试和后续后端测试可复用 `tests/fixtures_data.py` 的确定性数据构建器
- E2E 启动独立临时目录、独立 uvicorn 进程和确定性 33 个素材目录

### 性能基准

```bash
python scripts/benchmark.py --rows 100000 --iterations 20
```

本次实测环境：Windows 11，Python 3.10.14，SQLite 3.45.3，单机临时目录。列表场景使用界面默认每页 10 条。

| 场景 | 结果 |
|---|---|
| 10 万素材，关键词搜索首屏 P50 / P95 | 211.54ms / 220.07ms |
| 10 万素材，组合筛选首屏 P50 / P95 | 411.40ms / 454.32ms |
| 10 万行全量 CSV 导出 | 2.81s，约 16.3MB，约 35,600 行/秒 |

该基准不放入常规 CI。它使用直接查询与导出路径测量后端成本，不包含浏览器渲染、网络传输或缩略图下载。

## 主要限制与未完成项

- 不支持任意页码跳转；游标只支持相邻页导航
- 刷新页面会从第一页开始，分页游标不进入 URL
- 语义搜索没有接入新筛选、游标和导出，界面已隐藏
- 新界面、游标、组合标签和导出只支持 active 素材；旧 offset 客户端仍可查询其它状态
- 标签 facet 表示全库 active 统计，不是当前结果集的统计；页面不展示这些计数
- 导出上限为单次 100000 行，未实现异步任务、进度轮询、断点续传、限流和每用户配额
- 导出持有 SQLite 读事务，长时间导出会延迟 WAL checkpoint
- 浏览器通过 Blob 接收 CSV，文件大小受浏览器内存和导出上限约束
- 公式防护会改变危险开头的原值；关闭后输出原值，风险由调用方承担
- 游标没有 TTL；密钥轮换或服务端密钥变更会使已有游标失效并触发前端回第一页
- 未实现多集合、多媒体类型同时筛选，未实现跨请求快照
- E2E 只覆盖 Chromium；没有 Firefox/WebKit 矩阵，也没有完整的 WCAG 审计
- 没有异步导出队列、共享缓存、分布式会话或 Postgres 迁移

## 提交历史

实现按数据库、搜索、导出、前端、E2E、性能基准和文档拆分。最终 SHA 以 `feat/filter-sort-export` 分支 HEAD 为准并在交付消息中给出。
