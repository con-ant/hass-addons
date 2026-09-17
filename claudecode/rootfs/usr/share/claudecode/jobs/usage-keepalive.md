---
description: Keeps the claude.ai token fresh for the usage sensors
model: haiku           # the cheapest model; this job exists only to make one API call
timeout: 120
max_cost_usd: 0.05
max_turns: 2
min_interval: 900
tools:
  - Bash(ha core info)   # never used; the frontmatter needs one entry
---
# Token keepalive

Reply with the single word `OK` and nothing else. Do not run any tool.

(Why this job exists: the add-on's Claude subscription usage sensors read the claude.ai
access token that the Claude Code CLI keeps in its credential store. The CLI renews that
token only when it makes a model call, so on an install where the terminal session sits
idle for hours the token expires, `api.anthropic.com` answers 401, and the sensors go
`stale`. Running this job makes the CLI itself renew the token through its own locked
refresh path; the add-on never refreshes tokens on its own. Schedule it every few hours
with the "Claude Job · run on a schedule" blueprint, or set the add-on option
`usage_keepalive_job: usage-keepalive` and the endpoint runs it only when the token has
actually expired. See README "Claude subscription usage".)
