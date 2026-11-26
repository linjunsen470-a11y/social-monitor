# 广州团餐竞品三平台监测

Agent-first Python CLI for public competitor research across Douyin, WeChat Channels and Xiaohongshu.

## 当前状态
- Neon 项目已建立并隔离：`catering-competitor-monitor`
- Database：`catering_monitor`
- Schema：`catering_watch`
- Runtime role：`catering_watch_app`（仅限 schema 读写）
- 首轮样本保留 5 个账号、5 条作品、5 条别名样本。
- 三个平台的 Apify Actor 已通过连接器实测；本地直连 REST API Key 尚未配置验证。
- 没有创建定时任务或部署服务。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
cp .env.example .env
```

在 `.env` 填入 Neon `catering_watch_app` 角色连接串和 Apify API Token。不要使用 Neon owner。`OPENAI_API_KEY` 可选。

## 常用命令

```bash
competitor-watch doctor
competitor-watch db-status
competitor-watch doctor --check-apify
competitor-watch init-db

# 默认 dry-run
competitor-watch scan-url --url "https://www.douyin.com/video/7711674753035228403"

# 实际采集必须显式执行并限定费用
competitor-watch scan-url --url "https://www.douyin.com/video/7711674753035228403" --execute --max-cost 0.15

competitor-watch scan --platform douyin --query "广州团餐配送"
competitor-watch batch --input data/seed_accounts.csv
competitor-watch report --kind posts --format csv --output outputs/posts.csv
pytest
```

## 关键边界
- 以封面、标题、正文为主，不下载原片、不做关键帧分析。
- 元数据匹配只产生候选，不证明两个视频文件相同。
- 视频关系与账号运营归属分开判定。
- Neon 不可用时停止，不创建本地回退数据库。
- 付费 Actor 默认不运行；必须显式 `--execute`。
- 不创建定时任务、不部署。

详细移交说明：`docs/IDE_AGENT_HANDOFF.md`
首轮实测：`FIRST_RUN_RESULTS.md`

## 自营视频覆盖和整条重发报告

从项目根目录安装后，使用运行角色的 `DATABASE_URL` 导出近六个自然月内发布的作品。报告读取 Neon 和有出处的首轮搜索记录。导出阶段无需 Apify token。

```bash
competitor-watch report --kind coverage --format csv --output outputs/coverage.csv
competitor-watch report --kind reposts --format jsonl --output outputs/reposts.jsonl
```

`coverage` 每家竞品一行，三端分别列出状态、作品证据和检索记录。只有可识别为视频、发布时间在窗口内、且有自营证据的作品才计入平台数。公开认证主体须与目录中的企业全名相符，或由有公开依据的稳定账号 ID 映射确认。仅出现品牌词或商家认证标识时保留待核实状态。首轮测试文档中的定性结果带有来源链接及记录日期。已查未检出只描述该次检索。

`reposts` 每条观察到的相关源视频一行。每个目标平台保留与源视频直接比较后满足条件的自营作品。封面 64 位 pHash 距离最多 8 位，去除品牌名及常见团餐词后的标题或正文至少 8 字且相似度达到 88%。双端有时长时，差值上限取 2 秒与较短视频时长的 5% 中较大者。发布时间保留在证据中，允许延迟分发。缺少时长会标注；缺少封面指纹或独特文案时只能给出证据不足。疑似平台数含源平台，无合格配对时为 0。CSV 中的证据列表采用 JSON 编码。

内置目录见 `src/catering_watch/competitors.json`。可将 `COMPETITOR_CATALOG` 指向同结构 JSON 文件，添加企业认证全名、别名及种子作品 ID。人工核实的自营账号可在对应品牌下配置 `accounts` 列表，每项填写 `platform`、平台稳定账号 `id` 和公开证明链接 `evidence_url`。抖音种子优先回读已存作品，再按品牌、认证主体和别名在另外两端检索；指定 `--query` 可选用单个检索词。真实运行需要显式费用上限。

## 版本库约定

提交源码、测试、种子样例和开发说明。实际连接信息放在本地 `.env` 或运行环境的 Secrets 中。报告写到 `outputs/`，构建产物、缓存、压缩包和本地配置由 Git 忽略。
