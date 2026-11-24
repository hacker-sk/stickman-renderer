# STATE: relationship-silence

- status: in-progress
- format: shorts-hindi
- created: 2026-11-24
- last updated: 2026-11-24
- topic / idea: relationship topic: relationships break when two people stop talking; say what is in your heart today
- source of script: written by Claude
- art style: Indian Sepia Stickman Cartoon (CONFIRM still wanted)
- voice / delivery style: soft, sincere, slightly urgent male narrator
- length target: about 10 seconds
- total scenes / image prompts expected: 4
- image prompts saved so far: 4 / 4

## User instructions for this job
- "create a 10s reel on some relationship topic and along with it add the instructions key so after that every time you read package json you would have all information"
- Push only when the user says so (standing rule)

## Checklist
- [x] 1. Plan saved (01_plan.md)
- [x] 2. Script saved (02_script.md)
- [x] 3. Voice / delivery style decided
- [x] 4. Image prompts saved (03_image_prompts.json)
- [ ] 5. Final package assembled from the saved files and written once into jobs/package.json, with an `_instructions` key at the top (starts the run; only when the user says so)
- [ ] 6. Verified, marked done

## Decisions made
- Package must include an `_instructions` key (ignored by pipeline/package.py) pointing to progress/PACKAGE_FORMAT.md and the pipeline/package.py docstring
- Recurring characters are fully re-described in every prompt (no "same", "again")

## Next step
Waiting for the user to say push. Then assemble jobs/package.json once from 01_plan.md, 02_script.md, 03_image_prompts.json (hook {"line": "रिश्ता कब टूटता है?", "focus": "टूटता"}, mood emotional, bgm true) with the `_instructions` key first, then mark done here and in INDEX.md.
