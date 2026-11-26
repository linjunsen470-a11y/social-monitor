# IDE Agent 移交说明：广州团餐三平台竞品监测

## 1. 项目目标

构建一个 Agent-first CLI，输入抖音、微信视频号或小红书任意一个账号/作品入口，自动检索另外两个平台。目标行业为广州及周边团餐配送、员工餐、食堂承包。

本项目当前是基础骨架，不是生产完成品。后续 IDE Agent 应先阅读：
- `AGENTS.md`
- `.agents/skills/catering-competitor-intelligence/SKILL.md`
- `FIRST_RUN_RESULTS.md`
- `src/catering_watch/schema.sql`

## 2. 已经完成的实际基础设施

### Neon
- Neon project: `catering-competitor-monitor`
- Region: `aws-ap-southeast-1`
- Database: `catering_monitor`
- Schema: `catering_watch`
- Runtime role: `catering_watch_app`
- Project key: `guangzhou_catering_competitors`


### Apify
本轮通过已连接的 Apify MCP 实际运行了三平台 Actor，验证公开采集路径：
- Douyin: `socialdatax/socialdatax-douyin-data-api`
- WeChat Channels: `zen-studio/wechat-channels-scraper`
- Xiaohongshu: `socialdatax/socialdatax-xhs-data-api`

首轮运行结果及费用估算见 `FIRST_RUN_RESULTS.md`。本轮实际 MCP 运行成功，但这不等于本地直连 API Key 已配置或 REST client 已经完成真实 Key 验证。

## 3. API Key 状态与安全要求

本包中的 `.env` 和 `.env.example` 均为安全模板，不包含实际密钥。
用户当前 `.env` 状态核查发现：
- `APIFY_TOKEN` 尚未配置；
- `DATABASE_URL` 仍为占位符；
- `OPENAI_API_KEY` 为空。

IDE Agent 接手后：
1. 请用户在本地 IDE/Secret 管理中填入 Apify API Token。
2. 填入 Neon 的 `catering_watch_app` 角色连接串，不能使用 Neon owner。
3. `OPENAI_API_KEY` 可选；基础规则不依赖它。
4. 不要把任何密钥写进 Git、日志、CSV、JSONL 或最终公开压缩包。
5. 运行 `competitor-watch doctor --check-apify` 验证真实 Apify REST API Key。

## 4. 当前代码能力

- 直接 Apify REST API client：`src/catering_watch/apify.py`
- 三平台独立适配器：`src/catering_watch/adapters.py`
- Neon 读写、稳定 ID upsert、缓存和费用账本：`src/catering_watch/db.py`
- 封面 pHash（仅封面图）、标题/正文相似度：`src/catering_watch/matching.py`
- CLI：`src/catering_watch/cli.py`
- CSV/JSONL 导出
- `scan-url`、`scan`、`batch`、`report`、`doctor`、`db-status`、`init-db`

默认 dry-run；必须显式 `--execute`。每次 Actor 请求有 `maxTotalChargeUsd` 费用上限参数。成功查询缓存有 TTL，失败查询不覆盖旧成功记录。

## 5. 重要的待开发事项（按优先级）

### P0：先验证基础代码
- 配置 `APIFY_TOKEN` 与 `DATABASE_URL`。
- `competitor-watch doctor --check-apify`。
- 运行 pytest / ruff。
- 用低成本、单平台单关键词验证 REST API 端到端：Actor 启动、Dataset 读取、Neon upsert、费用账本。
- 检查各 Actor 最新输入输出 Schema；上游字段可能变化。
- 核实 Actor 实际费用字段 `usageTotalUsd` 是否返回；若缺失按 estimated 记账，不能伪称实际费用。

### P1：修复平台 ID 和作品规范化
- 视频号目前搜索返回字段里 `docID`、`exportId`、`hashDocID` 语义不同。不能随意把其中任意一个当最终稳定作品 ID。优先通过 `videoDetail`/`resolveVideoId` 验证，并把 opaque IDs 原样保存。
- 视频号的作者稳定 ID、主页 URL 和作品 URL 有时缺失；应把缺失状态显式记录。
- 小红书分享 URL 中可能带短期 `xsec_token`；稳定主键使用 `note_id`，链接作为证据。
- 抖音使用 `aweme_id`、`sec_user_id`，不要用显示名作为主键。
- 发布时间必须用平台返回的发布时间，不能用抓取时间或置顶顺序代替。

### P2：完成 Agent 规划层
- LLM 只负责提出品牌简称、关键词变体、跨平台搜索策略和疑难样本解释。
- Python 负责 API、限流、缓存、费用、数据库、去重和证据记录。
- 模型不可绕过预算网关直接调用付费 Actor。
- LLM Key 不配置时，确定性流程仍能运行。
- 输出应区分“同一运营方账号已发现”与“同一视频分发”。

### P3：增量和并发安全
- 增加数据库级原子预算预留（transaction/advisory lock），防止两个 Codex Cloud 沙箱同时超支。
- 将查询缓存从“关键词+时间窗”升级为包含 Actor 版本、平台参数、分页游标的规范化请求指纹。
- 处理分页、游标续跑、时间窗重叠和晚发/补发。
- 加入 `fetch_runs` 的 started/running 状态和失败冷却，不要重复发起并行的同一查询。
- 不要因为一次失败把旧成功缓存标记为无效。

### P4：匹配与报告
- 封面、标题、正文是主证据；封面 pHash 是辅助。
- 不进行关键帧或原片下载，除非用户另行授权。
- 结果分类：同一条视频候选、同素材改剪、疑似待复核、不同、无法比较。
- 运营归属独立分类：确认同一品牌自营/运营、有合作或授权证据、确认第三方转载、归属未核实。
- 单独记录查询失败、成功但零候选、返回候选但字段不足、媒体/封面打不开。
- 不能把通用厨房、员工餐、菜品等主题相似当作同视频。

## 6. 推荐命令

```bash
python -m venv .venv
# Windows PowerShell
.venv\\Scripts\\Activate.ps1
pip install -e ".[test]"

competitor-watch doctor
competitor-watch db-status
competitor-watch doctor --check-apify

# 默认 dry-run
competitor-watch scan-url --url "https://www.douyin.com/video/7711674753035228403"

# 真实运行需要用户明确授权费用上限
competitor-watch scan-url --url "https://www.douyin.com/video/7711674753035228403" --execute --max-cost 0.15

competitor-watch batch --input data/seed_accounts.csv
competitor-watch report --kind posts --format csv --output outputs/posts.csv
competitor-watch report --kind matches --format jsonl --output outputs/matches.jsonl

pytest
```

注意：目前 CSV seed 示例的列名是旧格式，IDE Agent 应统一为 `platform,url,query,max_items`，并保留旧字段兼容或迁移示例文件。

## 7. 验收条件

- 新 Codex Cloud 沙箱能连接同一个 Neon 数据库，读取历史 5 个账号/作品样本，而不重新采集。
- 同一账号/作品重复写入返回同一主键。
- 数据库断连时立即停止，不建立本地空数据库。
- Apify token 验证成功，Actor API 通过 Bearer token 直连成功。
- 每次付费请求有可执行的费用上限；并发任务共享同一预算池。
- 三平台各自 adapter 至少完成一条“真实请求→规范化→数据库 upsert→导出”的端到端测试。
- 六个方向分别记录查询状态，不能将空结果写成不存在。
- 不声称仅靠封面/标题/正文能证明视频文件相同。
- 最终输出 CSV + JSONL，且证据可回查。
- 不创建定时任务、不部署服务，除非用户单独批准。
