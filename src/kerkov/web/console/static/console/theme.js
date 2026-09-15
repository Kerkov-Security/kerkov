(() => {
  const storageKey = "kerkov-theme";
  const systemTheme = window.matchMedia("(prefers-color-scheme: light)");
  let preference = null;

  try {
    preference = window.localStorage.getItem(storageKey);
  } catch (_error) {
    // The theme still works when browser storage is unavailable.
  }

  function applyTheme() {
    const theme = preference === "light" || preference === "dark"
      ? preference
      : systemTheme.matches ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    const toggle = document.querySelector("#theme-toggle");
    if (toggle) {
      const label = `Switch to ${theme === "dark" ? "light" : "dark"} theme`;
      toggle.setAttribute("aria-label", label);
      toggle.title = label;
    }
  }

  // Loaded in the head so the saved theme is set before the page paints.
  applyTheme();

  document.addEventListener("DOMContentLoaded", () => {
    applyTheme();
    const toggle = document.querySelector("#theme-toggle");
    if (!toggle) {
      return;
    }
    toggle.addEventListener("click", () => {
      preference = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      applyTheme();
      try {
        window.localStorage.setItem(storageKey, preference);
      } catch (_error) {
        // Keep the selected theme for this page even without storage.
      }
    });
  });

  systemTheme.addEventListener("change", applyTheme);
  window.addEventListener("storage", (event) => {
    if (event.key === storageKey || event.key === null) {
      preference = event.newValue;
      applyTheme();
    }
  });
})();
