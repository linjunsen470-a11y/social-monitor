# Agent instructions

Load `.agents/skills/catering-competitor-intelligence/SKILL.md` before working.

This repository is an initial CLI skeleton, not a finished autonomous research agent.
The persistent source of truth is the isolated Neon project `catering-competitor-monitor`,
schema `catering_watch`, project key `guangzhou_catering_competitors`.

Before paid collection:
- run `competitor-watch doctor`
- run `competitor-watch db-status`
- run `competitor-watch doctor --check-apify`
- inspect cache and existing IDs
- present the query plan and cost cap
- require `--execute` and an explicit `--max-cost`

Never fall back to SQLite/local files if Neon fails. Do not infer same-video identity
or common ownership from title/cover/body alone. Do not download original videos by default.
