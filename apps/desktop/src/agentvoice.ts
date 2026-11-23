// AgentVoice livetiles: Yumlog Studio speaker voices, imported into the asset
// object model by `python -m gen_audio.cli.agent_voices import` and served as
// /library/agentvoice/assets.json.
//
// One flipcard per source voice. Front: name, appearance count, total time,
// the appearance timeline over the source recording, play/scrub (the shared
// transport) and the voice's own 2D spectrogram strip. Back: the voice's own
// inverse-HDR cube (same cube JSON pipeline and layer colors as the Cube tab,
// time slice following this tile's audio) plus the appearance list. A voice
// without a WAV says so; nothing is drawn in its place, and no other voice's
// spectrogram or cube is borrowed.

import { checkMediaPath, isUid } from "./asset";
import { LAYER_COLORS, parseCubeDoc, type CubePoint } from "./cubeview";
import { copyUidButton, flipGlyph, glyphMark } from "./glyph";
import { mediaUrl, type AssetEnvelope } from "./library-assets";
import { emptyStrip, spectrogramStrip } from "./livestrip";
import { playClip, renderTransport, seekClip } from "./playback";
import { setTileFaceState } from "./render";

export const AGENTVOICE_CATALOG_URL = "/library/agentvoice/assets.json";
export const AGENTVOICE_PAGE_SIZE = 6;
const APPEARANCE_PAGE = 6;

export interface Appearance {
  start_s: number;
  end_s: number;
}

export interface AgentVoiceRow {
  set: string;
  alias: string;
  speaker_id?: string;
  legacy_id: string;
  status: string;
  reason?: string;
  appearance_count: number;
  total_s: number;
  appearances: Appearance[];
  clip_uid?: string;
  spectrogram_uid?: string;
  cube_uid?: string;
  spectrogram_reason?: string;
}

export interface AgentVoiceSource {
  set: string;
  schema: string;
  method: string;
  recording_duration_s: number;
  voice_count: number;
}

export interface AgentVoiceCatalogDoc {
  sources: AgentVoiceSource[];
  voices: AgentVoiceRow[];
  assets: unknown[];
}

export interface AgentVoiceModel {
  key: string;
  set: string;
  alias: string;
  status: "ok" | "unavailable";
  reason: string;
  appearances: Appearance[];
  /** Appearance spans inside the combined-excerpt WAV (body.segments), same order. */
  segments: Appearance[];
  totalS: number;
  recordingS: number;
  clip: AssetEnvelope | null;
  spectrogram: AssetEnvelope | null;
  cube: AssetEnvelope | null;
}

const num = (value: unknown, fallback = 0): number => (typeof value === "number" && Number.isFinite(value) ? value : fallback);

function spans(value: unknown): Appearance[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => ({ start_s: num((item as Appearance)?.start_s, NaN), end_s: num((item as Appearance)?.end_s, NaN) }))
    .filter((item) => Number.isFinite(item.start_s) && Number.isFinite(item.end_s) && item.end_s > item.start_s);
}

function safeAsset(value: unknown): value is AssetEnvelope {
  const asset = value as Partial<AssetEnvelope> | null;
  if (!asset || typeof asset !== "object" || !isUid(asset.uid) || typeof asset.kind !== "string") return false;
  if (!asset.uid.startsWith(`ga:${asset.kind}:`) || !Array.isArray(asset.media) || !Array.isArray(asset.src)) return false;
  try {
    asset.media.forEach((item) => checkMediaPath(item.path));
  } catch {
    return false;
  }
  return asset.src.every((uid) => isUid(uid));
}

/** One model per catalog voice row, largest total time first (Yumlog's order). Pure; no DOM. */
export function agentVoiceModels(doc: AgentVoiceCatalogDoc | null): AgentVoiceModel[] {
  if (!doc || !Array.isArray(doc.voices)) return [];
  const assets = (Array.isArray(doc.assets) ? doc.assets : []).filter(safeAsset);
  const byUid = new Map(assets.map((asset) => [asset.uid, asset]));
  const recording = new Map((doc.sources ?? []).map((source) => [source.set, num(source.recording_duration_s)]));
  const pick = (uid: unknown, kind: string): AssetEnvelope | null => {
    const asset = typeof uid === "string" ? byUid.get(uid) : undefined;
    return asset && asset.kind === kind ? asset : null;
  };
  return doc.voices
    .map((row): AgentVoiceModel => {
      const clip = row.status === "ok" ? pick(row.clip_uid, "audio_clip") : null;
      const derived = (uid: unknown, kind: string) => {
        const asset = clip ? pick(uid, kind) : null;
        return asset && asset.src.length === 1 && asset.src[0] === clip?.uid ? asset : null;
      };
      return {
        key: `${row.set}-${row.alias}`,
        set: row.set,
        alias: row.alias,
        status: clip ? "ok" : "unavailable",
        reason: clip ? "" : row.reason ?? "No audio_clip asset for this voice.",
        appearances: spans(row.appearances),
        segments: spans(clip?.body?.segments),
        totalS: num(row.total_s),
        recordingS: recording.get(row.set) ?? 0,
        clip,
        spectrogram: derived(row.spectrogram_uid, "spectrogram_2d"),
        cube: derived(row.cube_uid, "cube_ihdr"),
      };
    })
    .sort((a, b) => b.totalS - a.totalS || a.alias.localeCompare(b.alias));
}

