import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { slugify } from "../src/ops.js";

describe("slugify", () => {
  it("lowercases and joins words with dashes", () => {
    assert.equal(slugify("Hello World"), "hello-world");
  });

  it("collapses surrounding and repeated whitespace", () => {
    assert.equal(slugify("  Mixed   Case  Text "), "mixed-case-text");
  });
});
