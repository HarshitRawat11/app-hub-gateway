"use strict";

// The dashboard talks to gateway, which is also the server that sent this
// file. So every URL below is a bare path -- no host, no port, no configured
// API base. That is the single biggest reason the dashboard lives inside
// gateway: a separate origin would need CORS headers on every response and a
// build-time or runtime answer to "where is the API?".

const el = {
  links: document.getElementById("links"),
  search: document.getElementById("search"),
  banner: document.getElementById("banner"),
  count: document.getElementById("count"),
  form: document.getElementById("add-form"),
  toggle: document.getElementById("toggle-add"),
  cancel: document.getElementById("cancel-add"),
  categories: document.getElementById("categories"),
};

let allLinks = [];

// gateway's whole reason to exist is that it distinguishes these. Showing the
// user "something went wrong" for all three would throw that away at the last
// step -- 503 and 504 are about links-service, 502 means it answered but
// badly, and they point at different things to go and look at.
const GATEWAY_ERRORS = {
  502: "links-service answered, but with an error.",
  503: "links-service is unreachable. Is it running?",
  504: "links-service did not answer within 3 seconds.",
};

function showError(status, detail) {
  el.banner.textContent = "";
  const known = GATEWAY_ERRORS[status];
  el.banner.append(known || detail || "Request failed.");
  const code = document.createElement("code");
  code.textContent = `  HTTP ${status}`;
  el.banner.append(code);
  el.banner.hidden = false;
}

function clearError() {
  el.banner.hidden = true;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    // Read the detail before throwing. An empty body is fine -- the status is
    // what carries the meaning.
    let detail = "";
    try {
      const parsed = await res.json();
      detail = typeof parsed.detail === "string" ? parsed.detail : "";
    } catch { /* non-JSON error body; the status still stands */ }
    const err = new Error(detail || `HTTP ${res.status}`);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

// Only http(s) reaches an href. A stored "javascript:..." url would otherwise
// execute on click, with this page's origin and whatever it can reach -- and
// link records are writable by anything that can POST to the API. Rendering
// untrusted data is the one place a dashboard can genuinely hurt you.
function safeHref(url) {
  try {
    const parsed = new URL(url, window.location.origin);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}

function hostOf(url) {
  try {
    return new URL(url, window.location.origin).host;
  } catch {
    return url;
  }
}

function render(links) {
  el.links.textContent = "";
  el.links.setAttribute("aria-busy", "false");

  if (!links.length) {
    const p = document.createElement("p");
    p.className = "empty";
    p.textContent = allLinks.length ? "Nothing matches that." : "No links yet. Add one.";
    el.links.append(p);
    return;
  }

  const byCategory = new Map();
  for (const link of links) {
    const key = link.category || "uncategorised";
    if (!byCategory.has(key)) byCategory.set(key, []);
    byCategory.get(key).push(link);
  }

  for (const category of [...byCategory.keys()].sort()) {
    const section = document.createElement("section");
    const h2 = document.createElement("h2");
    h2.textContent = category;
    const ul = document.createElement("ul");

    const sorted = byCategory.get(category)
      .sort((a, b) => a.name.localeCompare(b.name));

    for (const link of sorted) {
      ul.append(row(link));
    }
    section.append(h2, ul);
    el.links.append(section);
  }
}

function row(link) {
  const li = document.createElement("li");

  const icon = document.createElement("span");
  icon.className = "icon";
  // textContent throughout, never innerHTML. Every string here came from the
  // API, and an icon field reading "<img onerror=...>" would otherwise be
  // markup rather than text.
  icon.textContent = link.icon || "•";

  const href = safeHref(link.url);
  const a = document.createElement(href ? "a" : "span");
  if (href) {
    a.href = href;
    a.rel = "noopener noreferrer";
  }
  a.textContent = link.name;
  a.title = link.url;

  const host = document.createElement("span");
  host.className = "host";
  host.textContent = href ? hostOf(link.url) : "unsupported URL";
  a.append(host);

  const del = document.createElement("button");
  del.className = "del";
  del.type = "button";
  del.textContent = "×";
  del.title = `Delete ${link.name}`;
  del.setAttribute("aria-label", `Delete ${link.name}`);
  del.addEventListener("click", () => remove(link));

  li.append(icon, a, del);
  return li;
}

function applyFilter() {
  const q = el.search.value.trim().toLowerCase();
  const matches = !q ? allLinks : allLinks.filter((l) =>
    [l.name, l.url, l.category].some((f) => (f || "").toLowerCase().includes(q))
  );
  render(matches);
  el.count.textContent = q
    ? `${matches.length} of ${allLinks.length}`
    : `${allLinks.length} link${allLinks.length === 1 ? "" : "s"}`;
}

function refreshCategories() {
  el.categories.textContent = "";
  for (const c of [...new Set(allLinks.map((l) => l.category))].sort()) {
    const option = document.createElement("option");
    option.value = c;
    el.categories.append(option);
  }
}

async function load() {
  try {
    allLinks = await api("GET", "/links");
    clearError();
    refreshCategories();
    applyFilter();
  } catch (err) {
    showError(err.status, err.detail);
    el.links.setAttribute("aria-busy", "false");
  }
}

async function remove(link) {
  if (!window.confirm(`Delete "${link.name}"?`)) return;
  try {
    await api("DELETE", `/links/${encodeURIComponent(link.id)}`);
    clearError();
    await load();
  } catch (err) {
    showError(err.status, err.detail);
  }
}

el.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(el.form));
  // Send no `icon` key at all when it is blank, rather than "". The API models
  // it as `str | None`, and an empty string is a value that means "not set" --
  // exactly the shape C-06 avoided storing in DynamoDB.
  if (!data.icon) delete data.icon;
  try {
    await api("POST", "/links", data);
    el.form.reset();
    el.form.hidden = true;
    el.toggle.setAttribute("aria-expanded", "false");
    clearError();
    await load();
  } catch (err) {
    // 422 reaches here because gateway passes links-service's validation
    // response through rather than flattening it to a 502.
    showError(err.status, err.status === 422 ? "That link was rejected: check the URL." : err.detail);
  }
});

el.toggle.addEventListener("click", () => {
  const opening = el.form.hidden;
  el.form.hidden = !opening;
  el.toggle.setAttribute("aria-expanded", String(opening));
  if (opening) el.form.querySelector("input").focus();
});

el.cancel.addEventListener("click", () => {
  el.form.hidden = true;
  el.toggle.setAttribute("aria-expanded", "false");
});

el.search.addEventListener("input", applyFilter);

document.addEventListener("keydown", (event) => {
  // "/" focuses search, the convention every start page and code host uses.
  // Guarded so it does not hijack a slash typed into the add form.
  if (event.key === "/" && document.activeElement.tagName !== "INPUT") {
    event.preventDefault();
    el.search.focus();
  }
  if (event.key === "Escape" && document.activeElement === el.search) {
    el.search.value = "";
    applyFilter();
    el.search.blur();
  }
});

load();
