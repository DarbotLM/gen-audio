# Live-tile faces contract (C13)

Status: **implemented contract for the current main tree**. This document replaces historical
implementation notes. It describes the code and tests that are present now, including the
one-release compatibility behavior.

The contract is split across the core reducer, MCP control bus, desktop renderer, viewport
validators, and release gate tests:

- `crates/gen-audio-core/src/viewport.rs`
- `crates/gen-audio-core/src/cards.rs`
- `crates/gen-audio-mcp/src/control.rs`
- `crates/gen-audio-mcp/src/lib.rs`
- `apps/desktop/src/glyph.ts`
- `apps/desktop/src/livestrip.ts`
- `apps/desktop/src/render.ts`
- `apps/desktop/src/main.ts`
- `apps/desktop/src/env.ts`
- `apps/desktop/src/validate.ts`
- `schemas/card-viewport.schema.json`

## 1. Card identity

Every card in a viewport document has two identifiers:

- `id`: the runtime tile id, such as `lib-misaki-kokoro` or `profile-anton`.
- `uid`: the required content-addressed card identity, matching
  `ga:card:[a-z2-7]{25}[aeimquy4]`.

`id` remains the selector used by desktop tiles and most UI tools. `uid` is required because the
card glyph is derived from it. A missing, malformed, or non-card uid is rejected; the renderer does
not invent a glyph for an unidentified card.

The rule is enforced in:

- `apps/desktop/src/validate.ts`: `ViewportCard.uid` and `CARD_UID`.
- `crates/gen-audio-core/src/cards.rs`: `validate_viewport`.
- `schemas/card-viewport.schema.json`: viewport `cards.items` requires `uid`.

The schema intentionally requires `uid` at the viewport-card item boundary rather than inside
`$defs/card`. The same card definition is also the body of an asset-envelope card, whose identity
uid lives on the asset envelope itself. `schemas/examples/viewport.missing-uid.negative.json` is the
negative test input.

## 2. Ordered faces

A face is an ordered `{id, name}` pair. `front` and `back` are aliases for positions, not face ids.
`name` is used by the desktop glyph label. The canonical face tables are owned by
`Viewport::faces_for` in `crates/gen-audio-core/src/viewport.rs`.

| Tile kind | Full ordered table |
|---|---|
| Audio clip (`LibraryClip` / `audio_clip`) | `clip: Clip`, `cube: Cube`, `layers: Layers`, `spectrogram: Spectrogram`, `relations: Relations` |
| Voice model (`EngineStatus` / `voice_model`) | `model: Model`, `cubes: Cubes`, `relations: Relations` |
| Connector (`ConnectorStatus` / `connector`) | `connector: Connector` |
| Voice profile (`VoiceProfile` / `voice_profile`) | `profile: Profile`, `persona: Persona`, `relations: Relations` |
| Generated cube view | `cube: Cube` |
| Generated spectrogram view | `spectrogram: Spectrogram` |
| Generated video view | `video: Video` |

An applicable face list is written to `views[].faces`. Omitted faces are absent from that list,
absent from `face_count`, and cannot be selected.

The desktop needs a usable face tree before the first `viewport_get` response.
`facesForCard` in `apps/desktop/src/render.ts` creates provisional faces from the viewport card
body. The bootstrap `resyncViewport` in `apps/desktop/src/main.ts` then replaces the provisional
state with the core reducer's `ui.faces` state. The reducer remains authoritative for face count
and position.

Fixture-only kinds that are not release cards (`SpectrogramPanel`, `Cube3D`, `PodcastCast`,
`BenchmarkCompare`, and `ServeHealth`) receive the desktop's provisional `summary` / `details`
faces. They are excluded from production by the release fixture gate.

### 2.1 Applicability

`Viewport::faces_for` computes the applicable list from catalog and media facts:

| Face | Applies when |
|---|---|
| `clip`, `model`, `connector`, `profile` | Always; it is the first face of its kind. |
| `cube`, `layers`, `spectrogram` | The audio clip has a present WAV. |
| `cubes` | A voice model has at least one real cube row. G2P-only models and models whose clips have no WAV omit it. |
| `persona` | The persona has profile facts such as domain, accent, traits, or refs. |
| `relations` | At least one relation applies. The current connector facts define no connector relation types. |

The release face counts are:

