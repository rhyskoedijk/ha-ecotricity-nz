// @vitest-environment happy-dom
import { expect, it } from "vitest";

import { CARD_TYPE } from "../src/card-type";

type Constructor = CustomElementConstructor;

/**
 * A stand-in for the scoped custom element registry polyfill that Home Assistant's app installs:
 * a registry of its own, which knows nothing of what the native one held before it.
 */
class ScopedRegistry {
  private definitions = new Map<string, Constructor>();
  private waiting = new Map<string, (constructor: Constructor) => void>();

  constructor(private readonly native: CustomElementRegistry) {}

  define(name: string, constructor: Constructor): void {
    this.definitions.set(name, constructor);
    this.waiting.get(name)?.(constructor);
    // The polyfill defines a stand-in natively, so the browser upgrades the element.
    this.native.define(name, class extends HTMLElement {});
  }

  get(name: string): Constructor | undefined {
    return this.definitions.get(name);
  }

  whenDefined(name: string): Promise<Constructor> {
    const defined = this.definitions.get(name);
    return defined
      ? Promise.resolve(defined)
      : new Promise((resolve) => this.waiting.set(name, resolve));
  }
}

function useRegistry(registry: unknown): void {
  for (const target of [window, globalThis]) {
    Object.defineProperty(target, "customElements", { value: registry, configurable: true });
  }
}

// review: TEST-21
it("defines the card in the registry the app installs, however early it's loaded", async () => {
  // Loaded with the frontend, before the app has started.
  const native = window.customElements;
  const { APP_ELEMENT } = await import("../src/index");
  // However long the app takes, the card waits for it.
  await new Promise((resolve) => setTimeout(resolve, 200));
  expect(native.get(CARD_TYPE)).toBeUndefined();

  // The app installs its registry, then defines itself.
  const scoped = new ScopedRegistry(native);
  useRegistry(scoped);
  scoped.define(APP_ELEMENT, class extends HTMLElement {});
  for (let i = 0; i < 10 && !scoped.get(CARD_TYPE); i++) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  // The card is where Home Assistant looks for it.
  expect(scoped.get(CARD_TYPE)).toBeDefined();
});
