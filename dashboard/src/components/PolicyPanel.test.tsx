import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_RULE } from "../eventStore";
import { POLICY } from "../test/factories";
import { PolicyPanel } from "./PolicyPanel";

describe("PolicyPanel", () => {
  it("highlights the rule that decided the selected request", () => {
    render(<PolicyPanel policy={POLICY} highlight="block-paste-sites" reloading={false} onReload={vi.fn()} />);
    const matched = screen.getByText("block-paste-sites").closest("li");
    expect(matched).toHaveAttribute("aria-current", "true");
    expect(matched).toHaveTextContent("matched");
    expect(screen.getByText("allow-github-read").closest("li")).not.toHaveAttribute("aria-current");
  });

  it("highlights the default action when no rule matched", () => {
    render(<PolicyPanel policy={POLICY} highlight={DEFAULT_RULE} reloading={false} onReload={vi.fn()} />);
    expect(screen.getByText("Everything else (default)").closest("li")).toHaveAttribute("aria-current", "true");
  });

  it("shows actions, matchers, termination, and rate limit", () => {
    render(<PolicyPanel policy={POLICY} highlight={null} reloading={false} onReload={vi.fn()} />);
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("*pastebin.com")).toBeInTheDocument();
    expect(screen.getByText(/Stops the run on violation/)).toBeInTheDocument();
    expect(screen.getByText(/120 requests per minute/)).toBeInTheDocument();
  });

  it("reloads on request and disables the button while reloading", async () => {
    const onReload = vi.fn();
    const { rerender } = render(<PolicyPanel policy={POLICY} highlight={null} reloading={false} onReload={onReload} />);
    await userEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(onReload).toHaveBeenCalledTimes(1);
    rerender(<PolicyPanel policy={POLICY} highlight={null} reloading onReload={onReload} />);
    expect(screen.getByRole("button", { name: "Reloading…" })).toBeDisabled();
  });
});
