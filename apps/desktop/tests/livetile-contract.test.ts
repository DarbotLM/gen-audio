import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { copyUidButton, flipGlyph, flipGlyphLabel } from "../src/glyph.ts";
import { applyCardOp, facesForCard } from "../src/render.ts";

class Classes {
  private values = new Set<string>();
  add(...values: string[]) { for (const value of values) this.values.add(value); }
  remove(...values: string[]) { for (const value of values) this.values.delete(value); }
  contains(value: string) { return this.values.has(value); }
  toggle(value: string, force?: boolean) {
    const on = force ?? !this.values.has(value);
    if (on) this.values.add(value); else this.values.delete(value);
    return on;
  }
}

class Node {
  dataset: Record<string, string> = {};
  attributes = new Map<string, string>();
  children: Node[] = [];
  classList = new Classes();
  className = "";
  textContent = "";
  title = "";
  type = "";
  hidden = false;
  parent: Node | null = null;
  listeners = new Map<string, Array<(event: Record<string, unknown>) => unknown>>();
  selector: ((query: string) => Node | null) | null = null;
  query: ((query: string) => Node[]) | null = null;
  closestFace: Node | null = null;

  append(...nodes: Node[]) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this);
  }
  setAttribute(name: string, value: string) { this.attributes.set(name, value); }
  getAttribute(name: string) { return this.attributes.get(name) ?? null; }
  removeAttribute(name: string) { this.attributes.delete(name); }
  addEventListener(name: string, listener: (event: Record<string, unknown>) => unknown) {
    const list = this.listeners.get(name) ?? [];
    list.push(listener);
    this.listeners.set(name, list);
  }
  querySelector(query: string): Node | null { return this.selector?.(query) ?? null; }
  querySelectorAll(query: string): Node[] { return this.query?.(query) ?? []; }
  closest(query: string): Node | null { return query === ".face" ? this.closestFace : null; }
  dispatch(name: string) {
    const event = {
      stopPropagation() {},
      preventDefault() {},
      key: name === "keydown" ? "Enter" : undefined,
    };
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }
}

const copied: string[] = [];
const globals = globalThis as unknown as Record<string, unknown>;
const documentMock = { createElement: () => new Node(), createElementNS: () => new Node() };
globals.document = documentMock;
globals.window = { document: documentMock, setTimeout: (work: () => void) => (work(), 1) };
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: { clipboard: { writeText: async (uid: string) => { copied.push(uid); } } },
});
globals.CSS = { escape: (value: string) => value };

const repo = (path: string) => fileURLToPath(new URL(`../../../${path}`, import.meta.url));
const release = JSON.parse(readFileSync(repo("schemas/examples/viewport.release.json"), "utf8"));

test("current release cards advertise real multi-face ids, not a front/back boolean collapse", () => {
  const card = (id: string) => release.cards.find((candidate: { id: string }) => candidate.id === id);
  const ids = (id: string) => facesForCard(card(id)).map((face) => face.id);
  assert.deepEqual(ids("lib-misaki-kokoro"), ["clip", "cube", "layers", "spectrogram", "relations"]);
  assert.deepEqual(ids("lib-magpie"), ["clip", "relations"]);
  assert.deepEqual(ids("engine-kokoro-dayour"), ["model", "cubes", "relations"]);
  assert.deepEqual(ids("engine-misaki"), ["model", "relations"]);
  assert.deepEqual(ids("engine-pocket"), ["model"]);
  assert.deepEqual(ids("conn-mcp"), ["connector"]);
  assert.deepEqual(ids("profile-anton"), ["profile", "persona", "relations"]);
  assert.equal(flipGlyphLabel(3, ["Clip", "Cube", "Layers", "Spectrogram", "Relations"]), "Flip card, face 4 of 5: Relations");
});

test("the glyph is a flip control and never copies; Copy uid is a separate labelled control", async () => {
  copied.length = 0;
  let flips = 0;
  const uid = "ga:card:wgi2proc543zzqzfa2ydlfbnl4";
  const glyph = flipGlyph(uid, () => { flips += 1; }) as unknown as Node;
  assert.ok(glyph);
  glyph.dispatch("click");
  assert.equal(flips, 1);
  assert.deepEqual(copied, []);

  const copy = copyUidButton(uid, { label: "Copy cube uid" }) as unknown as Node;
  assert.ok(copy);
  assert.equal(copy.textContent, "Copy cube uid");
  copy.dispatch("click");
  await Promise.resolve();
  assert.deepEqual(copied, [uid]);
  assert.equal(flips, 1, "the copy control did not flip");
});

function tile(id: string, ids: string[], names: string[]): Node {
  const node = new Node();
  node.dataset.id = id;
  node.dataset.faceIds = ids.join(" ");
  node.dataset.faces = ids.join(" ");
  node.dataset.faceNames = names.join("|");
  node.dataset.faceId = ids[0];
  node.dataset.face = ids[0];
  node.dataset.faceIndex = "0";
  node.dataset.faceCount = String(ids.length);
  const faces = ids.map((faceId) => {
    const face = new Node();
    face.dataset.faceId = faceId;
    face.hidden = faceId !== ids[0];
    return face;
  });
  node.query = (selector) => selector === ":scope > .flip > .face" ? faces : [];
  return node;
}

test("card_status and legacy flipcard update refs without changing either tile's face", () => {
  const clip = tile("lib-misaki-kokoro", ["clip", "cube", "layers", "spectrogram", "relations"], ["Clip", "Cube", "Layers", "Spectrogram", "Relations"]);
  const profile = tile("profile-anton", ["profile", "persona", "relations"], ["Profile", "Persona", "Relations"]);
  const personaFace = new Node();
  personaFace.dataset.faceId = "persona";
  const refs = new Node();
  refs.closestFace = personaFace;
  profile.selector = (selector) =>
    selector === '[data-field="refs"]'
      ? refs
      : selector === ":scope > .profile-status"
        ? profile.children.find((child) => child.className === "profile-status") ?? null
        : profile.querySelectorAll(selector)[0] ?? null;
  const board = {
    querySelector(selector: string) {
      if (selector.includes('data-id="lib-misaki-kokoro"')) return clip;
      if (selector.includes('data-id="profile-anton"')) return profile;
      return null;
    },
  } as unknown as HTMLElement;
  const update = { personaId: "anton", refs: ["persona:anton", "clip:lib-misaki-kokoro"] };

  assert.equal(applyCardOp(board, "flip", {
    tileId: "lib-misaki-kokoro",
    face_id: "relations",
    face_index: 4,
    face_count: 5,
    ids: ["clip", "cube", "layers", "spectrogram", "relations"],
    profile: update,
  }), true);
  assert.equal(clip.dataset.faceId, "relations", "the named tile moved");
  assert.equal(profile.dataset.faceId, "profile", "the profile payload did not flip its own tile");
  assert.match(refs.textContent, /clip:lib-misaki-kokoro/);

  for (const op of ["card_status", "flipcard"]) {
    assert.equal(applyCardOp(board, op, { profile: update }), true);
    assert.equal(clip.dataset.faceId, "relations");
    assert.equal(profile.dataset.faceId, "profile");
  }
  const badge = profile.children.find((child) => child.className === "profile-status");
  assert.match(badge?.textContent ?? "", /persona face/);
});
