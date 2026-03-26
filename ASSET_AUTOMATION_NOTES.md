# Asset automation (HARO / SOS outreach)

## Modules added

| Module | Role |
|--------|------|
| `app/asset_types.py` | `AssetMode`, `AssetPlan`, `AssetCandidate`, `AssetContext` |
| `app/asset_planner.py` | `prepare_assets_for_request()` — rules + optional LLM brief refinement |
| `app/asset_finder.py` | `find_candidate_assets()` — scans env-configured dirs; TODO for DB libraries |
| `app/image_generator.py` | `generate_candidate_images()` — provider abstraction + stub (no vendor lock-in) |
| `app/asset_ranker.py` | `rank_and_select_assets()` — heuristic scores with component breakdown |
| `app/asset_send_guard.py` | `should_auto_send_with_assets()` — blocks risky auto-sends |
| `app/asset_orchestrator.py` | `run_asset_reply_pipeline()` — wires planner → finder → rank → draft → guard |

## Asset modes

- **no_visuals** — Text-only pitch; no attachments.
- **real_only** — Only verified/real assets may be attached; AI concepts are not auto-used as substitutes for real work.
- **concept_allowed** — If policy allows, AI-generated *styling concepts* may be produced; they are never labeled as real client projects.

## Auto-send guardrails

`should_auto_send_with_assets()` blocks auto-send when (non-exhaustive): real projects are required but only AI assets exist; geography is required but assets are not verified; the query says “no AI” but AI assets are selected; draft text contradicts asset reality; `AUTO_SEND_CONCEPT_VISUALS` / `AUTO_SEND_REAL_ASSETS` disable the corresponding send path.

When blocked, `manual_review_required` is set on the `Reply` and the message stays **DRAFT** (not auto-sent).

## Wiring a real asset library

1. Set `BUSINESS_LIFESTYLE_IMAGE_DIRS` to a comma-separated list of directories containing image files (`.jpg`, `.png`, `.webp`).
2. Optionally set `EDITORIAL_ASSET_LIBRARY_DIR` for a second tier.
3. Extend `find_candidate_assets()` to query a future `media_assets` table or CDN API — hooks are explicit TODOs in code.

## Wiring a real image generation provider

1. Implement `ImageGenerationProvider` in `app/image_generator.py` (see `StubImageGenerationProvider`).
2. Update `get_provider()` to select your vendor based on env (e.g. `IMAGE_GEN_PROVIDER=openai`).
3. Map returned bytes into `AssetCandidate` rows with `source_type="ai_generated"`, `is_real=False`.

## Config flags (env)

| Variable | Default | Meaning |
|----------|---------|---------|
| `ENABLE_ASSET_AUTOMATION` | false | Master switch for the asset pipeline + metadata on replies. |
| `ENABLE_AI_CONCEPT_VISUALS` | false | Allow generation path (still subject to guardrails). |
| `ENABLE_INLINE_IMAGE_PREVIEWS` | false | SMTP attaches preview images when paths exist. |
| `MAX_INLINE_PREVIEW_IMAGES` | 2 | Cap on attachments per message. |
| `MAX_GENERATED_CANDIDATES` | 6 | Upper bound for generator stub / future providers. |
| `AUTO_SEND_CONCEPT_VISUALS` | false | Allow auto-send when AI concept assets are selected. |
| `AUTO_SEND_REAL_ASSETS` | false | Allow auto-send when real assets are attached. |
| `ASSET_PLANNER_USE_LLM` | false | Optional OpenAI pass to refine `visual_brief`. |

## Database

New nullable / defaulted columns on `replies`: `asset_mode`, `asset_plan_json`, `selected_asset_metadata_json`, `attachment_paths_json`, `inline_preview_paths_json`, `full_res_link`, `must_disclose_ai`, `manual_review_required`, `manual_review_reason`, `asset_send_status`. Applied automatically on startup via `ALTER TABLE` helpers in `app/db.py` (SQLite-friendly).

## Manual review states

- **`NEEDS_REAL_ASSETS`** — Query required real imagery; none found in configured libraries.
- **`NEEDS_ASSETS`** — Concept mode allowed generation but none were produced (e.g. stub provider).
- Guard reasons stored in `manual_review_reason` (e.g. `auto_send_real_assets_disabled`).

## Safety rules (enforced in code)

- AI-generated assets are **never** described as real client projects in drafting instructions.
- Regency refusal-draft suppression and existing auto-send logic remain in place.
- Plain-text email body; attachments are optional and controlled by config.
