import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { bindEnv } from "../src/env.ts";

bindEnv({ VITE_GEN_AUDIO_FIXTURES: "1" });

type ElementMock = {
  tagName: string;
  id: string;
  className: string;
  textContent: string;
  value: string;
  type: string;
  hidden: boolean;
  disabled: boolean;
  dataset: Record<string, string>;
  style: { setProperty: (name: string, value: string) => void };
  children: ElementMock[];
  parent: ElementMock | null;
  classList: {
    add: (...values: string[]) => void;
    remove: (...values: string[]) => void;
    toggle: (...values: unknown[]) => boolean;
    contains: (value: string) => boolean;
  };
  append: (...nodes: ElementMock[]) => void;
  appendChild: (node: ElementMock) => ElementMock;
  prepend: (...nodes: ElementMock[]) => void;
  replaceChildren: (...nodes: ElementMock[]) => void;
  setAttribute: (key: string, value: string) => void;
  getAttribute: (key: string) => string | null;
  addEventListener: () => void;
  removeEventListener: () => void;
  querySelector: (selector: string) => ElementMock | null;
  querySelectorAll: (selector: string) => ElementMock[];
  closest: (selector: string) => ElementMock | null;
  focus: () => void;
  matches: (selector: string) => boolean;
};

function matches(node: ElementMock, selector: string): boolean {
  const attr = /^\[data-([A-Za-z0-9-]+)(?:="([^"]*)")?\]$/.exec(selector);
  if (attr) {
    const value = node.dataset[attr[1]];
    return attr[2] === undefined ? value !== undefined : value === attr[2];
  }
  if (selector.startsWith("#") && !selector.includes(" ") && !selector.includes(".")) {
    return node.id === selector.slice(1);
  }
  if (selector.startsWith(".")) return node.className.split(/\s+/).includes(selector.slice(1));
  return node.tagName.toLowerCase() === selector.toLowerCase();
}

function walk(root: ElementMock, visit: (node: ElementMock) => void): void {
  visit(root);
  for (const child of root.children) walk(child, visit);
}

function queryAll(root: ElementMock, selector: string): ElementMock[] {
  const parts = selector.trim().split(/\s+/);
  let current = [root];
  for (const part of parts) {
    const next: ElementMock[] = [];
    for (const node of current) {
      walk(node, (candidate) => {
        if (candidate !== node && matches(candidate, part)) next.push(candidate);
      });
    }
    current = next;
  }
  return current;
}

function query(root: ElementMock, selector: string): ElementMock | null {
  let found: ElementMock | null = null;
  walk(root, (node) => {
    if (matches(node, selector)) found ??= node;
  });
  return found;
}

function element(tag = "div"): ElementMock {
  const style: Record<string, string> = {};
  const classes = new Set<string>();
  const syncClasses = () => {
    classes.clear();
    for (const value of (node.className ?? "").split(/\s+/)) {
      if (value) classes.add(value);
    }
  };
  const node = {
    tagName: tag.toUpperCase(),
    id: "",
    className: "",
    textContent: "",
    value: "",
    type: "",
    hidden: false,
    disabled: false,
    dataset: {},
    style: { setProperty(name: string, value: string) { style[name] = value; } },
    children: [] as ElementMock[],
    parent: null as ElementMock | null,
    classList: {
      add(...values: string[]) {
        syncClasses();
        for (const value of values) classes.add(value);
        node.className = [...classes].join(" ");
      },
      remove(...values: string[]) {
        syncClasses();
        for (const value of values) classes.delete(value);
        node.className = [...classes].join(" ");
      },
      toggle(...values: unknown[]) {
        syncClasses();
        const value = String(values[0]);
        const on = values[1] === undefined ? !classes.has(value) : Boolean(values[1]);
        if (on) classes.add(value); else classes.delete(value);
        node.className = [...classes].join(" ");
        return on;
      },
      contains(value: string) { return node.className.split(/\s+/).includes(value); },
    },
    append(...nodes: ElementMock[]) {
      for (const child of nodes) {
        child.parent = node;
        node.children.push(child);
      }
    },
    appendChild(child: ElementMock) { node.append(child); return child; },
    prepend(...nodes: ElementMock[]) {
      for (const child of nodes) child.parent = node;
      node.children.unshift(...nodes);
    },
    replaceChildren(...nodes: ElementMock[]) {
      node.children = [];
      node.append(...nodes);
    },
    setAttribute() {},
    getAttribute() { return null; },
    addEventListener() {},
    removeEventListener() {},
    querySelector(selector: string) { return query(node, selector); },
    querySelectorAll(selector: string) { return queryAll(node, selector); },
    closest(selector: string) {
      let current: ElementMock | null = node;
      while (current) {
        if (matches(current, selector)) return current;
        current = current.parent;
      }
      return null;
    },
    focus() {},
    matches(selector: string) { return matches(node, selector); },
  } as ElementMock;
  return new Proxy(node, {
    get(target, key, receiver) {
      if (Reflect.has(target, key)) return Reflect.get(target, key, receiver);
      return () => receiver;
    },
  });
}

