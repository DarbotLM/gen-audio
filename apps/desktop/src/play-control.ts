// Play origin on the control bus (Optimus ruling on PR #5, C1). DOM-free so
// node:test can import it. No focus or rebind decision lives in TS: the Rust
// viewport reducer (PR #4) owns that.

/**
 * Who started playback: "user" (a Play click, or a control-bus play that is
 * not marked auto), "resume" (the floating bar), "auto" (autoplay and any
 * other programmatic start). In TS the origin is informational only.
 */
export type PlayOrigin = "user" | "resume" | "auto";

/** The control-bus request a UI Play click posts (MCP `ui_playback`, origin "user"). */
export function userPlayControl(tileId: string): {
  name: "ui_playback";
  args: { tileId: string; action: "play"; origin: "user" };
} {
  return { name: "ui_playback", args: { tileId, action: "play", origin: "user" } };
}

/** Origin of a control-bus `playback` event: "auto" only when it says so, else "user" (the bus default). */
export function controlPlayOrigin(args: Record<string, unknown>): PlayOrigin {
  return args.origin === "auto" ? "auto" : "user";
}

/** The JSON-RPC body the window POSTs to the loopback MCP for a tool call. */
export function mcpRequestBody(name: string, args: Record<string, unknown>): {
  jsonrpc: "2.0";
  id: 1;
  method: "tools/call";
  params: { name: string; arguments: Record<string, unknown> };
} {
  return { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } };
}

/**
 * Window->MCP failures, counted by `tool:status` (`http_415`, `rpc_-32602`)
 * or `tool:<error class>` (`TypeError` is what a CORS or network failure
 * throws). A failed call stays non-fatal but is never silent: the first
 * failure per key warns once, and every failure is counted.
 */
export class McpFailureCounter {
  private readonly counts = new Map<string, number>();
  private readonly warned = new Set<string>();

  constructor(
    private readonly warn: (message: string) => void = (message) => console.warn(message),
    private readonly onChange: (snapshot: Record<string, number>) => void = () => {},
  ) {}

  note(tool: string, kind: string): number {
    const key = `${tool}:${kind}`;
    const count = (this.counts.get(key) ?? 0) + 1;
    this.counts.set(key, count);
    if (!this.warned.has(key)) {
      this.warned.add(key);
      this.warn(`gen-audio: MCP ${tool} failed (${kind}); later failures of this kind are counted, not logged`);
    }
    this.onChange(this.snapshot());
    return count;
  }

  snapshot(): Record<string, number> {
    return Object.fromEntries(this.counts);
  }

  total(): number {
    let total = 0;
    for (const count of this.counts.values()) total += count;
    return total;
  }
}

/**
 * POST one tool call to the loopback MCP. Returns the JSON-RPC payload, or
 * null when the request fails (non-2xx, network/CORS error, bad JSON); each
 * failure, and each JSON-RPC error payload, is counted in `failures`.
 */
export async function postMcp(
  fetchImpl: typeof fetch,
  url: string,
  name: string,
  args: Record<string, unknown>,
  failures: McpFailureCounter,
  token: string,
): Promise<unknown | null> {
  if (!validMcpCredential(token)) {
    failures.note(name, "credential");
    return null;
  }
  try {
    const headers: Record<string, string> = {
      "content-type": "application/json",
      authorization: `Bearer ${token}`,
    };
    const response = await fetchImpl(url, {
      method: "POST",
      headers,
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
      // The exact body is the desktop half of the UI/agent contract
      // (schemas/examples/control/*.json, tested from both sides).
      body: JSON.stringify(mcpRequestBody(name, args)),
    });
    if (!response.ok) {
      failures.note(name, `http_${response.status}`);
      return null;
    }
    const payload: unknown = await response.json();
    const code = (payload as { error?: { code?: unknown } } | null)?.error?.code;
    if (code !== undefined) failures.note(name, `rpc_${String(code)}`);
    return payload;
  } catch (error) {
    failures.note(name, error instanceof Error ? error.name : "error");
    return null;
  }
}