| Tile | Applicable ids | `face_count` |
|---|---|---:|
| `lib-misaki-kokoro`, `lib-cube-explainer`, `lib-kokoro`, `lib-kokoro-onnx`, `lib-bitdot-braille-vibevoice` | `clip`, `cube`, `layers`, `spectrogram`, `relations` | 5 |
| `lib-magpie`, `lib-vibevoice`, `lib-pocket` | `clip`, `relations` | 2 |
| `engine-kokoro`, `engine-kokoro-dayour`, `engine-vibevoice` | `model`, `cubes`, `relations` | 3 |
| `engine-misaki` | `model`, `relations` | 2 |
| `engine-magpie`, `engine-pocket` | `model` | 1 |
| `conn-mcp`, `conn-acp`, `conn-harness`, `conn-copilot`, `conn-claude`, `conn-gpt`, `conn-gemini` | `connector` | 1 |
| Each `profile-*` release tile | `profile`, `persona`, `relations` | 3 |

`Viewport::release` creates all eight LibraryClip views: the five baked audio-clip envelopes in
`apps/desktop/public/library/assets.json` plus the three envelope-less catalog tiles. The set is
pinned against `schemas/examples/viewport.release.json` by
`release_library_views_match_the_release_deck`.

## 3. Reducer-owned face state

For a view id such as `view:lib-misaki-kokoro`, core stores:

```rust
face_index: BTreeMap<String, usize>
```

An absent entry means index zero. `face_index` is volatile UI state; it is not part of the asset,
facts snapshot, display revision, or catalog identity.

`Action::Flip` accepts `FlipTo`:

| Selection | Behavior |
|---|---|
| `FlipTo::Next` | `face_index = (face_index + 1) % face_count`; this wraps. |
| `FlipTo::Face(id)` | Select the id's position in the applicable list. |
| `FlipTo::Face("front")` | Compatibility alias for index zero. |
| `FlipTo::Face("back")` | Compatibility alias for index one; invalid when `face_count < 2`. |

`Viewport::apply_flip` publishes the full multi-face state:

```json
{
  "op": "flip",
  "view": "view:lib-misaki-kokoro",
  "tileId": "lib-misaki-kokoro",
  "face_id": "cube",
  "face_index": 1,
  "face_count": 5,
  "ids": ["clip", "cube", "layers", "spectrogram", "relations"],
  "flipped": true
}
```

`flipped` is a one-release compatibility boolean only. It is derived as `face_index != 0`; it is
not sufficient to identify `cube`, `layers`, `spectrogram`, or `relations`. Current consumers use
`face_id`, `face_index`, `face_count`, and `ids`.

### 3.1 Only explicit Flip changes face

`face_index` changes only through `Action::Flip`:

- a card glyph click or its Enter/Space activation posts `ui_flip {next: true}`;
- an agent call to `ui_flip`;
- resynchronization applies the reducer's existing `ui.faces` state.

The following do not mutate face state:

- a card body click or keyboard event;
- timer or animation callbacks;
- playback, seek, focus, navigation, selection, compare, or cube mode;
- rename, metadata harvest, or catalog refresh;
- `library_harvest` Attach;
- a profile update or profile status message.

The desktop performs no optimistic face change on a glyph press. `main.ts`'s `show` callback posts
the request, and the tile changes only when the echoed `flip` event or `viewport_get` supplies the
new reducer state. A failed request therefore leaves the named tile unchanged.

An explicit `flip` event can carry a profile payload. `applyCardOp` in
`apps/desktop/src/render.ts` updates the profile data/status, but flips only `args.tileId`. It never
uses the payload's persona to select the profile tile.

## 4. `ui_flip` compatibility and errors

`ui_flip` is implemented in `crates/gen-audio-mcp/src/control.rs`.

Supported targeting:

- `tileId`
- `uid`, resolved to its current tile id
- `view`

At most one face selector is accepted: `next`, `face`, or `flipped`. Two or more produce
JSON-RPC `-32602`.

Current selector forms:

```json
{"tileId":"lib-misaki-kokoro","next":true}
{"tileId":"lib-misaki-kokoro","face":"spectrogram"}
{"tileId":"lib-misaki-kokoro","flipped":false}
```

A bare call keeps its historical default for this release:

```json
{"tileId":"lib-misaki-kokoro"}
```

It selects `back`, which means index one on a multi-face tile. Unknown and omitted face ids return
`-32602`; the error data includes `valid_faces` in applicable order and `aliases: ["front", "back"]`.
`back` on an `M = 1` tile is an error, while `next` remains at index zero.

