// Adds a floating button that shows/hides every div in the site's global
// "toggle group" (class="sv-toggleable", set at build time by
// generate_indices.py from gloss_types.yaml's hideable: — see that file
// for the full convention). The button appears iff the current page has
// at least one .sv-toggleable div AT ALL, regardless of whether any of
// them start hidden (hidden_by_default only controls each div's OWN
// initial display via .sv-hidden-default — see custom.css — it has no
// bearing on the button).
//
// Clicking the button is a genuine show-all / hide-all for the WHOLE
// group: "Show" reveals every .sv-toggleable div, including ones that
// started visible-by-default; "Hide" hides every .sv-toggleable div,
// including ones that started visible-by-default. It is not merely a
// "reveal the ones that started hidden" switch.
//
// Before the first click, the page is showing its natural per-div
// initial state (.sv-hidden-default or not, per div). If NOTHING on the
// page actually starts hidden (every toggle-group member is already
// visible), that natural state already amounts to "fully shown" — so
// the button starts labeled "Hide" and its first click goes straight to
// force-hide, rather than uselessly offering "Show" first (which would
// visibly do nothing, since nothing was hidden to reveal). If at least
// one div does start hidden, the button starts labeled "Show" as before.
//
// Headings inside a toggle-group div still get an entry in Material's
// "on this page" TOC (built at build time, unaware of the button). While
// such a div is hidden, its headings' TOC entries are hidden too, so the
// TOC never offers a link to content that isn't on screen — see syncToc.
// An entry is hidden along with its whole <li>, so any sub-entries nested
// under it in the TOC go with it.
//
// Runs on every page load, including MkDocs Material's instant-navigation
// swaps.

(function () {
  // true iff `el` sits inside a toggle-group div that is currently
  // hidden — worked out from the same classes custom.css keys off
  // (body.sv-force-hide / body.sv-force-show / .sv-hidden-default), at
  // every .sv-toggleable ancestor, so a heading in a visible div nested
  // inside a hidden one counts as hidden.
  function inHiddenToggleable(el) {
    var body = document.body.classList;
    if (body.contains("sv-force-show")) return false;
    var t = el.closest(".sv-toggleable");
    if (t && body.contains("sv-force-hide")) return true;
    while (t) {
      if (t.classList.contains("sv-hidden-default")) return true;
      t = t.parentElement && t.parentElement.closest(".sv-toggleable");
    }
    return false;
  }

  // Show/hide the TOC entry of every heading inside a toggle-group div,
  // to match that heading's current visibility. A TOC link is matched by
  // its URL fragment (decoded — Devanagari ids come percent-encoded), and
  // only if it points at this page: with navigation.instant, Material
  // rewrites "#id" hrefs into absolute URLs.
  function syncToc() {
    var items = {};
    var here = location.origin + location.pathname;
    document.querySelectorAll(".md-nav--secondary a.md-nav__link").forEach(function (a) {
      var url = new URL(a.href, location.href);
      if (!url.hash || url.origin + url.pathname !== here) return;
      var id;
      try { id = decodeURIComponent(url.hash.slice(1)); } catch (e) { id = url.hash.slice(1); }
      var li = a.closest("li.md-nav__item");
      if (li) (items[id] = items[id] || []).push(li);
    });
    document
      .querySelectorAll(".md-typeset .sv-toggleable h1[id], .md-typeset .sv-toggleable h2[id], " +
        ".md-typeset .sv-toggleable h3[id], .md-typeset .sv-toggleable h4[id], " +
        ".md-typeset .sv-toggleable h5[id], .md-typeset .sv-toggleable h6[id]")
      .forEach(function (h) {
        var hidden = inHiddenToggleable(h);
        (items[h.id] || []).forEach(function (li) {
          li.style.display = hidden ? "none" : "";
        });
      });
  }

  function setup() {
    var toggleable = document.querySelectorAll(".md-typeset .sv-toggleable");
    var existing = document.getElementById("sv-notes-toggle");
    if (existing) existing.remove();
    if (!toggleable.length) return;

    // fresh page load always starts in the "respect each div's own
    // initial state" mode — neither forced-show nor forced-hide yet.
    document.body.classList.remove("sv-force-show", "sv-force-hide");

    var initiallyAllShown =
      document.querySelectorAll(".md-typeset .sv-toggleable.sv-hidden-default").length === 0;

    function currentlyShown() {
      if (document.body.classList.contains("sv-force-show")) return true;
      if (document.body.classList.contains("sv-force-hide")) return false;
      return initiallyAllShown; // natural state — already "shown" iff nothing was hidden to begin with
    }

    var btn = document.createElement("button");
    btn.id = "sv-notes-toggle";
    btn.type = "button";

    function render() {
      btn.textContent = currentlyShown() ? "टिप्पणीः गोपय" : "टिप्पणीः दर्शय";
    }

    btn.addEventListener("click", function () {
      var shown = currentlyShown();
      document.body.classList.remove("sv-force-show", "sv-force-hide");
      document.body.classList.add(shown ? "sv-force-hide" : "sv-force-show");
      render();
      syncToc();
    });

    render();
    syncToc();
    document.body.appendChild(btn);
  }

  if (window.document$) {
    // mkdocs-material instant navigation: document$ emits on every page swap
    window.document$.subscribe(setup);
  } else {
    document.addEventListener("DOMContentLoaded", setup);
  }
})();
