import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSemanticCube, projectSemanticCube, SEMANTIC_MAX_BYTES } from "../src/semantic-cube.ts";

function document(count: number) {
  return { schema_version: "1.0.0", kind: "semantic_cube", layers: [{id:"metric",position:0.5}],
    records: Array.from({length:count}, (_,i) => ({id:`r${i}`})) };
}

// These unit tests exercise page assembly; actual native validation is tested
// in gen-audio-core and gen-audio-mcp, not simulated as a native runtime.
test("native projection is assembled with its exact layer and record identities", async () => {
  const doc = document(2);
  const result = await projectSemanticCube(doc, async args => {
    assert.equal(args.offset, 0);
    return {ok:true,totalPoints:2,records:2,skipped:0,clipped:0,nextOffset:null,
      points:[{t:0,f:0,z:0.5,v:0,raw:0,layer:"metric",recordId:"r0"},
        {t:1,f:1,z:0.5,v:1,raw:2,layer:"metric",recordId:"r1"}]};
  });
  assert.equal(result.projection.points.length, 2);
  assert.equal(result.projection.points[0].v, 0);
});

test("refused, incomplete, stale, and invalid native pages never commit", async () => {
  const doc = document(1);
  const page = {ok:true,totalPoints:1,records:1,skipped:0,clipped:0,nextOffset:null,
    points:[{t:0,f:0,z:0.5,v:0,raw:0,layer:"metric",recordId:"r0"}]};
  await assert.rejects(projectSemanticCube(doc, async () => ({...page,points:[]})));
  await assert.rejects(projectSemanticCube(doc, async () => ({...page,nextOffset:0})));
  await assert.rejects(projectSemanticCube(doc, async () => ({...page,points:[{...page.points[0],z:0.3}]})));
  await assert.rejects(projectSemanticCube(doc, async () => { throw new Error("refused"); }));
  await assert.rejects(projectSemanticCube(doc, async () => page, () => false));
  assert.throws(() => parseSemanticCube("x".repeat(SEMANTIC_MAX_BYTES + 1)), /1 MiB/);
});
