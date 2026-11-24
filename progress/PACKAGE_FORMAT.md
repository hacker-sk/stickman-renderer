# Hindi shorts package format (jobs/package.json)

Read this before writing `jobs/package.json`. The real spec is the docstring at the top of `pipeline/package.py` (the code that reads the package): if this file and that docstring ever differ, the docstring wins. Re-read it on every package task.

## Fields

- `title` (required), `description` (required): Hindi
- `mood` (required): motivational | emotional | neutral (older inspiring, mysterious, dramatic, upbeat, calm still work); picks the background music only when `bgm` is true
- `hashtags`, `tags` (optional lists)
- `hook` (optional): a string, or `{ "line": "...", "focus": "<word to highlight>" }`; default is the script opening
- `art_style` (optional): only a label for the renderer's fallback image providers. It adds NO style wording, so the full style must be written inside every image_prompt
- `voice_style` (optional): delivery style for this script
- `random_bg` (optional, default false), `bgm` (optional, default false)
- `scenes` (required): list of `{ "narration": "Hindi in Devanagari", "image_prompt": "English prompt used as written" }`
- Any other key (for example `_instructions`) is ignored by `pipeline/package.py`

## Checks the code enforces

- Valid JSON object; title, description and a valid mood present; scenes not empty
- Every narration non-empty and with NO Latin letters (write English words in Devanagari)
- Every image_prompt at least 40 characters; cut at 1900 characters

## Image prompt rules (from the docstring)

- Used exactly as written, nothing is added: each prompt must draw its picture alone (the image model has no memory)
- Include subjects with full look (age, face, build, clothing, colours, one distinguishing feature, expression, pose, action), environment, time of day, composition, framing, camera angle, lighting, colours, mood
- Include the complete art style wording, the same words in every prompt of the video
- No readable text, letters, numbers, logos or watermarks
- Recurring character/place/object: repeat its full description in the same words in EVERY prompt that shows it
- Never write "same character", "as before", "again", or refer to another image or the script
- Neighbouring prompts differ in subject, setting, framing and camera angle unless the story needs the same

## Rules the user has stated

- Storytelling scripts: a situation, a conflict, a realization and a memorable ending
- Show people only when the phrase needs them, otherwise objects, places, nature or symbols
- Hindi voiceovers may run up to 3 minutes
- Push `jobs/package.json` only when the user says so (the push starts a render and posts to Buffer). ANY change to that file starts a run, so never edit it just to fix notes or comments
- Never put a half-finished package in this file; build drafts in `progress/<job>/`
- Do not depict Gurus or the faces of deities; use symbols, hands or light
- CONFIRM with the user: whether `Indian Sepia Stickman Cartoon` is still the style wanted

## `_instructions` key

Every new package should start with an `_instructions` key holding a short reminder (read `progress/PACKAGE_FORMAT.md` and the `pipeline/package.py` docstring, save progress as you go, push only on the user's command). It has no effect on the render.
