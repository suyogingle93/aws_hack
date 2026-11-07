import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { run } from "../test/factories";
import { ALL_RUNS, RunsSidebar } from "./RunsSidebar";

function setup(over: Partial<Parameters<typeof RunsSidebar>[0]> = {}) {
  const onStop = vi.fn();
  const onSelect = vi.fn();
  render(
    <RunsSidebar
      runs={[run({ id: "live1" }), run({ id: "done1", status: "finished" }), run({ id: "killed", status: "stopped" })]}
      selected={ALL_RUNS}
      stopping={new Set()}
      onStop={onStop}
      onSelect={onSelect}
      {...over}
    />,
  );
  return { onStop, onSelect };
}

describe("RunsSidebar", () => {
  it("offers a labelled Stop button only for running runs", () => {
    setup();
    expect(screen.getByRole("button", { name: "Stop run live1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop run done1" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop run killed" })).not.toBeInTheDocument();
  });

  it("calls onStop with the run id, without also selecting the run", async () => {
    const { onStop, onSelect } = setup();
    await userEvent.click(screen.getByRole("button", { name: "Stop run live1" }));
    expect(onStop).toHaveBeenCalledWith("live1");
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("works from the keyboard", async () => {
    const { onStop } = setup();
    screen.getByRole("button", { name: "Stop run live1" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(onStop).toHaveBeenCalledWith("live1");
  });

  it("disables Stop while the stop is in progress", () => {
    setup({ stopping: new Set(["live1"]) });
    const button = screen.getByRole("button", { name: "Stop run live1" });
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("Stopping…");
  });

  it("shows status, counts, and a short command", () => {
    setup();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Finished")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.getAllByText("✓ 3 allowed")).toHaveLength(3);
    expect(screen.getAllByText("python demo_agent.py")).toHaveLength(3);
  });

  it("marks the selected run and selects on click", async () => {
    const { onSelect } = setup({ selected: "done1" });
    const done = screen.getByRole("button", { name: /done1/ });
    expect(done).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: /live1.*Running/ }));
    expect(onSelect).toHaveBeenCalledWith("live1");
  });

  it("explains how to start a run when there are none", () => {
    render(<RunsSidebar runs={[]} selected={ALL_RUNS} stopping={new Set()} onStop={vi.fn()} onSelect={vi.fn()} />);
    expect(screen.getByText(/No runs yet/)).toBeInTheDocument();
  });
});
