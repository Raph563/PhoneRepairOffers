const tabs = Array.from(document.querySelectorAll('.tab'));
const panels = {
  search: document.getElementById('panel-search'),
  favorites: document.getElementById('panel-favorites'),
};

const searchForm = document.getElementById('search-form');
const searchResults = document.getElementById('search-results');
const favoritesResults = document.getElementById('favorites-results');
const statusEl = document.getElementById('search-status');
const metaEl = document.getElementById('result-meta');

let lastOffers = [];
let favoriteIdsByOffer = new Map();

function setTab(next) {
  tabs.forEach((tab) => tab.classList.toggle('is-active', tab.dataset.tab === next));
  Object.entries(panels).forEach(([name, panel]) => panel.classList.toggle('is-active', name === next));
  if (next === 'favorites') {
    loadFavorites();
  }
}

tabs.forEach((tab) => tab.addEventListener('click', () => setTab(tab.dataset.tab)));

function euro(value) {
  return new Intl.NumberFormat('fr-FR', { style: 'currency', currency: 'EUR' }).format(Number(value || 0));
}

function sourceBadge(source) {
  return source === 'ebay' ? 'eBay' : 'Leboncoin';
}

function resolveOfferImageSrc(offer) {
  if (offer && offer.imageUrl) {
    return `/api/image-proxy?url=${encodeURIComponent(offer.imageUrl)}`;
  }
  return '/static/placeholder-offer.svg';
}

function cardTemplate(offer, isFavorite, favoriteId = null) {
  const imageSrc = resolveOfferImageSrc(offer);
  const image = `<img src="${imageSrc}" alt="${offer.title}" loading="lazy" onerror="this.onerror=null;this.src='/static/placeholder-offer.svg';">`;

  const recentTag = offer.source === 'ebay' && offer.isRecentlyAdded === true
    ? '<span class="badge badge-recent">Recemment ajoute</span>'
    : '';

  return `
    <article class="offer-card" data-offer-id="${offer.id}" data-favorite-id="${favoriteId || ''}">
      <div class="thumb">${image}</div>
      <div class="body">
        <div class="line top">
          <div class="badges">
            <span class="badge">${sourceBadge(offer.source)}</span>
            ${recentTag}
          </div>
          <strong>${euro(offer.totalEur)}</strong>
        </div>
        <h3><a href="${offer.url}" target="_blank" rel="noopener noreferrer">${offer.title}</a></h3>
        <p class="muted">
          Prix: ${euro(offer.priceEur)} | Livraison: ${euro(offer.shippingEur)}
          ${offer.location ? `| ${offer.location}` : ''}
        </p>
        <div class="line actions">
          <button class="favorite-toggle" data-action="toggle-fav">${isFavorite ? '★ Favori' : '☆ Favori'}</button>
          <span class="muted small">${offer.queryType === 'replacement_screen' ? 'Ecran remplacement' : 'Telephone sans ecran'}</span>
        </div>
      </div>
    </article>
  `;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const txt = await response.text();
    throw new Error(txt || `HTTP ${response.status}`);
  }
  return response.json();
}

function attachOfferPayloads(container, offers) {
  const offersById = new Map(offers.map((offer) => [offer.id, offer]));
  Array.from(container.querySelectorAll('.offer-card')).forEach((card) => {
    const offer = offersById.get(card.dataset.offerId);
    if (offer) {
      card.dataset.offerJson = JSON.stringify(offer);
    }
  });
}

async function loadFavorites() {
  const data = await api('/api/favorites');
  favoriteIdsByOffer = new Map();
  data.favorites.forEach((row) => {
    const key = `${row.offer.source}|${row.offer.sourceOfferId}`;
    favoriteIdsByOffer.set(key, row.favoriteId);
  });

  if (!data.favorites.length) {
    favoritesResults.innerHTML = '<p class="muted">Aucun favori pour le moment.</p>';
    return;
  }

  const favoriteOffers = data.favorites.map((row) => row.offer);
  favoritesResults.innerHTML = data.favorites
    .map((row) => cardTemplate(row.offer, true, row.favoriteId))
    .join('');
  attachOfferPayloads(favoritesResults, favoriteOffers);
}

async function toggleFavorite(offer) {
  return api('/api/favorites/toggle', {
    method: 'POST',
    body: JSON.stringify({ source: offer.source, sourceOfferId: offer.sourceOfferId, offer }),
  });
}

function parseSources() {
  const sources = [];
  if (document.getElementById('source-lbc').checked) sources.push('leboncoin');
  if (document.getElementById('source-ebay').checked) sources.push('ebay');
  return sources;
}

searchForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  statusEl.textContent = 'Recherche en cours...';
  searchResults.innerHTML = '';
  metaEl.textContent = '';

  try {
    const sources = parseSources();
    if (!sources.length) {
      throw new Error('Selectionne au moins une source');
    }

    const payload = {
      brand: document.getElementById('brand').value.trim(),
      model: document.getElementById('model').value.trim(),
      partType: document.getElementById('partType').value,
      category: document.getElementById('category').value,
      sources,
      forceRefresh: document.getElementById('force-refresh').checked,
    };

    const maxPrice = document.getElementById('maxPrice').value.trim();
    if (maxPrice) payload.maxPriceEur = Number(maxPrice);

    const data = await api('/api/search', {
      method: 'POST',
      body: JSON.stringify(payload),
    });

    await loadFavorites();

    lastOffers = data.offers || [];
    metaEl.textContent = `${lastOffers.length} offres ${data.cached ? '(cache)' : '(live)'}`;

    if (!lastOffers.length) {
      searchResults.innerHTML = '<p class="muted">Aucune offre trouvee pour cette recherche.</p>';
    } else {
      searchResults.innerHTML = lastOffers
        .map((offer) => {
          const key = `${offer.source}|${offer.sourceOfferId}`;
          return cardTemplate(offer, favoriteIdsByOffer.has(key), favoriteIdsByOffer.get(key));
        })
        .join('');
      attachOfferPayloads(searchResults, lastOffers);
    }

    statusEl.textContent = 'Recherche terminee.';
    if (data.providerErrors && Object.keys(data.providerErrors).length) {
      statusEl.textContent += ` Erreurs source: ${JSON.stringify(data.providerErrors)}`;
    }
  } catch (err) {
    statusEl.textContent = `Erreur: ${err.message}`;
  }
});

document.body.addEventListener('click', async (event) => {
  const btn = event.target.closest('[data-action="toggle-fav"]');
  if (!btn) return;

  const card = btn.closest('.offer-card');
  if (!card || !card.dataset.offerJson) return;

  try {
    const offer = JSON.parse(card.dataset.offerJson);
    const result = await toggleFavorite(offer);
    btn.textContent = result.isFavorite ? '★ Favori' : '☆ Favori';
    await loadFavorites();
  } catch (err) {
    statusEl.textContent = `Erreur favoris: ${err.message}`;
  }
});

document.getElementById('refresh-favorites').addEventListener('click', loadFavorites);
loadFavorites().catch(() => {
  favoritesResults.innerHTML = '<p class="muted">Impossible de charger les favoris.</p>';
});