### 4.1 Deprecated `section` forms

The current C13 release still accepts two legacy section forms:

1. Bare `{tileId, section}` maps the section to the applicable face and returns one warning:
   `deprecated_bare_section`.
2. `face: "back"` or `flipped: true` plus `section` maps the section to the applicable face and
   returns one warning: `deprecated_back_section`.

Each successful deprecated call:

- writes exactly one warning with `code` and `detail` into the result;
- writes exactly one matching deprecation line to MCP stderr;
- carries no warning from the other deprecation form.

`face_for_deprecated_section` reads the current applicable face list rather than guessing a fixed
face. For example, `honesty` maps to `clip` on an audio tile, `model` on a model, and `connector`
on a connector. If a model's `cubes` or `relations` face is omitted, the corresponding section maps
to the `model` face where its pending explanation is rendered.

After the compatibility window, bare section and back-plus-section are expected to be `-32602`.
The current release tests deliberately keep the successful warning behavior pinned.

## 5. `viewport_get` shape

`viewport_get` is `Viewport::snapshot`, plus the control cursor and Cube state. Its canonical
face fields are:

```json
{
  "views": [
    {
      "id": "view:lib-misaki-kokoro",
      "asset": "lib-misaki-kokoro",
      "home": "slide:library",
      "faces": [
        {"id":"clip","name":"Clip"},
        {"id":"cube","name":"Cube"},
        {"id":"layers","name":"Layers"},
        {"id":"spectrogram","name":"Spectrogram"},
        {"id":"relations","name":"Relations"}
      ]
    }
  ],
  "ui": {
    "faces": {
      "view:lib-misaki-kokoro": {
        "tileId": "lib-misaki-kokoro",
        "face_id": "clip",
        "face_name": "Clip",
        "face_index": 0,
        "face_count": 5,
        "ids": ["clip", "cube", "layers", "spectrogram", "relations"]
      }
    }
  },
  "cursor": 0,
  "gap": false,
  "resync": "viewport_get"
}
```

For an `M = 1` face list, the `ui.faces` entry also carries a `glyph_label`, for example
`Card has 1 face: Model` or `Card has 1 face: Connector`.

The older `ui.flipped` map remains for one release. It maps index zero to `front` and every other
index to `back`; consumers must not use it as a real multi-face model. If both shapes are present,
desktop `resyncViewport` prefers `ui.faces`.

Cube compare remains independent of face state. `cube_mode`, `cube_compare`, `cube_seq`, and
`cube_modes` stay in `viewport_get` alongside the reducer snapshot; no face field reuses or
overwrites those keys.

## 6. Facts and pending data

The core non-volatile facts owner is `facts_snapshot` / `facts` in
`crates/gen-audio-core/src/viewport.rs`. A view snapshot contains:

```text
uid, kind, title, honesty, media, display_rev, sections[]
```

Every section has an `id`, a `status` (`real`, `pending`, or `partial`), `facts`, and optional
section-specific fields. Missing resolved values use the literal string `pending`; no pixels,
samples, or stand-in values are placed in facts.

Face-to-section mapping:

| Face | Sections |
|---|---|
| `clip` | `identity`, `honesty`, `clip` |
| `cube` | `cube` |
| `layers` | `layers` |
| `spectrogram` | `spectrogram` |
| `relations` | `relations` |
| `model` | `identity`, `honesty`, `model`; omitted `cube` / `relations` facts can appear here with a pending reason |
| `cubes` | `model_cubes` |
| `connector` | `identity`, `honesty`, `connector` |
| `profile` | `identity`, `honesty`, `profile` |
| `persona` | `persona` |

Current fact sources:

- The clip section reads `uid`, duration, sample rate, engine, generator, and WAV sha from the
  audio-clip envelope.
- The cube section uses the clip's own `cube_ihdr` (`library_r3`), validates source sha and
  coverage, and reports inverse-HDR as `fields.inv_hdr_ppm / 1e6`. Coverage below the `0.95`
  threshold is `partial`, not `real`.
- The layers section orders `signal`, `tonality`, `confidence`, and `quality`; formula references
  are real while unresolved formula text remains pending.
