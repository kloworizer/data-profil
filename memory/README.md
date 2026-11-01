# Memory

What AutoEDA has learned from comments on its reports. Commit this folder to share the
learning between machines (`git pull` brings in skills learned elsewhere).

* `skills.json`: the skills: instruction for the AI, hints for the table readers, and the
  keywords that tell which files a skill applies to. Edit them on the app's Memory page
  (`/memory`) or here.
* `feedback.jsonl`: every comment, one JSON object per line (append-only, merges cleanly).

How often each skill was used is kept per server in `data/memory_usage.json`, not here.
To start over, use "Clear memory" on the Memory page, or delete both files.
Another location: `AUTOEDA_MEMORY_DIR`.
