import type { FixtureHooksContext } from "./env";

/** Debug/test-only controls, loaded behind a compile-time fixture guard. */
export function installFixtureControls(context: FixtureHooksContext): void {
  const toolbar = document.querySelector(".ga-header-toolbar .toolbar");
  const showExample = document.createElement("button");
  showExample.type = "button";
  showExample.id = "show-example";
  showExample.textContent = "Load labeled example";
  showExample.addEventListener("click", () => {
    void context.loadDocument().then(context.refreshConnectors).then(context.show);
  });
  const runImprove = document.createElement("button");
  runImprove.type = "button";
  runImprove.id = "run-improve";
  runImprove.textContent = "Run Python improve on fixture";
  runImprove.addEventListener("click", async () => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const result = await invoke<Record<string, unknown>>("run_fixture_improve");
      context.status.textContent = result.ok
        ? "Python improve finished on the fixture tone (fixture only — not podcast speech)."
        : `Python improve did not finish: ${JSON.stringify(result)}`;
    } catch (error) {
      context.status.textContent = `Python improve needs a debug desktop shell. ${String(error)}`;
    }
  });
  toolbar?.prepend(showExample);
  toolbar?.append(runImprove);

  (window as unknown as { __genAudioScrub?: (fraction: number) => Promise<string> }).__genAudioScrub =
    (fraction: number) => {
      context.setCubeScrub(fraction, { silent: true });
      return context.seekBoundClip(fraction);
    };
}
