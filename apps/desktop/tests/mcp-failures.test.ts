// Silent MCP failures must be counted. An empty catch, a null return, or a
// dropped tile is a stub: nothing in the window shows that the call failed.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  McpFailureCounter,
  assetResolveFailure,
  fetchLibraryBlob,
  mcpOriginFromStatus,
  noteAssetResolveFailure,
  noteMcpLookupError,
} from "../src/play-control.ts";

test("a failed MCP handshake is counted and the fallback is named", () => {
  const failures = new McpFailureCounter(() => {});
  const found = mcpOriginFromStatus(
    { addr: "127.0.0.1:8766", handshake_ok: false },
    "http://127.0.0.1:8765",
    failures,
  );
  assert.equal(found.origin, "http://127.0.0.1:8765");
  assert.equal(found.token, null);
  assert.match(found.notice ?? "", /handshake/);
  assert.match(found.notice ?? "", /connections are disabled/);
  assert.equal(failures.snapshot()["mcp_status:handshake"], 1);
});

test("a thrown MCP address lookup is counted and named", () => {
  const failures = new McpFailureCounter(() => {});
  const notice = noteMcpLookupError(new TypeError("invoke"), "http://127.0.0.1:8765", failures);
  assert.match(notice, /TypeError/);
  assert.match(notice, /127\.0\.0\.1:8765/);
  assert.equal(failures.snapshot()["mcp_status:TypeError"], 1);
});

test("a successful handshake replaces the fallback and counts nothing", () => {
  const failures = new McpFailureCounter(() => {});
  const found = mcpOriginFromStatus(
    { addr: "127.0.0.1:8766", token: "ab".repeat(32), handshake_ok: true },
    "http://127.0.0.1:8765",
    failures,
  );
  assert.equal(found.origin, "http://127.0.0.1:8766");
  assert.equal(found.token, "ab".repeat(32));
  assert.equal(found.notice, null);
  assert.equal(failures.total(), 0);
});

test("a stale or malformed credential is rejected instead of reusing an endpoint", () => {
  for (const token of [undefined, "", "not-a-token"]) {
    const failures = new McpFailureCounter(() => {});
    const found = mcpOriginFromStatus(
      { addr: "127.0.0.1:8766", token, handshake_ok: true },
      "http://127.0.0.1:8765",
      failures,
    );
    assert.equal(found.origin, "http://127.0.0.1:8765");
    assert.equal(found.token, null);
    assert.match(found.notice ?? "", /disabled/);
    assert.equal(failures.total(), 1);
  }
});

test("an MCP address outside the CSP-approved desktop range is rejected", () => {
  for (const addr of [
    "127.0.0.1:8764",
    "127.0.0.1:8771",
    "localhost:8766",
    "[::1]:8766",
  ]) {
    const found = mcpOriginFromStatus(
      { addr, token: "ab".repeat(32), handshake_ok: true },
      "http://127.0.0.1:8765",
      new McpFailureCounter(() => {}),
    );
    assert.equal(found.origin, "http://127.0.0.1:8765");
    assert.equal(found.token, null);
    assert.match(found.notice ?? "", /disabled/);
  }
});

test("a failed library fetch is counted instead of returning null quietly", async () => {
  const failures = new McpFailureCounter(() => {});
  const fetchImpl = async () => new Response("no", { status: 404 });
  const blob = await fetchLibraryBlob(
    fetchImpl as typeof fetch,
    "http://127.0.0.1:8765",
    "/library/x.wav",
    failures,
    "ab".repeat(32),
  );
  assert.equal(blob, null);
  assert.equal(failures.snapshot()["library_media:http_404"], 1);
});

test("a library fetch that throws is counted", async () => {
  const failures = new McpFailureCounter(() => {});
  const fetchImpl = async () => {
    throw new TypeError("failed");
  };
  const blob = await fetchLibraryBlob(
    fetchImpl as typeof fetch,
    "http://127.0.0.1:8765",
    "/library/x.wav",
    failures,
    "ab".repeat(32),
  );
  assert.equal(blob, null);
  assert.equal(failures.snapshot()["library_media:TypeError"], 1);
});

test("a path outside /library/ is not a media failure", async () => {
  const failures = new McpFailureCounter(() => {});
  let called = false;
  const fetchImpl = async () => {
    called = true;
    return new Response(null, { status: 500 });
  };
  const blob = await fetchLibraryBlob(
    fetchImpl as typeof fetch,
    "http://127.0.0.1:8765",
    "/other",
    failures,
    "ab".repeat(32),
  );
  assert.equal(blob, null);
  assert.equal(called, false);
  assert.equal(failures.total(), 0);
});

test("asset_resolve ok false is counted and the tile text names the uid", () => {
  const failures = new McpFailureCounter(() => {});
  assert.equal(assetResolveFailure({ ok: false }), "asset_resolve failed");
  assert.equal(assetResolveFailure({ ok: false, error: "missing uid" }), "missing uid");
  assert.equal(assetResolveFailure(null), "asset_resolve failed");
  assert.equal(assetResolveFailure({ ok: true }), null);
  const message = noteAssetResolveFailure("ga:audio_clip:abc", "missing uid", failures);
  assert.match(message, /ga:audio_clip:abc/);
  assert.match(message, /missing uid/);
  assert.equal(failures.snapshot()["asset_resolve:ok_false"], 1);
});