export interface McpStatusReport {
  addr?: string;
  token?: string | null;
  handshake_ok?: boolean;
}

function validMcpCredential(token: string | null | undefined): token is string {
  return typeof token === "string" && /^[a-fA-F0-9]{64}$/.test(token);
}

function validMcpAddress(addr: string | undefined): addr is string {
  if (!addr || !/^127\.0\.0\.1:[1-9]\d{0,4}$/.test(addr)) return false;
  const port = Number(addr.slice(addr.lastIndexOf(":") + 1));
  return port >= 8765 && port <= 8770;
}

/**
 * Accept only a Tauri-reported owned listener with a valid bearer credential.
 * The display fallback remains useful in status text, but carries no token and
 * cannot be used for a connection.
 */
export function mcpOriginFromStatus(
  report: McpStatusReport | null | undefined,
  fallback: string,
  failures: McpFailureCounter,
): { origin: string; token: string | null; notice: string | null } {
  if (report?.handshake_ok === true && validMcpAddress(report.addr) && validMcpCredential(report.token)) {
    return { origin: `http://${report.addr}`, token: report.token, notice: null };
  }
  const kind = report?.handshake_ok === false
    ? "handshake"
    : report?.addr && !validMcpAddress(report.addr)
      ? "address"
      : report?.token !== undefined && !validMcpCredential(report.token)
        ? "credential"
        : "missing";
  failures.note("mcp_status", kind);
  return {
    origin: fallback,
    token: null,
    notice: `MCP owned-listener lookup failed (${kind}); connections are disabled`,
  };
}

/** The empty catch around the Tauri invoke. Count the error and name the fallback. */
export function noteMcpLookupError(error: unknown, fallback: string, failures: McpFailureCounter): string {
  const kind = error instanceof Error ? error.name : "error";
  failures.note("mcp_status", kind);
  return `MCP owned-listener lookup failed (${kind}); connections are disabled (${fallback})`;
}

/**
 * GET one library media URL. A non-library path is not a failure. A non-2xx
 * response or a thrown fetch is counted and returns null.
 */
export async function fetchLibraryBlob(
  fetchImpl: typeof fetch,
  origin: string,
  urlPath: string,
  failures: McpFailureCounter,
  token: string,
): Promise<Blob | null> {
  if (!urlPath.startsWith("/library/")) return null;
  if (!validMcpCredential(token)) {
    failures.note("library_media", "credential");
    return null;
  }
  try {
    const response = await fetchImpl(`${origin}${urlPath}`, {
      headers: { authorization: `Bearer ${token}` },
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    });
    if (!response.ok) {
      failures.note("library_media", `http_${response.status}`);
      return null;
    }
    return await response.blob();
  } catch (error) {
    failures.note("library_media", error instanceof Error ? error.name : "error");
    return null;
  }
}

export interface ControlStreamHooks {
  onOpen?: () => void;
  onControl: (data: string) => void;
  onGap: (data: string) => unknown | Promise<unknown>;
}

/**
 * Consume the stateless SSE snapshot with header-only authorization. Native
 * EventSource cannot set Authorization and must not receive a token in a URL.
 */
