import { authenticate } from "./auth";

describe("authenticate", () => {
  it("rejects empty tokens", () => {
    expect(authenticate("")).toBeNull();
  });
});