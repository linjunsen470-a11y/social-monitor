# 2026-11-26 接管版本

已修复 CLI 安装及缓存、封面指纹函数。新增 coverage 和 reposts 报告，使用方法见 README。25 个离线测试通过，已验证干净 wheel 安装、默认试运行、临时 PostgreSQL 重复写入和报告导出。outputs 下的覆盖及重发报告使用项目 Neon 只读样本和首轮记录生成。本次未调用付费 Actor。本地 Apify REST 与生产 Neon 的直连联调仍待配置实际凭据。

以下为接管前的交付记录。

# 基础骨架交付状态（2026-11-26）

## 已完成
- Python package / Typer CLI 基础结构。
- 直接 Apify REST API Bearer Token 客户端，支持 `/users/me` token 检查、Actor run、Dataset 读取和 `maxTotalChargeUsd` 单次费用上限。
- 抖音、微信视频号、小红书独立适配器及字段规范化初稿。
- Neon PostgreSQL schema、稳定账号/作品唯一键、历史别名、候选与证据、费用账本。
- 基于封面 pHash + 标题/正文的候选匹配；不下载原片、不做关键帧分析。
- 成功查询缓存及缓存结果回读；封面 pHash 持久化，避免重复取图和重复计算。
- dry-run 默认、显式 `--execute`、CSV/JSONL 导出、Skill 和 IDE Agent 移交文档。
- 本地单元测试：8 passed。

## 尚未完成 / 不得误报
- 当前 `.env` 中 `APIFY_TOKEN` 未配置；直接 REST API 的真实 token 验证尚未执行。此前通过已连接 Apify MCP 运行 Actor 的实测不等于本地 API Key 已配置。
- 当前 `.env` 中 `DATABASE_URL` 密码仍是占位符；Neon 项目、schema 和角色已创建，Neon MCP SQL 读写及 upsert 已验证，但 CLI 通过 psycopg 的连接尚未实测。
- 本执行容器缺少 psycopg 与 ImageHash，且无法在当前环境安装完整依赖；因此未完成 CLI 端到端测试。
- 并发预算原子预留、Actor schema 版本探测、视频号稳定 ID 解析、LLM discovery/adjudication、完整六方向增量状态机仍需 IDE Agent 开发。
- 8 个测试是本地单元测试，不是三个平台 API 的端到端测试。

## 下一步
在 Codex Cloud / IDE Secrets 配置 `APIFY_TOKEN` 和 Neon `DATABASE_URL`，安装依赖后先运行：
`competitor-watch doctor`、`competitor-watch db-status`、`competitor-watch doctor --check-apify`、`pytest`。
真实 Actor 测试每次都必须明确 `--execute --max-cost`，并遵守 `MAX_COST_USD`。
