import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ev } from "../test/factories";
import { TrafficTable } from "./TrafficTable";

function setup(events = defaultEvents(), extra: Partial<Parameters<typeof TrafficTable>[0]> = {}) {
  const onSelect = vi.fn();
  render(<TrafficTable events={events} selectedId={null} showRun={false} autoScroll onSelect={onSelect} {...extra} />);
  return { onSelect };
}

function defaultEvents() {
  return [
    ev({ id: "ok" }),
    ev({ id: "flight", host: "bedrock-runtime.us-east-1.amazonaws.com", path: "/model/x/converse", method: "POST", status_code: null }),
    ev({ id: "bad", host: "pastebin.com", path: "/api", method: "POST", decision: "blocked", rule_id: "block-paste-sites", status_code: 403 }),
    ev({ id: "stop", kind: "run_stopped", reason: "policy: rule 'block-paste-sites' requires termination" }),
  ];
}

describe("TrafficTable", () => {
  it("labels each decision with text, not only color", () => {
    setup();
    const rows = screen.getAllByRole("row").slice(1); // skip the header row
    expect(within(rows[0]).getByText("Allowed")).toBeInTheDocument();
    expect(within(rows[1]).getByText("In flight")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Blocked")).toBeInTheDocument();
    expect(within(rows[2]).getByText("block-paste-sites")).toBeInTheDocument();
    expect(within(rows[2]).getByText("403")).toBeInTheDocument();
  });

  it("shows run lifecycle events as their own rows", () => {
    setup();
    expect(screen.getByText("Run stopped")).toBeInTheDocument();
    expect(screen.getByText(/requires termination/)).toBeInTheDocument();
  });

  it("opens details from the keyboard-reachable destination button", async () => {
    const { onSelect } = setup();
    await userEvent.click(screen.getByRole("button", { name: "pastebin.com/api" }));
    expect(onSelect).toHaveBeenCalledWith("bad");
    expect(onSelect).toHaveBeenCalledTimes(1); // row click handler is not triggered twice
  });

  it("opens details when a row is clicked", async () => {
    const { onSelect } = setup();
    await userEvent.click(screen.getByText("Allowed"));
    expect(onSelect).toHaveBeenCalledWith("ok");
  });

  it("shows the run column only in the all-runs view", () => {
    setup(undefined, { showRun: true });
    expect(screen.getByRole("columnheader", { name: "Run" })).toBeInTheDocument();
  });

  it("explains how to start when there is no traffic", () => {
    setup([]);
    expect(screen.getByText(/Waiting for traffic/)).toBeInTheDocument();
  });

  it("is a focusable, labelled region so keyboard users can scroll it", () => {
    setup();
    const region = screen.getByRole("region", { name: "Live traffic" });
    expect(region).toHaveAttribute("tabindex", "0");
  });
});
