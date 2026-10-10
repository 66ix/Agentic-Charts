import { describe, expect, it } from "vitest";

import { panelLayer } from "./layers";

describe("panelLayer", () => {
  it("puts each dock set on its own named layer", () => {
    expect(panelLayer("myentry")).toBe("position");
    expect(panelLayer("liqzones")).toBe("liqs");
    expect(panelLayer("scanner")).toBe("scanner");
    expect(panelLayer("desk:active:INJUSDT")).toBe("desk");
    expect(panelLayer("exit:INJUSDT")).toBe("plan");
    expect(panelLayer("something-new")).toBe("panels");
  });
});
