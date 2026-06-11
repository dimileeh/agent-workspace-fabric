/**
 * Copy text to the clipboard in both secure and non-secure browsing contexts.
 *
 * `navigator.clipboard` only exists in a *secure context*: HTTPS, or the
 * `localhost`/`127.0.0.1` special case. When the console is reached over plain
 * HTTP — e.g. via a Tailscale address like `http://host.tailnet.ts.net` — the
 * async Clipboard API is unavailable, so `navigator.clipboard.writeText` throws
 * and copying silently fails. That is why "click the workspace id to copy" works
 * on 127.0.0.1 but not over Tailscale.
 *
 * Strategy: use the modern Clipboard API when it is present, and otherwise fall
 * back to the legacy `document.execCommand("copy")` path (a hidden, selected
 * textarea), which works over plain HTTP. Returns whether the copy succeeded so
 * callers can decide whether to show success feedback.
 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  if (
    typeof navigator !== "undefined" &&
    navigator.clipboard &&
    typeof navigator.clipboard.writeText === "function"
  ) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // The async API is present but rejected (blocked permission, or a browser
      // that exposes it over HTTP yet refuses to write). Fall through to the
      // legacy path rather than reporting failure outright.
    }
  }
  // `legacyCopyText` guards its own `execCommand` call, but the surrounding DOM
  // operations (createElement/appendChild/focus/selection restore) can still
  // throw. Catch here so this async function always resolves to a boolean rather
  // than rejecting and leaving the caller's success gate with an unhandled
  // rejection.
  try {
    return legacyCopyText(text);
  } catch {
    return false;
  }
}

/**
 * Legacy clipboard write via a transient, off-screen textarea + `execCommand`.
 * Deprecated but still the only option that works over plain HTTP, and supported
 * by every browser the console targets.
 */
function legacyCopyText(text: string): boolean {
  if (
    typeof document === "undefined" ||
    !document.body ||
    typeof document.execCommand !== "function"
  ) {
    return false;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  // Keep the element out of the viewport and non-interactive so selecting it
  // does not scroll the page or flash a focus ring.
  textarea.setAttribute("readonly", "");
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.top = "0";
  textarea.style.left = "0";
  textarea.style.width = "1px";
  textarea.style.height = "1px";
  textarea.style.padding = "0";
  textarea.style.border = "none";
  textarea.style.outline = "none";
  textarea.style.boxShadow = "none";
  textarea.style.background = "transparent";
  textarea.style.opacity = "0";

  // Preserve any existing user selection so we can restore it afterwards.
  const selection = typeof document.getSelection === "function" ? document.getSelection() : null;
  const previousRange = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
  // Remember which element had focus (typically the button that triggered the
  // copy) so we can hand focus back after the textarea is gone — otherwise
  // focusing then removing the textarea drops focus to <body>, which breaks
  // keyboard navigation for the user who just clicked/activated the control.
  const previouslyFocused = document.activeElement;

  document.body.appendChild(textarea);
  // `preventScroll` stops the browser from scrolling/jumping to the textarea on
  // focus, which can happen even for a `position: fixed` element.
  textarea.focus({ preventScroll: true });
  textarea.select();

  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }

  textarea.remove();

  if (selection && previousRange) {
    selection.removeAllRanges();
    selection.addRange(previousRange);
  }

  // Restore focus to the triggering element. `preventScroll` keeps this from
  // jumping the viewport, mirroring the textarea focus above.
  if (previouslyFocused && typeof (previouslyFocused as HTMLElement).focus === "function") {
    (previouslyFocused as HTMLElement).focus({ preventScroll: true });
  }

  return copied;
}
