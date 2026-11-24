# Progress checkpoint system

Purpose: when Claude builds a reel / video package (Hindi or English, shorts or long landscape), every planning step and every piece of prepared data is saved here **as it is made**, so if Claude's limit runs out mid-task, the work can be continued later from exactly where it stopped.

## What starts a render (do NOT touch these until the package is complete)

These workflows start on a push that changes one of these files:

| Video | Workflow | Files whose push starts a run |
|---|---|---|
| Shorts (Hindi portrait) | `shorts.yml` | `jobs/package.json` |
| Long landscape (Hindi or English) | `long_landscape.yml` | `jobs/long_package.json`, `jobs/rerun.txt` |

`progress/` is NOT in any push trigger, so checkpoint commits here never start a render. Never write a half-finished package into the files above: a push there starts a real run (and posts/uploads). All drafts live in `progress/` only.

## Rules for Claude (follow on EVERY package task)

1. **Before starting anything**: open `progress/INDEX.md`. If a job is `in-progress` and matches the request (or the user says "continue"), open its `STATE.md` and resume from `## Next step`. Do not redo finished stages.
2. **At the very start of a new job**: create `progress/<YYYY-MM-DD>-<short-slug>/STATE.md` (copy `progress/_TEMPLATE/STATE.md`) and add a line to `INDEX.md`. Commit immediately.
3. **Checkpoint often**: after each stage below (and for image prompts, after every ~5-10 prompts; for long videos with many scenes, every ~10-20) update the job folder and commit with message `wip(progress): <slug> - <what was just done>`. Small commits are fine; never wait to push everything in one go.
4. **Always update `STATE.md`** in the same commit: tick the checklist, write the exact `## Next step`, and note any decisions (voice style, art style, language, format, length, special user instructions).
5. **Save the real content, not summaries**: full script text, full image prompts, full payload JSON. Anything needed to continue must be inside the repo.
6. **Only the last step starts a render**: when `04_payload.json` is complete and valid, copy it to `jobs/package.json` (shorts) or `jobs/long_package.json` (long) in one commit. That push is what starts the run. Then set status `done` in `STATE.md` and `INDEX.md` (a second `progress/` commit).
7. If the render needs a re-run of the same long package, change `jobs/rerun.txt` only (see `long_landscape.yml`), and note it in `STATE.md`.
8. Never put secrets or API keys in this folder.

## Job folder layout

```
progress/<YYYY-MM-DD>-<slug>/
  STATE.md            status, checklist, decisions, NEXT STEP (always current)
  01_plan.md          idea, format, language, art style, voice style, length, user instructions
  02_script.md        final script (word for word if user gave it)
  03_image_prompts.json   array of prompts; STATE.md tracks done/total
  04_payload.json     final package (copied to jobs/ only at the very end)
  notes.md            anything else (research, trending topic sources, problems)
```

Shorts and long videos use the same layout. Set `format` in `STATE.md` to one of: `shorts-hindi`, `shorts-english`, `long-hindi`, `long-english`.

## Stages (checklist order)

1. Plan (idea / topic / user script received, format, language, style choices)
2. Script written or saved
3. Voice / delivery style decided
4. Image prompts (count must match total scenes), saved in batches
5. Payload assembled and saved as `04_payload.json` (inside `progress/` only)
6. Payload copied to `jobs/package.json` or `jobs/long_package.json` (this starts the run)
7. Verified started, job marked done
