export type GenAudioEnv = {
  VITE_GEN_AUDIO_FIXTURES?: string;
};

const viteEnv = (import.meta as ImportMeta & { env?: GenAudioEnv }).env;
export const VITE_GEN_AUDIO_FIXTURES: string | undefined = viteEnv?.VITE_GEN_AUDIO_FIXTURES;

// These literal-backed exports let Rollup remove fixture imports and controls
// from release bundles. The non-Vite branch is the plain-Node test seam.
export const UNDER_VITE: boolean = import.meta.env?.MODE !== undefined;
export const FIXTURES_BUILD: boolean =
  typeof import.meta.env !== "undefined" && import.meta.env.VITE_GEN_AUDIO_FIXTURES === "1";

let bound: GenAudioEnv | null = null;

export function bindEnv(next: GenAudioEnv): void {
  bound = { ...next };
}

export function fixturesFlag(): string | undefined {
  return bound ? bound.VITE_GEN_AUDIO_FIXTURES : VITE_GEN_AUDIO_FIXTURES;
}

export interface FixtureHooksContext {
  loadDocument: () => Promise<unknown>;
  refreshConnectors: (document: unknown) => Promise<unknown>;
  show: (document: unknown) => void;
  status: HTMLElement;
  setCubeScrub: (fraction: number, options: { silent: boolean }) => void;
  seekBoundClip: (fraction: number) => Promise<string>;
}

async function loadControls(context: FixtureHooksContext): Promise<void> {
  const { installFixtureControls } = await import("./fixture-controls");
  installFixtureControls(context);
}

export function installFixtureHooks(context: FixtureHooksContext): Promise<void> {
  const viteOn =
    typeof import.meta.env !== "undefined" && import.meta.env.VITE_GEN_AUDIO_FIXTURES === "1";
  if (viteOn || (bound !== null && bound.VITE_GEN_AUDIO_FIXTURES === "1")) {
    return loadControls(context);
  }
  return Promise.resolve();
}
