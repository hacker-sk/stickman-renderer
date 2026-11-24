# stickman-renderer
Renders Stickman Shorts videos on GitHub Actions

## For Claude: read this before any reel / video package task

1. Open `progress/INDEX.md` and `progress/README.md` FIRST and follow them on every package task.
2. At the start of a new job, create `progress/<YYYY-MM-DD>-<slug>/STATE.md` (copy `progress/_TEMPLATE/STATE.md`) and add a line to `progress/INDEX.md`.
3. Save the plan, script and image prompts into the job folder as each step is done, not after the push. Small `wip(progress): ...` commits are fine.
4. Only the final step writes `jobs/package.json` or `jobs/long_package.json` (that push starts a render). For Hindi shorts, push only when the user says so.
