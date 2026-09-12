// 数値・文字列の表示用ユーティリティ
window.fmt = (function () {
  function num(value, digits = 0) {
    if (value === null || value === undefined || Number.isNaN(value)) return "—";
    return Number(value).toLocaleString("ja-JP", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  }

  function priceDigits(currency) {
    return currency === "JPY" ? 1 : 2;
  }

  function price(value, currency) {
    return num(value, priceDigits(currency));
  }

  function volume(value) {
    if (value === null || value === undefined) return "—";
    if (Math.abs(value) >= 1e8) return num(value / 1e8, 2) + "億";
    if (Math.abs(value) >= 1e4) return num(value / 1e4, 1) + "万";
    return num(value);
  }

  function signed(value, digits = 1, suffix = "") {
    if (value === null || value === undefined) return "—";
    const sign = value > 0 ? "+" : "";
    return sign + num(value, digits) + suffix;
  }

  function date(value) {
    return value ? value.replaceAll("-", "/") : "—";
  }

  function escape(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  return { num, price, priceDigits, volume, signed, date, escape };
})();
