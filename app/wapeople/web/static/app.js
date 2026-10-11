// Progressive enhancement only: every page works without JavaScript.
document.addEventListener("change", (e) => {
  const el = e.target;
  if (el.matches("form.filters select, form.filters input[type=checkbox]")) {
    const form = el.form;
    const page = form.querySelector("input[name=page]");
    if (page) page.remove();
    form.requestSubmit ? form.requestSubmit() : form.submit();
  }
});

// Press "/" anywhere to jump to the search box.
document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
  const box = document.querySelector(".big-search input, .header-search input");
  if (box) { e.preventDefault(); box.focus(); box.select(); }
});