export function pageCount(total: number, size: number): number {
  return Math.max(1, Math.ceil(total / Math.max(1, size)));
}

/** Seconds in the combined-excerpt WAV where appearance `index` starts. */
export function appearanceOffset(model: AgentVoiceModel, index: number): number | null {
  const segment = model.segments[index];
  return segment ? segment.start_s : null;
}

export function clock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
  const whole = Math.floor(seconds);
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}

export function summaryLine(model: Pick<AgentVoiceModel, "appearances" | "totalS">): string {
  const n = model.appearances.length;
  return `${n} appearance${n === 1 ? "" : "s"} \u00b7 ${clock(model.totalS)} total`;
}

let pending: Promise<AgentVoiceCatalogDoc | null> | null = null;

export function loadAgentVoiceCatalog(): Promise<AgentVoiceCatalogDoc | null> {
  pending ??= fetch(AGENTVOICE_CATALOG_URL)
    .then((response) => (response.ok ? (response.json() as Promise<AgentVoiceCatalogDoc>) : null))
    .catch((error: unknown) => {
      pending = null;
      throw error;
    });
  return pending;
}

// ------------------------------------------------------------------ DOM

export type Placement = "agentvoice" | "audio";

export interface AgentVoiceOptions {
  onCopy?: (uid: string, copied: boolean) => void;
  /** Bind the voice's cube to the Cube tab (the WebGL viewport) with this tile's audio as its clock. */
  openCube?: (tileId: string, url: string, clipUid: string, label: string) => void;
}

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text = ""): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function tileId(model: AgentVoiceModel, placement: Placement): string {
  return `${placement === "audio" ? "audio-" : ""}av-${model.key}`;
}

function timeline(model: AgentVoiceModel): HTMLElement {
  const bar = el("div", "av-timeline");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", `${model.alias}: ${summaryLine(model)} across a ${clock(model.recordingS)} recording`);
  if (model.recordingS > 0) {
    for (const turn of model.appearances) {
      const mark = el("i");
      mark.style.left = `${(100 * turn.start_s) / model.recordingS}%`;
      mark.style.width = `${Math.max(0.35, (100 * (turn.end_s - turn.start_s)) / model.recordingS)}%`;
      bar.append(mark);
    }
  }
  return bar;
}

interface MiniCube {
  root: HTMLElement;
  start(): void;
}

