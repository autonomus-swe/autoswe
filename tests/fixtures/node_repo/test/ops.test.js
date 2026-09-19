import assert from "node:assert/strict";
import { test } from "node:test";

import { add, subtract } from "../src/ops.js";

test("add sums two numbers", () => {
  assert.equal(add(2, 3), 5);
});

test("subtract handles negative results", () => {
  assert.equal(subtract(5, 3), 2);
  assert.equal(subtract(0, 4), -4);
});