- The spectrogram section reads the `spectrogram_2d` envelope and must agree with the source WAV.
- `model_cubes` contains only real cube rows from `model_cube_rows`.
- Magpie and Pocket TTS carry pending `cube` / `relations` facts with `reason: wav_missing` or
  `reason: no_envelope`, while their omitted faces remain omitted.
- Relation targets that are assets are `ga:` uids. Runtime tile ids belong in `tile_ids`, not in
  `target_uid`.
- Generated video has a pending `video` section with `reason: no_video_asset_kind`; the facts do
  not borrow a path or sha from another asset kind.

The desktop's richer face panels currently render the viewport card, Adaptive Card, transport, and
catalog views as implemented in `apps/desktop/src/render.ts` and `apps/desktop/src/livestrip.ts`.
It consumes core-owned face state and facts remain the MCP/card-export contract; no face fact is
recomputed as local `front` / `back` state.

## 7. Glyph and copy controls

`apps/desktop/src/glyph.ts` gives the glyph three distinct uses:

### 7.1 Card glyph: explicit flip control

`flipGlyph` creates the card's button. It is a direct child of the card, outside the rotating
face DOM, so it remains in the same top-right corner on every face.

For multiple faces, `flipGlyphLabel` produces:

```text
Flip card, face N of M: <next face name>
```

For one face, `setArticleFaceState` sets `aria-disabled="true"` and the label:

```text
Card has 1 face: <name>
```

The click, Enter, and Space handlers stop propagation and invoke the explicit flip callback. An
`aria-disabled` glyph is a no-op. The glyph never writes to the clipboard.

### 7.2 Clip and cube glyphs: passive marks

`glyphMark` renders an identity mark as a non-button with the uid in its accessible label and
title. It is never a copy or flip target.

On a LibraryClip tile, `clipIdentity` in `apps/desktop/src/livestrip.ts` places a passive clip
glyph, the clip uid, and a separate button labelled **`Copy clip uid`** on the `clip` face only.

On the spatial slide, `fillCubeGlyph` in `apps/desktop/src/render.ts` places the passive bound-cube
glyph and a separate button labelled **`Copy cube uid`**. If no cube is bound, the slot is empty:
no stand-in text and no copy button.

`copyUidButton` is the only desktop UID-copy control. Its click stops propagation, so copying does
not also select or flip a tile.

## 8. Attach and `card_status`

`library_harvest` in `crates/gen-audio-mcp/src/control.rs` with `apply: true`:

1. proposes semantic and display names from catalog/sidecar facts;
2. attaches `clip:<clipId>` to the persona's process-local refs;
3. renames the clip display values without decoding or rewriting the WAV;
4. reads the updated profile.

Its direct result reports:

```text
ref, attached, personaId, profile, clipId, semanticName, faceName, source,
decodedAudio: false, openedWav: false, synthesizedSpeech: false
```

It emits one control event:

```json
{
  "op": "card_status",
  "args": {
    "personaId": "alice",
    "tileId": "profile-alice",
    "ref": "clip:lib-kokoro-onnx",
    "refs": ["..."],
    "attached": true,
    "profile": {"personaId":"alice","refs":["..."]}
  },
  "synthesizedSpeech": false
}
```

The producer no longer emits `flipcard`.

The desktop handles both:

- `card_status`: current profile/status event;
- `flipcard`: receive-only compatibility for this release.

Both route to `applyProfileUpdate`, which writes the refs, creates or refreshes the
`.profile-status` live region, and names the face that contains refs. Neither operation changes a
face. Direct handling of the Attach result uses the same function, so the result and bus echo stay
consistent.

The legacy `flipcard` name is accepted only on input during the compatibility window; callers and
tests must not expect the backend to publish it.

## 9. Release and fixture boot

Release boot uses `schemas/examples/viewport.release.json` and
`apps/desktop/public/library/assets.json`. The fixture deck and
`schemas/asset-object/fixtures/assets.dev.json` are loaded only when
`VITE_GEN_AUDIO_FIXTURES=1`.

`apps/desktop/src/env.ts` is the build/test env seam:

- `FIXTURES_BUILD` and `UNDER_VITE` make Vite release conditionals statically foldable;
- `loadShippedDocument` and `loadDevAssets` in `apps/desktop/src/main.ts` put fixture imports
  behind those guards;
- `installFixtureHooks` dynamically loads `apps/desktop/src/fixture-controls.ts`;
- `bindEnv` lets plain Node tests select fixture mode without a custom loader.

The release build must omit:

