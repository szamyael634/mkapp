// Live product search with image and name suggestions
let searchInput = null;
let searchBox = null;

function createSearchBox() {
  searchBox = document.createElement('div');
  searchBox.id = 'search-suggestions';
  searchBox.className = 'bg-white border border-gray-200 rounded-b-lg shadow-lg z-50 max-h-80 overflow-y-auto';
  searchBox.style.display = 'none';
  document.body.appendChild(searchBox);
}

function positionSearchBox() {
  if (!searchInput || !searchBox) return;
  const rect = searchInput.getBoundingClientRect();
  searchBox.style.position = 'absolute';
  searchBox.style.left = rect.left + window.scrollX + 'px';
  searchBox.style.top = (rect.bottom + window.scrollY) + 'px';
  searchBox.style.width = rect.width + 'px';
}

function showSuggestions(items) {
  if (!searchBox) createSearchBox();
  positionSearchBox();
  if (!items.length) {
    searchBox.style.display = 'none';
    return;
  }
  searchBox.innerHTML = items.map(p => `
    <a href="/product/${p.id}" class="flex items-center gap-3 px-4 py-2 hover:bg-amber-50 transition">
      <img src="${p.image_url}" alt="${p.name}" class="w-12 h-12 object-cover rounded border border-gray-200" />
      <span class="text-gray-800 font-medium">${p.name}</span>
    </a>
  `).join('');
  searchBox.style.display = 'block';
}

function hideSuggestions() {
  if (searchBox) searchBox.style.display = 'none';
}

document.addEventListener('DOMContentLoaded', () => {
  searchInput = document.getElementById('main-search-input');
  if (!searchInput) return;
  createSearchBox();
  let lastQuery = '';
  searchInput.addEventListener('input', async (e) => {
    const q = e.target.value.trim();
    if (q.length < 1) return hideSuggestions();
    if (q === lastQuery) return;
    lastQuery = q;
    try {
      const res = await fetch(`/search_products?q=${encodeURIComponent(q)}`);
      if (!res.ok) throw new Error('Failed');
      const data = await res.json();
      showSuggestions(data);
    } catch {
      hideSuggestions();
    }
  });
  
  // Function to perform search
  const performSearch = () => {
    const q = searchInput.value.trim();
    if (!q) return;
    hideSuggestions();
    window.location.href = `/products/search?q=${encodeURIComponent(q)}`;
  };
  
  // Pressing Enter should navigate to the products page and show all matching products
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      performSearch();
    }
  });
  
  // Search button click handler
  const searchBtn = document.getElementById('main-search-btn');
  if (searchBtn) {
    searchBtn.addEventListener('click', performSearch);
  }
  
  searchInput.addEventListener('focus', () => {
    if (searchBox && searchBox.innerHTML) {
      positionSearchBox();
      searchBox.style.display = 'block';
    }
  });
  window.addEventListener('resize', positionSearchBox);
  window.addEventListener('scroll', positionSearchBox, true);
  document.addEventListener('click', (e) => {
    if (!searchBox.contains(e.target) && e.target !== searchInput) hideSuggestions();
  });
});
