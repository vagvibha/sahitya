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
// Runs on every page load, including MkDocs Material's instant-navigation
// swaps.

(function () {
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
    });

    render();
    document.body.appendChild(btn);
  }

  if (window.document$) {
    // mkdocs-material instant navigation: document$ emits on every page swap
    window.document$.subscribe(setup);
  } else {
    document.addEventListener("DOMContentLoaded", setup);
  }
})();
