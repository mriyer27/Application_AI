// job-agent dashboard niceties. Everything works without JavaScript; this only
// adds copy-to-clipboard buttons for draft texts (they stay hidden without JS).
"use strict";
(function () {
  function copyFrom(button) {
    var target = document.getElementById(button.getAttribute("data-copy-target"));
    if (!target) { return; }
    var text = target.textContent || "";
    var done = function () {
      var label = button.textContent;
      button.textContent = "Copied";
      window.setTimeout(function () { button.textContent = label; }, 1500);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { selectText(target); });
    } else {
      selectText(target);
    }
  }

  function selectText(node) {
    var range = document.createRange();
    range.selectNodeContents(node);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var buttons = document.querySelectorAll("button[data-copy-target]");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].hidden = false;
      buttons[i].addEventListener("click", function (event) { copyFrom(event.currentTarget); });
    }
  });
})();
