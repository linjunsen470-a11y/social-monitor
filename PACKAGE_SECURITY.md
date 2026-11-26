# 本地配置

复制 `.env.example` 为 `.env`，填写只具备本项目权限的 `DATABASE_URL`。付费采集另需 `APIFY_TOKEN`。基础规则和报告无需 `OPENAI_API_KEY`。CI 使用 Secrets 注入配置。