function installDom(): { body: ElementMock; board: ElementMock; toolbar: ElementMock } {
  const body = element("body");
  const html = element("html");
  html.append(body);
  const ids = [
    "board", "empty", "status", "model-drop", "loaded-model", "loaded-model-id", "prompt",
    "source-files", "file-list", "voice", "agents", "add-agent", "duration", "generate-podcast",
    "show-empty", "layer-switch", "floating-playback",
  ];
  const byId = new Map<string, ElementMock>();
  for (const id of ids) {
    const tag = id === "voice" || id === "duration" ? "select" : id === "prompt" ? "textarea" : id === "source-files" ? "input" : "div";
    const node = element(tag);
    node.id = id;
    byId.set(id, node);
    body.append(node);
  }
  const header = element("div");
  header.className = "ga-header-toolbar";
  const toolbar = element("div");
  toolbar.className = "toolbar";
  header.append(toolbar);
  body.append(header);
  const documentMock = {
    documentElement: html,
    body,
    querySelector(selector: string) {
      if (selector === ".ga-header-toolbar .toolbar") return toolbar;
      if (selector.startsWith("#") && !selector.includes(" ")) return byId.get(selector.slice(1)) ?? query(body, selector);
      return query(body, selector);
    },
    querySelectorAll(selector: string) { return queryAll(body, selector); },
    createElement: (tag: string) => element(tag),
    createElementNS: (_namespace: string, tag: string) => element(tag),
    addEventListener() {},
  };
  const windowMock = {
    document: documentMock,
    addEventListener() {},
    requestAnimationFrame(work: () => void) { work(); return 1; },
    cancelAnimationFrame() {},
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    devicePixelRatio: 1,
    location: { href: "http://127.0.0.1/" },
  };
  Object.assign(globalThis, {
    document: documentMock,
    window: windowMock,
    Image: class ImageMock {
      naturalWidth = 0;
      naturalHeight = 0;
      src = "";
      addEventListener() {}
    },
    CSS: { escape: (value: string) => value },
    requestAnimationFrame: windowMock.requestAnimationFrame,
    cancelAnimationFrame() {},
  });
  class EventSourceMock {
    readyState = 2;
    addEventListener() {}
    close() {}
    set onerror(_listener: unknown) {}
  }
  (globalThis as unknown as { EventSource: unknown }).EventSource = EventSourceMock;
  return { body, board: byId.get("board")!, toolbar };
}

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const ASSETS = readFileSync(`${ROOT}apps/desktop/public/library/assets.json`, "utf8");
const dom = installDom();
(globalThis as unknown as { fetch: unknown }).fetch = async (url: string) => {
  if (String(url).includes("/library/assets.json")) {
    return { ok: true, status: 200, json: async () => JSON.parse(ASSETS), text: async () => ASSETS };
  }
  return { ok: false, status: 503, json: async () => ({}), text: async () => "" };
};

await import("../src/main.ts");

async function waitFor(selector: () => boolean): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    if (selector()) return;
    await new Promise((resolve) => setImmediate(resolve));
  }
  const ids = dom.board.querySelectorAll(".card").map((card) => card.dataset.id);
  const descendants: string[] = [];
  walk(dom.board, (node) => {
    if (node.dataset.id || node.className.includes("card")) descendants.push(`${node.tagName}:${node.className}:${node.dataset.id ?? ""}`);
  });
  const status = dom.body.querySelector("#status")?.textContent ?? "";
  assert.fail(`main.ts did not reach the fixture boot state before the wall-clock deadline; cards=${JSON.stringify(ids)} status=${JSON.stringify(status)} children=${dom.board.children.length} descendants=${JSON.stringify(descendants)}`);
}

test("bindEnv=1 boots the fixture deck and controls, never the release-only default", async () => {
  await waitFor(() => Boolean(dom.board.querySelectorAll(".card").some((card) => card.dataset.id === "spec-fixture")));
  const ids = new Set(dom.board.querySelectorAll(".card").map((card) => card.dataset.id));
  assert.ok(ids.has("cube-fixture"));
  assert.ok(ids.has("cast-sample"));
  assert.ok(dom.toolbar.querySelector("#show-example"), "fixture controls load through the env seam");
  assert.equal(typeof (window as unknown as { __genAudioScrub?: unknown }).__genAudioScrub, "function");
});
