# 广州团餐竞品三平台监测

Agent-first Python CLI for public competitor research across Douyin, WeChat Channels and Xiaohongshu.

## 当前状态
- Neon 项目已建立并隔离：`catering-competitor-monitor`
- Database：`catering_monitor`
- Schema：`catering_watch`
- Runtime role：`catering_watch_app`（仅限 schema 读写）
- Apify REST 鉴权及三平台采集已有实测记录。本次开发验收使用历史响应回放。
- 没有创建定时任务或部署服务。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
cp .env.example .env
```

首次使用请先按「账号级追踪与恢复」初始化数据库，再在 `.env` 填入 Neon `catering_watch_app` 角色连接串和 Apify API Token。

## 专用 Codex Cloud 运行环境

项目计划使用单独的 Codex Cloud 环境。由环境维护者将下表配置注入**运行 CLI 的进程**。本地开发仍可使用 Git 忽略的 `.env`。Cloud 中可以省略复制 `.env.example` 的步骤。

| 变量 | 用途 |
| --- | --- |
| `DATABASE_URL` | Neon 的 `catering_monitor` 数据库 TLS 连接串。日常采集、回放、报告使用 `catering_watch_app` 运行角色。 |
| `APIFY_TOKEN` 或 `APIFY_API_KEY` | Apify 密钥。两者都设置时优先使用非空的 `APIFY_TOKEN`。回放与报告仅需数据库。 |
| `MAX_COST_USD`、`DAILY_BUDGET_USD` | 单命令最高额度、项目 UTC 日预算。每次在线执行仍需传入 `--max-cost`。 |

在环境的依赖安装步骤运行 `python -m pip install -e ".[test]"`。将密钥保存在专用环境的受控凭据配置中，并确认任务执行阶段可以读取它们。仅在 setup 阶段可见的 secret 不能为随后启动的 CLI 提供鉴权。避免在 setup 中打印凭据或把它们落入工作树。允许任务访问 Neon 数据库端点的 PostgreSQL TLS 端口及 `api.apify.com` 的 HTTPS 端口。

首次安装或从旧版升级时，使用建表角色执行一次 `competitor-watch init-db`，创建调研快照表并授权，随后切回运行角色。在同一任务环境中运行 `competitor-watch doctor`、`competitor-watch db-status`、`competitor-watch doctor --check-apify`。普通 `doctor` 验证数据库，可用于只做回放的环境；加 `--check-apify` 后还会验证真实 Apify 凭据。

## 从微信关键词生成竞品调研表

```bash
competitor-watch research --query "广州食堂承包" --query "东莞团餐配送"
# 核对计划、缓存和预算后再执行
competitor-watch research --query "广州食堂承包" --query "东莞团餐配送" --max-items 12 --peer-items 3 --max-details 2 --execute --max-cost 1.00 --output outputs/research.xlsx
competitor-watch research-report <调研UUID> --output outputs/research-copy.xlsx
```

`research` 逐词搜索微信作品，在同一预算内补查缺少账号 ID 的作品，再按发布者昵称或人工选定品牌检索抖音和小红书。每词最多取 `--max-items` 行，每个品牌每端最多取 `--peer-items` 行，整项任务最多补查 `--max-details` 条详情。详情通过请求中的作品标识关联。搜索结果保留原始检索词、作品 ID、昵称、平台链接和运行 ID。微信预览链接能否播放受平台和客户端影响。

Excel 包含竞品三端核查、微信视频作品、跨平台作品证据、策略观察及检索记录。自营判断沿用目录中的认证主体和有公开依据的账号映射，其他检索命中保留为候选。策略观察统计命中采购相关用语的作品并列出样本。部分失败、预算跳过或缺少回放记录时，命令保存已有证据并返回非零退出码。成功查询且结果为空会单独标记。全失败时也会打印调研 UUID，原有 Excel 保留。可使用该 UUID 另行导出失败记录或重新导出成功快照。

### 回放历史数据与人工核对

将留存的 Dataset 整理为本机 `data/replays/runs.json`。每行检索由 `platform`、`operation`、`query` 唯一定位；文件为 JSON 数组。例如：

```json
[
  {"platform":"channels","operation":"videoSearch","query":"广州食堂承包","run_id":"原运行ID","rows":[{"exportId":"export/example","creatorName":"示例餐饮","title":"广州厂区食堂承包"}]},
  {"platform":"douyin","operation":"public_post_search","query":"示例餐饮","run_id":"原抖音运行ID","rows":[]},
  {"platform":"xiaohongshu","operation":"public_post_search","query":"示例餐饮","run_id":"原小红书运行ID","status":"FAILED","rows":[]}
]
```

`videoDetail` 的 `query` 为该次单作品详情请求的 exportId。失败记录填入其原始状态。支持 SocialDataX 标准字段、ToolzerHub 搜索结果及抖音批量搜索的扁平和 authorMeta 字段。回放不调用 Actor，也不下载封面。回放同样写入 Neon，每次产生独立调研 UUID。

```bash
competitor-watch research --query "广州食堂承包" --replay data/replays/runs.json --max-details 0 --execute
```

可选的 `--review data/replays/review.json` 可与在线检索或回放合用。其中 `selection` 将选中的微信 exportId 映射到品牌名，至少填写一项，省略时使用各作品发布者作为线索。`terms` 指定各品牌在目标平台的实际搜索词，`video_links` 保存人工核对过的作品链接，`reviews` 保存归属核对结论及公开依据。链接在保存前会去掉凭据及跟踪参数。未提供人工结论的线索会标记为待核实。

```json
{
  "selection": {"export/example": "示例餐饮"},
  "terms": {"示例餐饮": {"douyin": "示例餐饮集团"}},
  "reviews": {"示例餐饮": {"conclusion": "已在官网核对账号入口", "urls": ["https://example.org/accounts"]}}
}
```

## 常用命令

```bash
competitor-watch doctor
competitor-watch db-status
competitor-watch doctor --check-apify

