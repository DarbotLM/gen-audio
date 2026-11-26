export const SEMANTIC_MAX_BYTES = 1024 * 1024;
export interface MetricAxis { label: string; unit: string; minimum: number; maximum: number }
export interface MetricLayer extends MetricAxis {
  id: string; metric: string; color: string; position: number;
  scale: "linear" | "log"; outside: "reject" | "clip";
  opacity: number; gain: number; blend: "normal" | "add" | "multiply"; enabled: boolean;
}
export interface MetricRecord {
  id: string; x: number; y: number; metrics: Record<string, number | null>;
  context: Record<string, string>;
}
export interface SemanticCube {
  schema_version: "1.0.0"; kind: "semantic_cube"; id: string; title: string;
  domain: "agent.session" | "storyline" | "camera" | "market.stock" | "market.crypto" | "custom";
  source: { kind: "measured" | "authored" | "example"; label: string };
  axes: { x: MetricAxis; y: MetricAxis }; layers: MetricLayer[]; records: MetricRecord[];
}
export interface MetricPoint { t: number; f: number; z: number; v: number; layer: string; recordId: string; raw: number }
export interface MetricProjection { ok: true; points: MetricPoint[]; records: number; skipped: number; clipped: number }

// Native MCP is the authority for schema and semantic validation. The desktop
// checks the transport and assembles pages before replacing the visible cube.
export type SemanticResult = { document: SemanticCube; projection: MetricProjection };
export type ProjectionCall = (args: Record<string, unknown>) => Promise<unknown>;
const MAX_POINTS = 2048 * 16;
const PAGE_SIZE = 2048;

export function parseSemanticCube(raw: string): unknown {
  if (new TextEncoder().encode(raw).byteLength > SEMANTIC_MAX_BYTES) {
    throw new Error("Semantic cube exceeds 1 MiB");
  }
  return JSON.parse(raw) as unknown;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Invalid semantic projection response");
  }
  return value as Record<string, unknown>;
}

function count(value: unknown, max: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0 || value > max) {
    throw new Error("Invalid semantic projection count");
  }
  return value;
}

function number(value: unknown, normalized = false): number {
  if (typeof value !== "number" || !Number.isFinite(value) || (normalized && (value < 0 || value > 1))) {
    throw new Error("Invalid semantic projection coordinate");
  }
  return value;
}

export async function projectSemanticCube(
  value: unknown, call: ProjectionCall, current: () => boolean = () => true,
): Promise<SemanticResult> {
  const raw = JSON.stringify(value);
  // Leave room for the JSON-RPC envelope in the 1 MiB HTTP transport.
  if (!raw || new TextEncoder().encode(raw).byteLength > SEMANTIC_MAX_BYTES - 1024) {
    throw new Error("Semantic cube exceeds the MCP document budget");
  }
  const document = JSON.parse(raw) as SemanticCube;
  const points: MetricPoint[] = [];
  let total = -1, records = 0, skipped = 0, clipped = 0;
  let offset = 0;
  for (;;) {
    if (!current()) throw new Error("Semantic projection superseded");
    const page = object(await call({ document, offset, limit: PAGE_SIZE }));
    if (!current()) throw new Error("Semantic projection superseded");
    if (page.ok !== true || !Array.isArray(page.points)) throw new Error("Semantic projection refused");
    const pageTotal = count(page.totalPoints, MAX_POINTS);
    const pageRecords = count(page.records, 2048);
    const pageSkipped = count(page.skipped, MAX_POINTS);
    const pageClipped = count(page.clipped, MAX_POINTS);
    if (total < 0) {
      total = pageTotal; records = pageRecords; skipped = pageSkipped; clipped = pageClipped;
      if (document?.kind !== "semantic_cube" || document.schema_version !== "1.0.0"
        || !Array.isArray(document.layers) || !Array.isArray(document.records)
        || document.records.length !== records || document.layers.length < 1
        || document.layers.length > 16 || total < 1 || total + skipped !== records * document.layers.length
        || clipped > total) throw new Error("Invalid validated semantic document");
    } else if (total !== pageTotal || records !== pageRecords || skipped !== pageSkipped || clipped !== pageClipped) {
      throw new Error("Semantic projection changed between pages");
    }
    if (page.points.length !== Math.min(PAGE_SIZE, total - offset)) throw new Error("Incomplete semantic projection page");
    const layers = new Map(document.layers.map(layer => [layer.id, layer.position]));
    const ids = new Set(document.records.map(record => record.id));
    for (const item of page.points) {
      const point = object(item);
      if (typeof point.layer !== "string" || typeof point.recordId !== "string"
        || !layers.has(point.layer) || !ids.has(point.recordId)) throw new Error("Unknown semantic point identity");
      const z = number(point.z, true);
      if (z !== layers.get(point.layer)) throw new Error("Semantic point has the wrong layer plane");
      points.push({ t: number(point.t, true), f: number(point.f, true), z,
        v: number(point.v, true), layer: point.layer, recordId: point.recordId, raw: number(point.raw) });
    }
    offset += page.points.length;
    if (offset === total) {
      if (page.nextOffset !== null) throw new Error("Invalid final semantic projection cursor");
      break;
    }
    if (page.nextOffset !== offset || offset >= MAX_POINTS) throw new Error("Invalid semantic projection cursor");
  }
  return { document, projection: { ok: true, points, records, skipped, clipped } };
}

export function hexRgb(color: string): [number, number, number] {
  if (!/^#[a-f0-9]{6}$/i.test(color)) throw new Error("Layer color must be #RRGGBB");
  return [1, 3, 5].map(offset => Number.parseInt(color.slice(offset, offset + 2), 16) / 255) as [number, number, number];
}

export function axisValue(axis: MetricAxis, fraction: number): string {
  const value = axis.minimum + fraction * (axis.maximum - axis.minimum);
  if (axis.unit === "unix_s") return new Date(value * 1000).toISOString().slice(11, 19);
  return new Intl.NumberFormat("en", { maximumFractionDigits: 3, notation: Math.abs(value) >= 10000 ? "compact" : "standard" }).format(value);
}