export async function consumeMcpControlStream(
  fetchImpl: typeof fetch,
  url: string,
  token: string,
  after: number,
  failures: McpFailureCounter,
  hooks: ControlStreamHooks,
): Promise<void> {
  if (!validMcpCredential(token) || !Number.isSafeInteger(after) || after < 0) {
    throw new Error("Invalid MCP control connection");
  }
  const separator = url.includes("?") ? "&" : "?";
  const response = await fetchImpl(`${url}${separator}after=${after}&wait=2000`, {
    method: "GET",
    headers: { authorization: `Bearer ${token}` },
    credentials: "omit",
    cache: "no-store",
    redirect: "error",
  });
  if (!response.ok) {
    failures.note("control_stream", `http_${response.status}`);
    throw new Error(`MCP control stream HTTP ${response.status}`);
  }
  if (!response.body) throw new Error("MCP control stream body is missing");
  hooks.onOpen?.();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let bytes = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 1024 * 1024) {
        await reader.cancel();
        throw new Error("MCP control stream exceeds limit");
      }
      buffered += decoder.decode(value, { stream: true });
      while (true) {
        const marker = /\r?\n\r?\n/.exec(buffered);
        if (!marker) break;
        const frame = buffered.slice(0, marker.index);
        buffered = buffered.slice(marker.index + marker[0].length);
        let event = "";
        const data: string[] = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
        }
        const payload = data.join("\n");
        if (event === "control") hooks.onControl(payload);
        else if (event === "gap") await hooks.onGap(payload);
      }
    }
    buffered += decoder.decode();
  } finally {
    reader.releaseLock();
  }
}

/**
 * Why a finished job cannot paint its tile. `null` when asset_resolve
 * returned ok. A missing body and `ok: false` are both failures.
 */
export function assetResolveFailure(resolved: unknown): string | null {
  if (!resolved || typeof resolved !== "object") return "asset_resolve failed";
  const record = resolved as { ok?: unknown; error?: unknown };
  if (record.ok === true) return null;
  return typeof record.error === "string" && record.error.length > 0 ? record.error : "asset_resolve failed";
}

/** Count an asset_resolve failure and the sentence the tile shows. */
export function noteAssetResolveFailure(uid: string, reason: string, failures: McpFailureCounter): string {
  failures.note("asset_resolve", "ok_false");
  return `asset_resolve failed for ${uid}: ${reason}`;
}

/** Where a control-bus seek landed, as the window measured it. */
export interface SeekLanding {
  ok: boolean;
  /** currentTime after the attempt, or null. */
  actual: number | null;
  status: string;
}

/**
 * The report the window posts after an MCP `ui_playback` seek (bus event
 * `seq`) finishes: MCP returns it to the agent as
 * {requested_t, landed_t, ok, reason}.
 */
export function seekReportControl(
  seq: number,
  requested: number,
  landing: SeekLanding,
): {
  name: "ui_seek_report";
  args: { seq: number; requested_t: number; landed_t: number | null; ok: boolean; reason: string };
} {
  const landedT = landing.actual !== null && Number.isFinite(landing.actual) ? Math.round(landing.actual * 1000) / 1000 : null;
  const reason = landing.status === "superseded" ? "superseded by a newer seek" : landing.status;
  return { name: "ui_seek_report", args: { seq, requested_t: requested, landed_t: landedT, ok: landing.ok, reason: reason.slice(0, 240) } };
}

/** Delay before the next control-stream connect. 250ms, then double, capped at 8s. */
export function controlBackoffMs(attempt: number): number {
  const step = Math.min(Math.max(0, attempt), 5);
  return 250 * 2 ** step;
}

/** Status line while the control stream is down. The window shows this instead of retrying silently. */
export function controlDisconnectedNotice(attempt: number): string {
  const wait = controlBackoffMs(attempt);
  const when = wait >= 1000 ? `${wait / 1000}s` : `${wait}ms`;
  return `Control stream disconnected. Retrying in ${when}.`;
}

/** One sentence for a failure the window used to drop. The only UI error log. */
export function surfaceUiError(error: unknown, where: string): string {
  const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  const message = `${where}: ${detail}`;
  console.warn(`gen-audio: ${message}`);
  return message;
}

let reportSink: (message: string) => void = () => {};

/** Where a reported rejection is shown. The window points this at the status line. */
export function setReportSink(sink: (message: string) => void): void {
  reportSink = sink;
}

/**
 * Log a rejected UI promise, show it, and rethrow.
 * Dropping the log leaves the warning empty. Dropping the rethrow resolves the promise.
 */
export function reportAsync(work: Promise<unknown>, where: string): Promise<unknown> {
  return work.catch((error: unknown) => {
    reportSink(surfaceUiError(error, where));
    throw error;
  });
}
