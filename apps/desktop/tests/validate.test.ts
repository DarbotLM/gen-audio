// The viewport checker must not read Vite's import.meta.env. Under plain Node
// that object is missing, so the example deck's "not remeasured" note used to
// crash the check instead of being accepted. Callers pass "allowed" or "release".
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { Ajv2020 } from "ajv/dist/2020.js";
import { validateViewport } from "../src/validate.ts";

const example = JSON.parse(
  readFileSync(fileURLToPath(new URL("../../../schemas/examples/viewport.example.json", import.meta.url)), "utf8"),
);

test("the example deck is valid when the caller passes allowed", () => {
  assert.equal(validateViewport(example, "allowed"), null);
});

test("release mode rejects the example deck's not-remeasured note", () => {
  assert.match(validateViewport(example, "release") ?? "", /not measured/);
});

test("allowed still rejects a note that claims the figures were measured", () => {
  const note = structuredClone(example);
  const row = note.cards.find((card: { kind: string }) => card.kind === "BenchmarkCompare");
  row.body.sourceNote = "These numbers were measured in this window.";
  assert.match(validateViewport(note, "allowed") ?? "", /not measured/);
});

test("validate.ts takes no build settings and does not embed the release-gate needle", () => {
  const source = readFileSync(fileURLToPath(new URL("../src/validate.ts", import.meta.url)), "utf8");
  assert.equal(source.includes("import.meta.env"), false);
  assert.equal(source.includes("not remeasured"), false);
});

test("card uid is required by validate.ts and the viewport schema", () => {
  const negative = JSON.parse(
    readFileSync(fileURLToPath(new URL("../../../schemas/examples/viewport.missing-uid.negative.json", import.meta.url)), "utf8"),
  );
  const error = validateViewport(negative, "allowed");
  assert.match(error ?? "", /engine-kokoro.*uid|required.*uid/i);
  const dropped = structuredClone(example);
  delete dropped.cards[0].uid;
  assert.match(validateViewport(dropped, "allowed") ?? "", /uid.*required|required.*uid/i);

  const schema = JSON.parse(
    readFileSync(fileURLToPath(new URL("../../../schemas/card-viewport.schema.json", import.meta.url)), "utf8"),
  );
  const validateSchema = new Ajv2020({ strict: false }).compile(schema);
  assert.equal(validateSchema(negative), false);
  assert.ok(
    (validateSchema.errors ?? []).some((issue) =>
      issue.params && (issue.params as { missingProperty?: string }).missingProperty === "uid"),
  );
});