# 默认 dry-run
competitor-watch scan-url --url "https://www.douyin.com/video/7711674753035228403"

# 实际采集必须显式执行并限定费用
competitor-watch scan-url --url "https://www.douyin.com/video/7711674753035228403" --execute --max-cost 0.15

competitor-watch scan --platform douyin --query "广州团餐配送"
competitor-watch batch --input data/seed_accounts.csv
competitor-watch report --kind posts --format csv --output outputs/posts.csv
pytest
```

`batch` 按剩余有效行数均分当前可用预算，后续行可使用前面缓存命中或结算后节省的额度。完成后统一导出到 `outputs/latest_scan.csv` 和 `outputs/latest_scan.jsonl`。部分行失败时仍保存成功结果并返回非零退出码，全部失败时保留上次导出。

## 关键边界
- 以封面、标题、正文为主，不下载原片、不做关键帧分析。
- 元数据匹配只产生候选，不证明两个视频文件相同。
- 视频关系与账号运营归属分开判定。
- Neon 不可用时停止，不创建本地回退数据库。
- 付费 Actor 默认不运行；必须显式 `--execute`。
- 不创建定时任务、不部署。

首轮试采和开发背景见[实测记录](FIRST_RUN_RESULTS.md)、[原始交接说明](docs/IDE_AGENT_HANDOFF.md)和[早期构建记录](docs/BUILD_STATUS.md)。

## 自营视频覆盖和整条重发报告

从项目根目录安装后，使用运行角色的 `DATABASE_URL` 导出近六个自然月内发布的作品。报告从当前项目的数据库读取作品和检索记录。导出阶段无需 Apify token。

```bash
competitor-watch report --kind coverage --format csv --output outputs/coverage.csv
competitor-watch report --kind reposts --format jsonl --output outputs/reposts.jsonl
```

`coverage` 每家竞品一行，三端分别列出状态、作品证据和检索记录。只有可识别为视频、发布时间在窗口内、且有自营证据的作品才计入平台数。公开认证主体须与目录中的企业全名相符，或由有公开依据的稳定账号 ID 映射确认。仅出现品牌词或商家认证标识时保留待核实状态。已查未检出只描述该次检索。

`reposts` 每条观察到的相关源视频一行。每个目标平台保留与源视频直接比较后满足条件的自营作品。封面 64 位 pHash 距离最多 8 位，去除品牌名及常见团餐词后的标题或正文至少 8 字且相似度达到 88%。双端有时长时，差值上限取 2 秒与较短视频时长的 5% 中较大者。发布时间保留在证据中，允许延迟分发。缺少时长会标注；缺少封面指纹或独特文案时只能给出证据不足。疑似平台数含源平台，无合格配对时为 0。CSV 中的证据列表采用 JSON 编码。

内置目录见 `src/catering_watch/competitors.json`。可将 `COMPETITOR_CATALOG` 指向同结构 JSON 文件，添加企业认证全名、别名及种子作品 ID。人工核实的自营账号可在对应品牌下配置 `accounts` 列表，每项填写 `platform`、平台稳定账号 `id` 和公开证明链接 `evidence_url`。抖音种子优先回读已存作品，再按品牌、认证主体和别名在另外两端检索；指定 `--query` 可选用单个检索词。真实运行需要显式费用上限。

## 版本库约定

提交源码、测试、种子样例和开发说明。实际连接信息放在本地 `.env` 或运行环境的 Secrets 中。默认报告写到 `outputs/`。本地回放放在 `data/replays/`，原始数据放在 `data/raw/`。Git 会忽略这些目录、Excel 工作簿和 Python 构建产物。PR 检查使用临时 PostgreSQL 和模拟 Apify 响应。

## 账号级追踪与恢复

首次启用时，将 `DATABASE_URL` 指向新库的建表角色并执行 `competitor-watch init-db`。初始化完成后切换为运行角色进行采集和报告导出。

```bash
# 新账号无需修改内置目录。昵称检索返回候选，选择其稳定 ID 后再登记。
competitor-watch track-account "品牌名" douyin "账号昵称" --kind name --max-cost 0.05
competitor-watch track-account "品牌名" douyin "稳定账号ID" --execute
competitor-watch discover-peers "品牌名" douyin --max-cost 0.30
competitor-watch sync-account "品牌名" douyin "稳定账号ID" --max-items 20 --max-cost 0.20
competitor-watch sync-account "品牌名" douyin "稳定账号ID" --backfill --max-items 50 --max-cost 0.30
competitor-watch match-local --output outputs/local-matches.jsonl
```

采集命令默认展示操作和额度，加 `--execute` 后执行。昵称检索展示账号 ID、昵称、认证主体、简介、主页及可用的粉丝数。`discover-peers` 按品牌词筛出候选。`scan-url` 自动同步已登记账号或认证主体与目录企业全名完全一致的账号。其余候选可用 `track-account … --kind id --execute` 登记。登记表示愿意追踪，自营归属仍需公开认证主体或 `--evidence-url`。

抖音、小红书的日常同步从首页开始，在本次条数和预算内翻页。遇到已收录的非置顶作品或六个月窗口边界，会处理完当前页。加 `--backfill` 从保存的历史游标继续。若 Actor 只返回某页的一部分，回填会保留游标，需给足该页条数和额度后重试。视频号只支持最新页，返回的窗口内作品整页入库。无时区的 ISO 发布时间按上海时间解释。封面签名按采集时的原值保存，签名过期时，需要平台返回新链接才能补算指纹。

`--max-cost` 限制单次命令。`DAILY_BUDGET_USD` 限制项目 UTC 日额度，默认值为 `MAX_COST_USD`。新 run 先预留请求上限。Apify 明确拒绝启动时释放预留。返回终态费用 `usageTotalUsd` 时按实收结算。缺少实收值时，账本标为估算，预算按请求上限保留至结算日结束。跨日未决 run 始终占用额度。缓存和续跑已有 run 无新增预留。`APIFY_BUILD` 默认为 latest。同参数的首页也受 `CACHE_TTL_HOURS` 控制，要提高采集频率需相应缩短 TTL。

`init-db` 会给已存在的运行角色 `catering_watch_app` 授予本项目 schema 的使用权限、业务表的读写权限和序列使用权限。使用自定义运行角色时，由建表角色为它授予相应权限。

同一参数再次运行会读取已完成的 Dataset 页，或继续轮询已登记的 run。STARTING 或 RECONCILE 且缺少 provider_run_id 时，先在 Apify Console 核对同一账号、Actor、启动时间和输入。确认唯一对应后，运维可将该行的 provider_run_id 填入并将 status 更新为 RUNNING，然后重试原命令；无法确认时保持待核对。

集成测试使用独立临时 PostgreSQL。将 `CATERING_TEST_DATABASE_URL` 设置为测试库的本地 Unix socket 连接串（形如 `postgresql:///postgres?host=/tmp/测试目录`）再运行 pytest。测试会建表和写入独立的测试项目，Apify 使用模拟响应。
