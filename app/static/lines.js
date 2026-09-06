/* Live document-line editor: keeps the on-screen totals in step with what the
   server will calculate. The server always recalculates on save — this is only
   to show the user what they are building. */
(function () {
  "use strict";

  function num(v) {
    var n = parseFloat(String(v || "0").replace(/,/g, "").trim());
    return isNaN(n) ? 0 : n;
  }

  function round2(n) {
    return Math.round((n + Number.EPSILON) * 100) / 100;
  }

  function fmt(n) {
    var neg = n < 0;
    var s = Math.abs(round2(n)).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return (neg ? "(" : "") + SYMBOL + s + (neg ? ")" : "");
  }

  var SYMBOL = document.body.getAttribute("data-symbol") || "₦";

  function taxRate(select) {
    if (!select) return 0;
    var opt = select.options[select.selectedIndex];
    return opt ? num(opt.getAttribute("data-rate")) : 0;
  }

  function whtRate() {
    var sel = document.getElementById("wht_code_id");
    if (!sel) return 0;
    var opt = sel.options[sel.selectedIndex];
    return opt ? num(opt.getAttribute("data-rate")) : 0;
  }

  function recalc() {
    var subtotal = 0, vat = 0, discount = 0;
    document.querySelectorAll("tr.line-row").forEach(function (row) {
      var qty = num(row.querySelector(".l-qty").value);
      var price = num(row.querySelector(".l-price").value);
      var disc = num(row.querySelector(".l-disc") ? row.querySelector(".l-disc").value : 0);
      var gross = round2(qty * price);
      var net = round2(gross * (100 - disc) / 100);
      var t = round2(net * taxRate(row.querySelector(".l-tax")) / 100);
      discount += gross - net;
      subtotal += net;
      vat += t;
      var cell = row.querySelector(".l-total");
      if (cell) cell.textContent = net ? fmt(net) : "";
    });

    var wht = round2(subtotal * whtRate() / 100);
    var total = round2(subtotal + vat);

    set("t-subtotal", fmt(subtotal));
    set("t-discount", fmt(discount));
    set("t-vat", fmt(vat));
    set("t-total", fmt(total));
    set("t-wht", fmt(wht));
    set("t-net", fmt(total - wht));
    var dr = document.getElementById("discount-row");
    if (dr) dr.style.display = discount ? "" : "none";
    var wr = document.getElementById("wht-row");
    if (wr) wr.style.display = wht ? "" : "none";
  }

  function set(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function onItemChange(select) {
    var row = select.closest("tr");
    var opt = select.options[select.selectedIndex];
    if (!opt || !opt.value) return;
    var desc = row.querySelector(".l-desc");
    var price = row.querySelector(".l-price");
    var tax = row.querySelector(".l-tax");
    var account = row.querySelector(".l-account");
    if (desc && !desc.value) desc.value = opt.getAttribute("data-name") || opt.text;
    if (price && !num(price.value)) price.value = opt.getAttribute("data-price") || "";
    var taxId = opt.getAttribute("data-tax");
    if (tax && taxId) tax.value = taxId;
    var accId = opt.getAttribute("data-account");
    if (account && accId) account.value = accId;
    recalc();
  }

  function addRow() {
    var body = document.getElementById("line-body");
    var last = body.querySelector("tr.line-row:last-child");
    var clone = last.cloneNode(true);
    clone.querySelectorAll("input").forEach(function (i) {
      if (i.classList.contains("l-qty")) i.value = "1";
      else if (i.classList.contains("l-disc")) i.value = "0";
      else i.value = "";
    });
    clone.querySelectorAll("select").forEach(function (s) { s.selectedIndex = 0; });
    var t = clone.querySelector(".l-total");
    if (t) t.textContent = "";
    body.appendChild(clone);
    bind(clone);
    recalc();
  }

  function removeRow(btn) {
    var body = document.getElementById("line-body");
    var rows = body.querySelectorAll("tr.line-row");
    if (rows.length <= 1) {
      btn.closest("tr").querySelectorAll("input").forEach(function (i) { i.value = ""; });
      btn.closest("tr").querySelectorAll("select").forEach(function (s) { s.selectedIndex = 0; });
    } else {
      btn.closest("tr").remove();
    }
    recalc();
  }

  function bind(scope) {
    scope.querySelectorAll(".l-qty, .l-price, .l-disc").forEach(function (i) {
      i.addEventListener("input", recalc);
    });
    scope.querySelectorAll(".l-tax").forEach(function (s) {
      s.addEventListener("change", recalc);
    });
    scope.querySelectorAll(".l-item").forEach(function (s) {
      s.addEventListener("change", function () { onItemChange(s); });
    });
    scope.querySelectorAll(".rm-row").forEach(function (b) {
      b.addEventListener("click", function (e) { e.preventDefault(); removeRow(b); });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("line-body")) return;
    bind(document);
    var add = document.getElementById("add-row");
    if (add) add.addEventListener("click", function (e) { e.preventDefault(); addRow(); });
    var wht = document.getElementById("wht_code_id");
    if (wht) wht.addEventListener("change", recalc);
    recalc();
  });
})();