/** The voice's cube drawn from its own cube JSON, with the time slice on this tile's audio clock. */
function miniCube(model: AgentVoiceModel, audio: HTMLAudioElement | null, id: string, options: AgentVoiceOptions): MiniCube {
  const root = el("div", "av-cube");
  const url = model.cube ? mediaUrl(model.cube, "cube_json") : null;
  if (!model.cube || !url || !model.clip) {
    root.dataset.state = "none";
    root.append(el("p", "av-cube-label", `No cube \u2014 ${model.status === "ok" ? "no cube was computed for this voice." : "this voice has no WAV."}`));
    return { root, start() {} };
  }
  const canvas = el("canvas", "av-cube-canvas");
  canvas.setAttribute("role", "img");
  const label = el("p", "av-cube-label", "Inverse-HDR cube \u00b7 loading");
  const legend = el("ul", "av-cube-legend");
  for (const name of ["signal", "tonality", "confidence", "quality"]) {
    const item = el("li", "", name);
    item.dataset.layer = name;
    legend.append(item);
  }
  const open = el("button", "av-open-cube", "Open in Cube tab");
  open.type = "button";
  open.addEventListener("click", (event) => {
    event.stopPropagation();
    options.openCube?.(id, url, model.clip?.uid ?? "", model.alias);
  });
  root.dataset.state = "loading";
  root.dataset.uid = model.cube.uid;
  root.append(canvas, legend, label, open);
  let points: CubePoint[] = [];
  let coversS = 0;
  let timeBins = 0;
  let loaded: Promise<void> | null = null;
  let frame = 0;
  const seconds = () => (audio && Number.isFinite(audio.currentTime) ? audio.currentTime : 0);
  const draw = () => {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round((canvas.clientWidth || 240) * ratio));
    const height = Math.max(1, Math.round((canvas.clientHeight || 110) * ratio));
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    ctx.fillStyle = "#06090f";
    ctx.fillRect(0, 0, width, height);
    const depthX = width * 0.16;
    const depthY = height * 0.2;
    const spanX = width - depthX - 12 * ratio;
    const spanY = height - depthY - 10 * ratio;
    const project = (t: number, f: number, z: number) => [6 * ratio + t * spanX + z * depthX, height - 6 * ratio - f * spanY - z * depthY];
    const now = seconds();
    const slice = coversS > 0 ? Math.min(1, now / coversS) : 0;
    const near = timeBins > 0 ? 1.5 / timeBins : 0.01;
    const dot = Math.max(1.5, 1.6 * ratio);
    // Back layers first, as in the cube PNG.
    for (const point of [...points].sort((a, b) => b.z - a.z)) {
      const [x, y] = project(point.t, point.f, point.z);
      const [r, g, b] = LAYER_COLORS[point.layer] ?? [1, 1, 1];
      const lit = Math.abs(point.t - slice) <= near;
      const alpha = lit ? 1 : 0.25 + 0.55 * Math.max(0, Math.min(1, point.v));
      ctx.fillStyle = `rgba(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)},${alpha.toFixed(3)})`;
      ctx.fillRect(x - dot / 2, y - dot / 2, lit ? dot * 1.8 : dot, lit ? dot * 1.8 : dot);
    }
    // The time-slice plane through all four layers at the playhead.
    const [x0, y0] = project(slice, 0, 0);
    const [x1, y1] = project(slice, 1, 0);
    const [x2, y2] = project(slice, 1, 1);
    const [x3, y3] = project(slice, 0, 1);
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.lineTo(x3, y3);
    ctx.closePath();
    ctx.fillStyle = "rgba(244,251,255,0.10)";
    ctx.strokeStyle = now > coversS + 0.001 && coversS > 0 ? "#ff8a3d" : "rgba(244,251,255,0.75)";
    ctx.lineWidth = Math.max(1, ratio);
    ctx.fill();
    ctx.stroke();
    root.dataset.slice = slice.toFixed(4);
    root.dataset.seconds = now.toFixed(2);
  };
  const loop = () => {
    draw();
    frame = audio && !audio.paused && !audio.ended ? window.requestAnimationFrame(loop) : 0;
  };
  const kick = () => {
    if (!frame) frame = window.requestAnimationFrame(loop);
  };
  const load = () => {
    loaded ??= fetch(url)
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`cube JSON ${response.status}`))))
      .then((data) => {
        const parsed = parseCubeDoc(url, data);
        if (typeof parsed === "string") throw new Error(parsed);
        points = parsed.points;
        coversS = parsed.meta.coversS ?? 0;
        timeBins = parsed.meta.timeBins;
        root.dataset.state = "ready";
        const inv = parsed.meta.invHdr === null ? "?" : parsed.meta.invHdr.toFixed(4);
        label.textContent = `Inverse-HDR cube rev ${parsed.meta.revision ?? "?"} \u00b7 inv_hdr ${inv} \u00b7 ${parsed.meta.points} points \u00b7 ${parsed.meta.freqBins}\u00d7${parsed.meta.timeBins} bins \u00b7 slice follows this tile's audio`;
        canvas.setAttribute("aria-label", label.textContent);
        draw();
      })
      .catch((error: unknown) => {
        root.dataset.state = "missing";
        label.textContent = `No cube \u2014 the cube JSON did not load (${String(error instanceof Error ? error.message : error)}).`;
      });
  };
  if (audio) for (const name of ["play", "playing", "seeked", "timeupdate"]) audio.addEventListener(name, kick);
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => draw()).observe(canvas);
  return { root, start: load };
}

