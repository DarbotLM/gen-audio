// Glyph badge: a 2-cell braille SVG drawn from the first 16 bits of an asset uid.
// A card glyph is the explicit flip control. Uid copying always uses a separate
// labelled button; the glyph never copies.
// CSP: no style attributes. Hue comes from the fixed per-kind class (ga-kind-<kind>).

import { glyphBytesFromUid, glyphFromUid, hueClass, isUid, parseUid } from "./asset";
import { surfaceUiError } from "./play-control";

const SVG_NS = "http://www.w3.org/2000/svg";
/** Braille dot k+1 for bit k: [column, row] inside one cell. */
const DOT_POSITIONS: Array<[number, number]> = [
  [0, 0], [0, 1], [0, 2], // dots 1-3
  [1, 0], [1, 1], [1, 2], // dots 4-6
  [0, 3], [1, 3], // dots 7-8
];

export function glyphSvg(uid: string): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 10 9");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add("ga-glyph-svg");
  const bytes = glyphBytesFromUid(uid);
  bytes.forEach((byte, cell) => {
    DOT_POSITIONS.forEach(([column, row], bit) => {
      const dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("cx", String(1.5 + cell * 5 + column * 2));
      dot.setAttribute("cy", String(1.5 + row * 2));
      dot.setAttribute("r", "0.75");
      dot.classList.add("ga-dot", byte & (1 << bit) ? "is-on" : "is-off");
      svg.append(dot);
    });
  });
  return svg;
}

export interface MarkOptions {
  /** Short visible role tag, e.g. "clip" for the bound audio clip on a library tile. */
  role?: string;
}

function makeGlyph<T extends HTMLElement>(node: T, uid: string, role?: string): T {
  const { kind } = parseUid(uid);
  node.className = `ga-glyph ${hueClass(kind)}`;
  node.dataset.uid = uid;
  node.dataset.glyph = glyphFromUid(uid);
  node.append(glyphSvg(uid));
  if (role) {
    const tag = document.createElement("span");
    tag.className = "ga-glyph-role";
    tag.textContent = role;
    node.append(tag);
  }
  return node;
}

/** Passive visual mark. It is deliberately not an interactive copy target. */
export function glyphMark(uid: unknown, options: MarkOptions = {}): HTMLSpanElement | null {
  if (!isUid(uid)) return null;
  const { kind } = parseUid(uid);
  const mark = makeGlyph(document.createElement("span"), uid, options.role);
  mark.classList.add("ga-glyph-mark");
  mark.setAttribute("role", "img");
  mark.setAttribute("aria-label", `${kind.replace(/_/g, " ")} ${uid}`);
  mark.title = uid;
  return mark;
}

/** Card-only flip button. It never writes the clipboard. */
export function flipGlyph(uid: unknown, onFlip: () => void): HTMLButtonElement | null {
  if (!isUid(uid)) return null;
  const button = makeGlyph(document.createElement("button"), uid);
  button.type = "button";
  button.classList.add("flip-glyph");
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    if (button.getAttribute("aria-disabled") !== "true") onFlip();
  });
  button.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    event.stopPropagation();
    if (button.getAttribute("aria-disabled") !== "true") onFlip();
  });
  return button;
}

export function flipGlyphLabel(index: number, faces: readonly string[]): string {
  if (faces.length === 1) return `Card has 1 face: ${faces[0]}`;
  const current = ((index % faces.length) + faces.length) % faces.length;
  return `Flip card, face ${current + 1} of ${faces.length}: ${faces[(current + 1) % faces.length]}`;
}

export interface CopyOptions {
  label: string;
  onCopy?: (uid: string, copied: boolean) => void;
}

/** Explicit labelled uid copy control, kept separate from the card glyph. */
export function copyUidButton(uid: unknown, options: CopyOptions): HTMLButtonElement | null {
  if (!isUid(uid)) return null;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy-uid";
  button.dataset.action = "copy-uid";
  button.dataset.uid = uid;
  button.textContent = options.label;
  button.title = uid;
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    void copyUid(uid).then((copied) => {
      button.classList.toggle("is-copied", copied);
      window.setTimeout(() => button.classList.remove("is-copied"), 1200);
      options.onCopy?.(uid, copied);
    });
  });
  return button;
}

export async function copyUid(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (error) {
    surfaceUiError(error, "copy uid");
    return false;
  }
}
