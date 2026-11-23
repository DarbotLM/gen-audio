// AgentVoice tab + "Audio" rename. The tab strip, slide order and layer map are
// read from the shipped sources; the voice count and per-voice spectrogram/cube
// come from the committed catalog (public/library/agentvoice/assets.json), which
// `python -m gen_audio.cli.agent_voices import` writes from a Yumlog Studio
// agent-voices.json. Nothing here is a hand-written voice list.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  agentVoiceModels,
  appearanceOffset,
  clock,
  pageCount,
  summaryLine,
  type AgentVoiceCatalogDoc,
} from "../src/agentvoice.ts";
import { SLIDE_SCHEMAS, layerForSlide, slideForLayer } from "../src/render.ts";

const repo = (path: string) => fileURLToPath(new URL(`../../../${path}`, import.meta.url));
const CATALOG = repo("apps/desktop/public/library/agentvoice/assets.json");

test("viewport tabs read Voice models, AgentVoice, Audio, Video, Cube", () => {
  const html = readFileSync(repo("apps/desktop/index.html"), "utf8");
  const strip = html.slice(html.indexOf('id="layer-switch"'), html.indexOf("</div>", html.indexOf('id="layer-switch"')));
  const tabs = [...strip.matchAll(/data-layer="([a-z]+)"[^>]*>([^<]+)</g)].map((m) => [m[1], m[2].trim()]);
  assert.deepEqual(tabs, [
    ["models", "Voice models"],
    ["agentvoice", "AgentVoice"],
    ["audio", "Audio"],
    ["video", "Video"],
    ["cube", "Cube"],
  ]);
});

test("no shipped UI source still says Audio clips", () => {
  for (const path of ["apps/desktop/index.html", "apps/desktop/src/render.ts", "apps/desktop/src/main.ts"]) {
    assert.ok(!/Audio clips/i.test(readFileSync(repo(path), "utf8")), `${path} still says Audio clips`);
  }
});

test("slide order puts AgentVoice between the voice models and Audio", () => {
  const ids = SLIDE_SCHEMAS.map((slide) => slide.id);
  const at = (id: string) => ids.indexOf(id);
  assert.ok(at("models") >= 0 && at("agentvoice") > at("profiles") && at("library") > at("agentvoice"));
  assert.equal(at("audio-agentvoice"), at("library") + 1);
  const library = SLIDE_SCHEMAS[at("library")];
  assert.equal(library.title, "Audio");
  assert.equal(library.layer, "audio");
  assert.equal(SLIDE_SCHEMAS[at("agentvoice")].title, "AgentVoice");
  assert.equal(SLIDE_SCHEMAS[at("agentvoice")].slot, "agentvoice");
  assert.equal(SLIDE_SCHEMAS[at("audio-agentvoice")].slot, "audio");
});

test("tab ids resolve to slides; the old clips id still opens Audio", () => {
  assert.equal(slideForLayer("audio"), "library");
  assert.equal(slideForLayer("clips"), "library");
  assert.equal(slideForLayer("agentvoice"), "agentvoice");
  assert.equal(slideForLayer("cube"), "spatial");
  assert.equal(slideForLayer("nope"), "");
  assert.equal(layerForSlide("library"), "audio");
  assert.equal(layerForSlide("audio-agentvoice"), "audio");
  assert.equal(layerForSlide("agentvoice"), "agentvoice");
});

test("clock, summary and paging", () => {
  assert.equal(clock(2979.4), "49:39");
  assert.equal(summaryLine({ appearances: [{ start_s: 0, end_s: 1 }], totalS: 1 }), "1 appearance \u00b7 00:01 total");
  assert.equal(pageCount(31, 6), 6);
  assert.equal(pageCount(0, 6), 1);
});

test("a voice row without an audio_clip stays unavailable; no other asset is borrowed", () => {
  const doc: AgentVoiceCatalogDoc = {
    sources: [{ set: "s", schema: "urn:yumlog:agent-voices:v1", method: "m", recording_duration_s: 10, voice_count: 1 }],
    voices: [
      {
        set: "s",
        alias: "gone",
        legacy_id: "agentvoice:s:gone",
        status: "unavailable",
        reason: "WAV missing at export",
        appearance_count: 1,
        total_s: 2,
        appearances: [{ start_s: 1, end_s: 3 }],
        spectrogram_uid: "ga:spectrogram_2d:aaaaaaaaaaaaaaaaaaaaaaaaaa",
      },
    ],
    assets: [],
  };
  const [model] = agentVoiceModels(doc);
  assert.equal(model.status, "unavailable");
  assert.equal(model.reason, "WAV missing at export");
  assert.equal(model.spectrogram, null);
  assert.equal(model.cube, null);
  assert.equal(appearanceOffset(model, 0), null);
});

test("committed AgentVoice catalog: one tile per source voice; every voice with audio has a spectrogram and a cube", () => {
  assert.ok(existsSync(CATALOG), "public/library/agentvoice/assets.json is missing; run python -m gen_audio.cli.agent_voices import");
  const doc = JSON.parse(readFileSync(CATALOG, "utf8")) as AgentVoiceCatalogDoc;
  const expected = doc.sources.reduce((sum, source) => sum + source.voice_count, 0);
  assert.ok(expected > 0);
  const models = agentVoiceModels(doc);
  assert.equal(models.length, expected, "tile count must equal the Yumlog source voice_count");
  const withAudio = models.filter((model) => model.status === "ok");
  assert.ok(withAudio.length > 0, "no voice has audio");
  for (const model of withAudio) {
    assert.ok(model.spectrogram, `${model.alias} has audio but no spectrogram`);
    assert.ok(model.cube, `${model.alias} has audio but no cube`);
    assert.equal(model.segments.length, model.appearances.length, `${model.alias} segment/appearance mismatch`);
    assert.ok(model.cube.media.some((item) => item.role === "cube_json"));
  }
  for (const model of models.filter((m) => m.status !== "ok")) assert.ok(model.reason.length > 0);
});
