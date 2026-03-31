let stockSearchResults = [];
let stockSearchActiveIndex = -1;
let stockSearchDebounce = null;

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function getStockSearchElements() {
  const input =
    document.getElementById("stockSearch") ||
    document.getElementById("searchInput") ||
    document.querySelector("input");

  const dropdown =
    document.getElementById("stockSearchDropdown") ||
    document.getElementById("searchDropdown");

  return { input, dropdown };
}

async function fetchStockSuggestions(query) {
  const res = await fetch(`/api/search?q=${encodeURIComponent(query)}&limit=8`);
  return await res.json();
}

function renderStockDropdown(results) {
  const { dropdown } = getStockSearchElements();
  if (!dropdown) return;

  stockSearchResults = results || [];
  stockSearchActiveIndex = -1;

  if (!stockSearchResults.length) {
    dropdown.innerHTML = `<div style="padding:10px;">No stock found</div>`;
    dropdown.style.display = "block";
    return;
  }

  dropdown.innerHTML = stockSearchResults.map((item, index) => `
    <div class="stock-item" data-index="${index}" data-symbol="${item.symbol}"
      style="padding:10px; cursor:pointer;">
      <b>${item.symbol}</b><br>
      <small>${item.name}</small>
    </div>
  `).join("");

  dropdown.style.display = "block";

  dropdown.querySelectorAll(".stock-item").forEach(el => {
    el.onclick = () => selectStock(el.dataset.symbol);
  });
}

function hideDropdown() {
  const { dropdown } = getStockSearchElements();
  if (dropdown) dropdown.style.display = "none";
}

function selectStock(symbol) {
  const { input } = getStockSearchElements();
  input.value = symbol;
  hideDropdown();

  if (typeof openStock === "function") openStock(symbol);
}

async function handleInput(e) {
  const q = e.target.value.trim();

  if (stockSearchDebounce) clearTimeout(stockSearchDebounce);

  if (!q) {
    hideDropdown();
    return;
  }

  stockSearchDebounce = setTimeout(async () => {
    try {
      const data = await fetchStockSuggestions(q);
      renderStockDropdown(data.results);
    } catch {
      renderStockDropdown([]);
    }
  }, 200);
}

function handleKey(e) {
  if (e.key === "Enter") {
    if (stockSearchResults.length > 0) {
      selectStock(stockSearchResults[0].symbol);
    }
  }
}

function initStockSearch() {
  const { input } = getStockSearchElements();
  if (!input) return;

  input.addEventListener("input", handleInput);
  input.addEventListener("keydown", handleKey);
}

document.addEventListener("DOMContentLoaded", initStockSearch);