function appearanceList(model: AgentVoiceModel, id: string): HTMLElement {
  const wrap = el("div", "av-appearances");
  let page = 0;
  const pages = pageCount(model.appearances.length, APPEARANCE_PAGE);
  const list = el("ol", "av-appearance-list");
  const pager = el("div", "av-pager");
  const prev = el("button", "", "\u2039");
  const next = el("button", "", "\u203a");
  const where = el("span", "av-page");
  prev.type = next.type = "button";
  prev.setAttribute("aria-label", `Previous appearances of ${model.alias}`);
  next.setAttribute("aria-label", `Next appearances of ${model.alias}`);
  const show = () => {
    list.replaceChildren();
    list.start = page * APPEARANCE_PAGE + 1;
    model.appearances.slice(page * APPEARANCE_PAGE, (page + 1) * APPEARANCE_PAGE).forEach((turn, offset) => {
      const index = page * APPEARANCE_PAGE + offset;
      const item = el("li");
      const go = el("button", "av-appearance", `${clock(turn.start_s)}\u2013${clock(turn.end_s)}`);
      go.type = "button";
      const at = appearanceOffset(model, index);
      go.disabled = at === null || model.status !== "ok";
      go.title = at === null ? "No WAV for this voice" : `Recording ${clock(turn.start_s)}; plays from ${clock(at)} in the combined excerpts`;
      go.addEventListener("click", (event) => {
        event.stopPropagation();
        if (at === null) return;
        void seekClip(id, at).then(() => playClip(id, "user"));
      });
      item.append(go);
      list.append(item);
    });
    where.textContent = `${page + 1} / ${pages}`;
    prev.disabled = page === 0;
    next.disabled = page + 1 >= pages;
  };
  prev.addEventListener("click", (event) => {
    event.stopPropagation();
    page = Math.max(0, page - 1);
    show();
  });
  next.addEventListener("click", (event) => {
    event.stopPropagation();
    page = Math.min(pages - 1, page + 1);
    show();
  });
  pager.append(prev, where, next);
  wrap.append(el("p", "av-hint", "Appearances in the source recording"), list);
  if (pages > 1) wrap.append(pager);
  show();
  return wrap;
}

/** One AgentVoice flipcard livetile. Faces: voice (front), cube (back: cube + appearances). */
export function renderAgentVoiceTile(model: AgentVoiceModel, placement: Placement, options: AgentVoiceOptions = {}): HTMLElement {
  const id = tileId(model, placement);
  const article = el("article", "card livetile agentvoice-tile");
  article.tabIndex = -1;
  article.dataset.id = id;
  article.dataset.kind = "AgentVoice";
  article.dataset.placement = placement;
  article.dataset.voice = model.alias;
  article.dataset.status = model.status;
  if (model.clip) article.dataset.clipUid = model.clip.uid;
  if (model.spectrogram) article.dataset.spectrogramUid = model.spectrogram.uid;
  if (model.cube) article.dataset.cubeUid = model.cube.uid;
  article.setAttribute("aria-label", `AgentVoice: ${model.alias}`);

  const flip = el("div", "flip");
  const front = el("div", "face front");
  front.dataset.faceId = "voice";
  const row = el("div", "live-row");
  row.append(el("div", "kind", "AgentVoice"));
  const badge = el("span", model.status === "ok" ? "live-mark" : "live-mark is-unavailable", model.status === "ok" ? "Real WAV" : "Unavailable");
  row.append(badge);
  front.append(row, el("h2", "", model.alias));
  if (model.clip) {
    const identity = el("div", "clip-identity");
    const mark = glyphMark(model.clip.uid, { role: "clip" });
    const code = el("code", "clip-uid", model.clip.uid);
    const copy = copyUidButton(model.clip.uid, { label: "Copy uid", onCopy: options.onCopy });
    if (mark) identity.append(mark);
    identity.append(code);
    if (copy) identity.append(copy);
    front.append(identity);
  }
  front.append(el("p", "av-summary", summaryLine(model)), timeline(model));
  let audio: HTMLAudioElement | null = null;
  if (model.clip) {
    const wavUrl = mediaUrl(model.clip, "wav") ?? undefined;
    const transport = renderTransport(id, wavUrl, num(model.clip.body?.duration_s));
    audio = transport.querySelector("audio");
    if (audio) audio.preload = "none";
    audio?.addEventListener("error", () => {
      badge.textContent = "WAV missing";
      badge.classList.add("is-unavailable");
      article.dataset.status = "wav-missing";
    });
    front.append(el("p", "av-hint", "Combined source excerpts"), transport);
    front.append(model.spectrogram ? spectrogramStrip(model.spectrogram, model.clip, audio).root : emptyStrip("no spectrogram was computed for this voice."));
  } else {
    front.append(el("p", "av-reason", model.reason), el("p", "av-hint", "No WAV \u2014 honest empty tile."));
    front.append(emptyStrip("this voice has no WAV, so there is nothing to analyze."));
  }

  const back = el("div", "face back");
  back.dataset.faceId = "cube";
  back.append(el("div", "kind", `AgentVoice \u00b7 ${model.alias}`));
  const cube = miniCube(model, audio, id, options);
  back.append(cube.root, appearanceList(model, id));
  flip.append(front, back);
  article.append(flip);

  const faces = { ids: ["voice", "cube"], names: ["Voice", "Cube + appearances"] };
  const glyph = model.clip
    ? flipGlyph(model.clip.uid, () => {
        const next = article.dataset.faceIndex === "1" ? 0 : 1;
        if (next === 1) cube.start();
        setTileFaceState(article, { face_index: next, ...faces });
      })
    : null;
  const button = el("button", "av-flip", "Flip to cube + appearances");
  button.type = "button";
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const next = article.dataset.faceIndex === "1" ? 0 : 1;
    if (next === 1) cube.start();
    setTileFaceState(article, { face_index: next, ...faces });
  });
  const backButton = el("button", "av-flip", "Flip to voice");
  backButton.type = "button";
  backButton.addEventListener("click", (event) => {
    event.stopPropagation();
    setTileFaceState(article, { face_index: 0, ...faces });
  });
  front.append(button);
  back.append(backButton);
  if (glyph) article.append(glyph);
  setTileFaceState(article, { face_index: 0, ...faces });
  return article;
}

