// Adds a floating button that shows/hides every "hideable" content block
// on the current page: <div class="notes">, the kavya-verse commentary
// sections (anvaya/padartha/vyutpatti/tika/alankara/bhavartha/vyakarana/
// kosha), and any other block the source explicitly marked with
// toggle-hide="true" — generate_indices.py tags all of these with the
// shared class "sv-hideable" at build time. Hidden by default; clicking
// the button adds .sv-show-notes to <body> to reveal them. Runs on every
// page load, including MkDocs Material's instant-navigation swaps.

(function () {
  function setup() {
    var hideable = document.querySelectorAll(".md-typeset .sv-hideable");
    var existing = document.getElementById("sv-notes-toggle");
    if (existing) existing.remove();
    if (!hideable.length) return;

    document.body.classList.remove("sv-show-notes"); // hidden by default on every page load

    var btn = document.createElement("button");
    btn.id = "sv-notes-toggle";
    btn.type = "button";

    function render() {
      var shown = document.body.classList.contains("sv-show-notes");
      btn.textContent = shown ? "टिप्पणीः गोपय" : "टिप्पणीः दर्शय";
    }

    btn.addEventListener("click", function () {
      document.body.classList.toggle("sv-show-notes");
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
