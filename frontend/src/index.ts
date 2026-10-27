/**
 * The bill card's entry point, loaded on every dashboard by the integration.
 *
 * Home Assistant loads it with the frontend, possibly before the frontend's own app has started.
 * The app replaces `window.customElements` with a scoped registry polyfill, which knows nothing of
 * elements defined before it, so a card defined too early is never found ("Custom element not
 * found"). The card, with Lit, is only loaded once the app has defined `<home-assistant>`, by
 * which time the polyfill is in place. The registry is read when that's asked, so this works
 * whether the polyfill is installed yet or not: it defines the native element too.
 */

export const APP_ELEMENT = "home-assistant";

export async function loadWhenReady(): Promise<void> {
  await customElements.whenDefined(APP_ELEMENT);
  await import("./ecotricity-bill-card");
}

void loadWhenReady();