/** Flip an AgentVoice tile from outside (dev hooks, screenshots). */
export function flipAgentVoiceTile(board: HTMLElement, id: string, faceIndex: 0 | 1): boolean {
  const tile = board.querySelector<HTMLElement>(`.agentvoice-tile[data-id="${CSS.escape(id)}"]`);
  const button = tile?.querySelector<HTMLButtonElement>(faceIndex === 1 ? ".face.front .av-flip" : ".face.back .av-flip");
  if (!tile || !button) return false;
  if ((tile.dataset.faceIndex === "1") !== (faceIndex === 1)) button.click();
  return true;
}

/** Fill the AgentVoice slide and the Audio tab's AgentVoice slide, one page of tiles at a time. */
export function fillAgentVoiceSlides(board: HTMLElement, doc: AgentVoiceCatalogDoc | null, options: AgentVoiceOptions = {}): number {
  const models = agentVoiceModels(doc);
  board.querySelectorAll<HTMLElement>("[data-agentvoice-slot]").forEach((slot) => {
    const placement = (slot.dataset.agentvoiceSlot === "audio" ? "audio" : "agentvoice") as Placement;
    const section = slot.closest<HTMLElement>(".slide");
    const status = section?.querySelector<HTMLElement>(".slide-banner p");
    slot.replaceChildren();
    if (!doc) {
      slot.append(el("p", "av-empty", "AgentVoice catalog did not load (/library/agentvoice/assets.json)."));
      return;
    }
    if (models.length === 0) {
      slot.append(el("p", "av-empty", "No AgentVoice set imported yet. Run python -m gen_audio.cli.agent_voices import."));
      return;
    }
    const tiles = models.map((model) => renderAgentVoiceTile(model, placement, options));
    const pages = pageCount(tiles.length, AGENTVOICE_PAGE_SIZE);
    let page = 0;
    const withAudio = models.filter((model) => model.status === "ok").length;
    const pager = el("span", "av-slide-pager");
    const prev = el("button", "", "\u2039 Prev");
    const next = el("button", "", "Next \u203a");
    const where = el("span", "av-page");
    prev.type = next.type = "button";
    const show = () => {
      tiles.forEach((tile, index) => {
        tile.hidden = Math.floor(index / AGENTVOICE_PAGE_SIZE) !== page;
      });
      where.textContent = `Page ${page + 1} of ${pages}`;
      prev.disabled = page === 0;
      next.disabled = page + 1 >= pages;
      slot.dataset.page = String(page);
    };
    prev.addEventListener("click", () => {
      page = Math.max(0, page - 1);
      show();
    });
    next.addEventListener("click", () => {
      page = Math.min(pages - 1, page + 1);
      show();
    });
    pager.append(prev, where, next);
    const sets = [...new Set(models.map((model) => model.set))].join(", ");
    if (status) status.textContent = `${models.length} Yumlog Studio voices (${sets}) \u00b7 ${withAudio} with real WAV, spectrogram and cube`;
    section?.querySelector(".slide-banner")?.append(pager);
    slot.append(...tiles);
    slot.dataset.count = String(tiles.length);
    show();
  });
  return models.length;
}
