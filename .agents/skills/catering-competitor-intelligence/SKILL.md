---
name: catering-competitor-intelligence
description: Research cross-platform public competitor accounts and video posts for Guangzhou-area catering, employee meals and canteen outsourcing.
---

# Operating contract

1. Start with `competitor-watch doctor`, `competitor-watch db-status`, and `competitor-watch doctor --check-apify`.
2. Never collect if Neon is unavailable. Never create a local fallback database.
3. Resolve platform and stable account/post identifiers before searching. A display name is an alias, not a primary key.
4. Query the persistent Neon database before any paid Actor run. Reuse existing account/post records and recent successful query cache.
5. Propose a bounded query plan and cost cap. Keep `--max-cost` within `MAX_COST_USD`; default to dry-run.
6. Use only public data returned by platform-compatible adapters. Do not bypass login, CAPTCHA, access controls or platform restrictions.
7. Use cover, title and body text for candidate ranking. Do not claim that metadata proves two video files are identical.
8. Keep video relation and account/operator ownership as separate conclusions.
9. Record source URLs, platform IDs, source timestamps, Actor run IDs, status, and estimated/actual costs.
10. A failed or empty search is not proof that a platform has no account or post. Never overwrite a previous successful observation with a failed query.
11. Ask the user before any spend not already explicitly approved by the current task's budget.
12. Do not create schedules, deploy services, send messages, or automate recurring work unless separately approved.
