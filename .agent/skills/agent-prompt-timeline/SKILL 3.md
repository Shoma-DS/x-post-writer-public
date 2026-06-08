---
name: agent-prompt-timeline
description: Record and maintain a chronological learning log of agent prompts, original user instructions, work summaries, and follow-up actions. Use when the user asks to preserve prompts, make an agent work history site, add prompt logging hooks, summarize what was done after a task, or update the prompt timeline.
---

# Agent Prompt Timeline

## Purpose

Use this skill to keep `prompt-timeline/` useful as a study log. The automatic hook records raw user prompts. The agent should add a short summary after meaningful work so the site shows both "what was asked" and "what was done". The same skill can install the timeline site and hooks into other repositories.

## Standard workflow

1. Read the latest prompt event when needed:
   ```bash
   python "$TEAM_INFO_ROOT/.agent/skills/agent-prompt-timeline/scripts/record_event.py" --print-latest
   ```
2. After finishing a task, append a summary to the latest unsummarized prompt:
   ```bash
   python "$TEAM_INFO_ROOT/.agent/skills/agent-prompt-timeline/scripts/record_event.py" \
     --kind summary \
     --summary "[1-3 sentence summary of what was done]" \
     --actions "[key files changed, tests run, or next action]" \
     --update-latest
   ```
3. If the summary belongs to a specific event, pass its id:
   ```bash
   python "$TEAM_INFO_ROOT/.agent/skills/agent-prompt-timeline/scripts/record_event.py" \
     --kind summary \
     --parent-id "[event id]" \
     --summary "[summary]" \
     --actions "[actions]"
   ```
4. Open `prompt-timeline/index.html` to inspect the study site. If the browser cannot load local files, serve the folder:
   ```bash
   python -m http.server 8765 --directory "$TEAM_INFO_ROOT/prompt-timeline"
   ```

## Install into repositories

Install into the current repository:

```bash
python "$TEAM_INFO_ROOT/.agent/skills/agent-prompt-timeline/scripts/install_prompt_timeline.py" \
  --target "$TEAM_INFO_ROOT"
```

Install into every git repository under a workspace folder:

```bash
python "$TEAM_INFO_ROOT/.agent/skills/agent-prompt-timeline/scripts/install_prompt_timeline.py" \
  --all-under "[absolute workspace path]" \
  --max-depth 4
```

The installer copies:

- `.agent/skills/agent-prompt-timeline/`
- `prompt-timeline/`
- `.claude/settings.json` `UserPromptSubmit` hook
- `.codex/hooks.json` `UserPromptSubmit` hook

It preserves `prompt-timeline/data/events.jsonl` if it already exists and does not edit `.gitignore`.

## Repository-specific public URL

Each repository must have its own prompt timeline Vercel URL. Do not reuse one
repository's prompt timeline URL as the canonical URL for another repository.

Store the canonical URL in:

```text
prompt-timeline/data/site.json
```

Minimum shape:

```json
{
  "repo_name": "team-info",
  "vercel_url": "https://prompt-timeline.vercel.app"
}
```

When installing into another repository, create `site.json` if it is missing.
After publishing that repository's timeline on Vercel, update `vercel_url` with
the verified production URL and rebuild the static site snapshot.

## Publish on Vercel

For a static deployment:

```bash
npx vercel deploy "$TEAM_INFO_ROOT/prompt-timeline" --prod --yes
```

If Vercel asks to link or create a project, use the project name that matches the repository plus `-prompt-timeline` where possible. After deployment, verify the page loads and shows timeline entries:

```bash
agent-browser open "[deployment-url]"
agent-browser eval "({title: document.title, items: document.querySelectorAll('.timeline-button').length})"
```

## Hook behavior

The shared Claude/Codex hooks call `scripts/record_event.py` on `UserPromptSubmit`. The script writes:

- `prompt-timeline/data/events.jsonl`: append-only source log
- `prompt-timeline/assets/events.js`: browser-friendly snapshot for `index.html`

The hook must stay local-only. Do not add network calls, API calls, model calls, Docker, long-running work, or secret-reading behavior to the hook.

## Summary quality

Write summaries for learning, not commit logs.

- Preserve the user's original intent in plain language.
- Name the main files or systems touched.
- Mention verification or known gaps.
- Keep it short enough to scan in the timeline.

## Maintenance

When changing the event schema, update both `scripts/record_event.py` and `prompt-timeline/assets/app.js`. Keep backward compatibility with existing JSONL events whenever possible.
