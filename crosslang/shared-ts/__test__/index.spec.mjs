import test from "ava";

import { sumAsString } from "../index.js";

test("sum from native", (t) => {
  t.is(sumAsString(1, 2), "3");
});
