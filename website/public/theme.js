(() => {
  const storageKey = "sigma-theme";
  const currentTheme = () => {
    try {
      return localStorage.getItem(storageKey) === "light" ? "light" : "dark";
    } catch {
      return "dark";
    }
  };

  function syncControls(theme) {
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll(".theme-toggle").forEach((button) => {
      const nextMode = theme === "light" ? "dark" : "light";
      button.setAttribute("aria-pressed", String(theme === "light"));
      button.setAttribute("aria-label", "Switch to " + nextMode + " mode");
      button.title = "Switch to " + nextMode + " mode";
      const label = button.querySelector("[data-theme-label], .theme-toggle-label, #theme-toggle-label");
      if (label) label.textContent = nextMode[0].toUpperCase() + nextMode.slice(1) + " mode";
    });
  }

  function setTheme(theme) {
    try {
      localStorage.setItem(storageKey, theme);
    } catch {}
    syncControls(theme);
  }

  syncControls(currentTheme());
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => syncControls(currentTheme()), { once: true });
  } else {
    syncControls(currentTheme());
  }
  document.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest(".theme-toggle") : null;
    if (button) setTheme(currentTheme() === "light" ? "dark" : "light");
  });
  window.addEventListener("storage", (event) => {
    if (event.key === storageKey) syncControls(event.newValue === "light" ? "light" : "dark");
  });
})();
