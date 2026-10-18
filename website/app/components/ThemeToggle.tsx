"use client";

import { useEffect } from "react";

type Theme = "dark" | "light";

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  window.localStorage.setItem("sigma-theme", theme);
}

export default function ThemeToggle() {
  useEffect(() => {
    const storedTheme = window.localStorage.getItem("sigma-theme");
    const initialTheme: Theme = storedTheme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = initialTheme;

    const syncTheme = (event: StorageEvent) => {
      if (event.key !== "sigma-theme") return;
      const nextTheme: Theme = event.newValue === "light" ? "light" : "dark";
      document.documentElement.dataset.theme = nextTheme;
    };
    window.addEventListener("storage", syncTheme);
    return () => window.removeEventListener("storage", syncTheme);
  }, []);

  function toggleTheme() {
    const nextTheme: Theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    applyTheme(nextTheme);
  }

  return (
    <button
      className="theme-toggle"
      type="button"
      onClick={toggleTheme}
      aria-label="Toggle light and dark theme"
      title="Toggle light and dark theme"
    >
      <svg className="theme-sun" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="3.5" />
        <path d="M12 2v2m0 16v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2 12h2m16 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42" />
      </svg>
      <svg className="theme-moon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M20.2 15.2A8.6 8.6 0 0 1 8.8 3.8 8.7 8.7 0 1 0 20.2 15.2Z" />
      </svg>
      <span>Theme</span>
    </button>
  );
}
