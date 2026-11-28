# STATE: 2026-11-28-sabka-sahara

format: shorts-hindi
status: done (run not verified)

## Checklist
- [x] 1 Plan (01_plan.md)
- [x] 2 Script (02_script.md)
- [x] 3 Voice / delivery style (in 01_plan.md)
- [x] 4 Image prompts 12/12 (03_image_prompts.json)
- [x] 5 Package written once into jobs/package.json (commit fb112eb), starts the run
- [ ] 6 Verify the Hindi Shorts run started and the reel posted to Buffer

## Decisions
- Request: viral-potential reel built from the Instagram analysis (ekminute.tv, 2026-11-28); user asked to create it with shorts.yml
- Art style: Sketchbook Doodle Stickman; bgm true, mood emotional, random_bg false
- Rerun 2026-11-28: user asked to rerun the latest Shorts run by repush; same package re-committed to jobs/package.json with an ignored "_rerun" key added so the file changes (commit bb2a386), which starts a new Hindi Shorts run

## Next step
None for the package. Optionally check the Actions run and the Buffer post.
