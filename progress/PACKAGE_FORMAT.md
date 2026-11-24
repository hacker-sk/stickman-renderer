# Hindi shorts package format (jobs/package.json)

Read this before writing `jobs/package.json`. It is built from the current working package (commit a8f5425, Guru Nanak reel) and from rules the user has stated. Items marked CONFIRM are not verified as current: ask the user before relying on them.

## Fields (all at top level)

- `title`: short Hindi title
- `description`: Hindi description for the post
- `mood`: one of inspiring, emotional, mysterious, dramatic, upbeat, calm (picks the background music)
- `hashtags`: list of Hindi/English hashtags starting with `#`
- `tags`: list of plain tags
- `hook`: `{ "line": "<Hindi hook>", "focus": "<the one word to highlight>" }`
- `art_style`: currently `Indian Sepia Stickman Cartoon` (CONFIRM it is still the style wanted)
- `voice_style`: one English sentence describing the narrator's delivery
- `random_bg`: false
- `bgm`: true
- `scenes`: list of `{ "narration": "<Hindi line>", "image_prompt": "<English prompt>" }`, one image per narration phrase

## Rules the user has stated

- Script is storytelling: a situation, a conflict, a realization and a memorable ending, not a quote page; evergreen or trending topics both fine
- Image prompts must be self-contained (the image model has no memory): repeat the full character description in every scene where that character appears
- Describe every element concretely (sizes, clothing, objects); people appear only when the phrase needs them, otherwise show objects, places, nature or symbols
- No text, letters or digits inside images (use icons, not words or numbers)
- Hindi voiceovers may run up to 3 minutes
- Push `jobs/package.json` only when the user says so (the push starts a render and posts to Buffer)
- Never put a half-finished package in this file; build drafts in `progress/<job>/` and write the package once at the end
- Religious figures: do not depict Gurus or deities' faces; use symbols, hands or light (applied for the Guru Nanak reel)

## Not in this file (not currently known to be in the package)

- `thumbnail_prompt` exists only on branch `thumbnail-end-frame`; do not add it to `main` packages unless the user asks
