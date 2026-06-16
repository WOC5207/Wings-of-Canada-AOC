/* Airport autocomplete for ICAO inputs.
 *
 * Any <input data-airport-ac> gets a dropdown of matching airports
 * (ICAO — name, city · UTC zone), fed by /dispatch/airports. Choosing one
 * fills the input with the ICAO code and fires change/input so dependent
 * logic (e.g. the route estimate) updates.
 */
(function () {
  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function init(input) {
    input.setAttribute("autocomplete", "off");

    var wrap = document.createElement("div");
    wrap.className = "ac-field";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var menu = document.createElement("div");
    menu.className = "ac-menu";
    menu.hidden = true;
    wrap.appendChild(menu);

    var active = -1;
    var timer = null;

    function close() { menu.hidden = true; active = -1; }

    function choose(a) {
      input.value = a.icao;
      close();
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function render(results) {
      menu.innerHTML = "";
      if (!results.length) { close(); return; }
      results.forEach(function (a) {
        var meta = escapeHtml(a.city || "") +
          (a.utc_label ? (a.city ? " · " : "") + a.utc_label : "");
        var el = document.createElement("div");
        el.className = "ac-item";
        el.innerHTML =
          '<span class="ac-icao mono">' + escapeHtml(a.icao) + "</span>" +
          '<span class="ac-name">' + escapeHtml(a.name) + "</span>" +
          '<span class="ac-meta">' + meta + "</span>";
        el._airport = a;
        el.addEventListener("mousedown", function (e) {
          e.preventDefault();  // keep focus; fire before blur closes the menu
          choose(a);
        });
        menu.appendChild(el);
      });
      active = -1;
      menu.hidden = false;
    }

    function query() {
      var q = input.value.trim();
      if (!q) { close(); return; }
      fetch("/dispatch/airports?q=" + encodeURIComponent(q))
        .then(function (r) { return r.ok ? r.json() : { results: [] }; })
        .then(function (j) { render(j.results || []); })
        .catch(function () { close(); });
    }

    function highlight() {
      menu.querySelectorAll(".ac-item").forEach(function (o, i) {
        o.classList.toggle("active", i === active);
      });
    }

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(query, 150);
    });
    input.addEventListener("focus", function () {
      if (input.value.trim()) query();
    });
    input.addEventListener("blur", function () { setTimeout(close, 120); });
    input.addEventListener("keydown", function (e) {
      if (menu.hidden) return;
      var opts = menu.querySelectorAll(".ac-item");
      if (e.key === "ArrowDown") {
        e.preventDefault();
        active = Math.min(active + 1, opts.length - 1); highlight();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        active = Math.max(active - 1, 0); highlight();
      } else if (e.key === "Enter" && active >= 0) {
        e.preventDefault();
        choose(opts[active]._airport);
      } else if (e.key === "Escape") {
        close();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("input[data-airport-ac]").forEach(init);
  });
})();