- the fixture deck chunk;
- the dev asset chunk;
- the fixture-controls chunk;
- fixture controls and scrub hooks;
- dev fixture card bodies and ids.

`apps/desktop/tests/mcp-wire.test.ts` boots the default main wiring against a wall-clock deadline
and requires the release Library deck with no `spec-fixture` or `cube-fixture`.
`apps/desktop/tests/main-env-seam.test.ts` uses `bindEnv` to require the fixture deck and fixture
controls under a separate wall-clock deadline.
`apps/desktop/tests/viewport-source.test.ts` builds both modes and verifies both sides of the
tree-shaking assertion: release has no fixture chunks, while a fixture build emits and imports
`viewport.example`, `assets.dev`, and `fixture-controls`.

## 10. Acceptance tests and current anchors

The following tests pin the C13 behavior:

### Core

- `release_library_views_match_the_release_deck`
- `release_face_lists_follow_the_omission_rule`
- `t17_flip_rejects_omitted_faces_and_names_the_valid_list`
- `t17_headless_actions_round_trip_through_viewport_get`
- `facts_sections_read_the_envelope_and_omit_missing_cubes`
- `card_uid_is_required`

These tests are in `crates/gen-audio-core/src/viewport.rs` and
`crates/gen-audio-core/src/cards.rs`.

### MCP

- `t17_ui_flip_next_face_aliases_and_one_selector`
- `flip_bus_payload_derives_flipped_from_face_index`
- `t17_section_deprecations_warn_once_and_select_the_face`
- `deprecated_section_follows_the_face_that_renders_it`
- `harvest_attaches_a_clip_ref_without_decoding_audio`

These tests are in `crates/gen-audio-mcp/src/control.rs` and
`crates/gen-audio-mcp/src/lib.rs`. The harvest test pins `card_status`, its ref/status fields, and
the absence of an emitted `flipcard`.

### Desktop, schema, and boot

- `apps/desktop/tests/livetile-contract.test.ts`
- `apps/desktop/tests/main-env-seam.test.ts`
- `apps/desktop/tests/mcp-wire.test.ts`
- `apps/desktop/tests/validate.test.ts`
- `apps/desktop/tests/viewport-source.test.ts`

Together they pin:

- real multi-face ids instead of front/back collapse;
- passive glyph and separate `Copy clip uid` / `Copy cube uid` controls;
- glyph-only explicit flip and no body/timer flip;
- `card_status` and receive-only legacy `flipcard` updates without a face change;
- required UIDs in TypeScript and JSON Schema;
- default release boot, fixture boot, and release chunk exclusion;
- wall-clock readiness rather than a fixed number of event-loop turns.

## 11. One-release compatibility summary

| Legacy behavior | Current behavior |
|---|---|
| `flipped` boolean on `ui_flip` requests and flip events | Accepted/emitted as a derived index-zero-vs-other compatibility field; consumers prefer face fields. |
| `ui.flipped` | Remains as a derived read for old clients; not authoritative for multi-face state. |
| `front` / `back` face selectors | Accepted as aliases for positions zero and one. |
| Bare `ui_flip` selector | Still means `back`. |
| Bare or back-plus-`section` | Accepted with the C13 warning and stderr behavior for this release, then rejected. |
| `flipcard` bus op | Accepted by the desktop only; backend publication is `card_status`. |

The source of truth for a live tile is therefore: **required card uid for identity, reducer-owned
`face_id` / `face_index` / `face_count` / `ids` for position, and explicit glyph/MCP Flip for the
only face transition path.**

## 12. AgentVoice tiles (desktop-owned)

AgentVoice flipcards (AgentVoice slide, and the "Audio · AgentVoice" slide on the
Audio tab) are built by the desktop from `/library/agentvoice/assets.json`, not by
the reducer. They use the same face dataset, `hidden`/`inert` and glyph label as
reducer tiles (`setTileFaceState`), with two faces: `voice` (name, appearances,
total time, timeline, transport, spectrogram) and `cube` (the voice's own
inverse-HDR cube, time slice on the tile's audio, appearance list). Card ids are
`av-<set>-<alias>` and `audio-av-<set>-<alias>`.

Not yet in the reducer: `viewport_get` does not list them and `ui_flip` cannot
address them. For tests and screenshots the window exposes
`window.genAudioAgentVoice.flip(tileId, 0|1)` and `.goTo(slideId)`.
