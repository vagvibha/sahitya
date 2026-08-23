// Adds a floating button that toggles visibility of every
// <div class="notes"> block on the current page. Runs on every page load,
// including MkDocs Material's instant-navigation swaps.

(function () {
  function setup() {
    var notes = document.querySelectorAll(".md-typeset .notes");
    var existing = document.getElementById("sv-notes-toggle");
    if (existing) existing.remove();
    if (!notes.length) return;

    var btn = document.createElement("button");
    btn.id = "sv-notes-toggle";
    btn.type = "button";

    function render() {
      var hidden = document.body.classList.contains("sv-hide-notes");
      btn.textContent = hidden ? "टिप्पणीः दर्शय" : "टिप्पणीः गोपय";
    }

    btn.addEventListener("click", function () {
      document.body.classList.toggle("sv-hide-notes");
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
