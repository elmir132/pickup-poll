(() => {
  const storageKey = "pickup-poll-theme";
  const root = document.documentElement;
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  let hasSavedChoice = false;
  let savedChoice = "";

  try {
    const saved = localStorage.getItem(storageKey);
    if (saved === "dark" || saved === "light") {
      savedChoice = saved;
      hasSavedChoice = true;
    }
  } catch (error) { /* private browsing can disable local storage */ }

  const applyTheme = theme => {
    root.dataset.theme = theme;
    const meta = document.getElementById("theme-color-meta");
    if (meta) meta.content = theme === "dark" ? "#111813" : "#f4f4ed";
  };

  applyTheme(hasSavedChoice ? savedChoice : (systemTheme.matches ? "dark" : "light"));

  const followSystemTheme = event => {
    if (!hasSavedChoice) applyTheme(event.matches ? "dark" : "light");
  };
  if (typeof systemTheme.addEventListener === "function") systemTheme.addEventListener("change", followSystemTheme);
  else systemTheme.addListener(followSystemTheme);

  window.pickupPollTheme = {
    toggle() {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      hasSavedChoice = true;
      try { localStorage.setItem(storageKey, next); } catch (error) { /* keep this tab's choice */ }
      return next;
    }
  };
})();
