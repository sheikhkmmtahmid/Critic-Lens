// app state - keeping it simple, just a plain object
var state = {
  currentSection: 'overview',
  selectedFilm: null,
  leaderboardLoaded: false,
  overviewLoaded: false,
  searchTimeout: null,
  searchDropdownOpen: false,

  // browse section state - tracks current page and results metadata
  browse: {
    page: 1,
    totalPages: 0,
    total: 0,
    loading: false,
  },
};

var API = '';  // relative - backend serves frontend so same origin

// init
document.addEventListener('DOMContentLoaded', function() {
  setupNavigation();
  setupSearch();
  setupBrowse();
  loadOverview();
  // restore the section the user was on before they refreshed
  var initial = sectionFromHash();
  if (initial !== 'overview') {
    switchSection(initial, true);
  }
  // close film modal on Escape
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeFilmModal();
  });
});


// navigation
var VALID_SECTIONS = ['overview', 'search', 'analyser', 'leaderboard'];

function sectionFromHash() {
  var hash = (window.location.hash || '').replace('#', '');
  return VALID_SECTIONS.indexOf(hash) !== -1 ? hash : 'overview';
}

function setupNavigation() {
  var navLinks = document.querySelectorAll('.nav-link');
  navLinks.forEach(function(link) {
    link.addEventListener('click', function() {
      var section = link.getAttribute('data-section');
      switchSection(section);
    });
  });

  // browser back/forward button support
  window.addEventListener('hashchange', function() {
    switchSection(sectionFromHash(), true);
  });
}

// skipHash=true when called from hashchange or initial load to avoid double-writing the hash
function switchSection(sectionName, skipHash) {
  // hide all sections
  document.querySelectorAll('.section').forEach(function(s) {
    s.classList.remove('active');
  });

  // show the target section
  var target = document.getElementById('section-' + sectionName);
  if (target) {
    target.classList.add('active');
  }

  // update nav active state
  document.querySelectorAll('.nav-link').forEach(function(link) {
    link.classList.remove('active');
    if (link.getAttribute('data-section') === sectionName) {
      link.classList.add('active');
    }
  });

  state.currentSection = sectionName;

  // persist in URL so refresh restores the right section
  if (!skipHash) {
    window.location.hash = sectionName === 'overview' ? '' : sectionName;
  }

  // smooth scroll to top
  window.scrollTo({ top: 0, behavior: 'smooth' });

  // lazy-load the leaderboard only when you navigate to it
  if (sectionName === 'leaderboard' && !state.leaderboardLoaded) {
    loadLeaderboard();
  }
}


//error handling
function showError(msg) {
  var banner = document.getElementById('error-banner');
  var msgEl = document.getElementById('error-message');
  msgEl.textContent = msg;
  banner.classList.remove('hidden');
  // auto-dismiss after 6 seconds
  setTimeout(function() { dismissError(); }, 6000);
}

function dismissError() {
  var banner = document.getElementById('error-banner');
  banner.classList.add('hidden');
}


// overview
async function loadOverview() {
  try {
    var data = await apiFetch('/api/stats/overview');
    renderOverview(data);
    state.overviewLoaded = true;
  } catch (e) {
    showError('Could not load overview stats - check the console');
    console.error(e);
    // replace skeleton cards with error state
    var container = document.getElementById('overview-stats');
    container.innerHTML = '<p style="color:var(--text-muted);font-size:13px;">Failed to load stats.</p>';
  }
}

function renderOverview(data) {
  var container = document.getElementById('overview-stats');

  var avgDiv = data.avg_divergence != null ? data.avg_divergence.toFixed(1) : 'N/A';
  var sign = data.avg_divergence > 0 ? '+' : '';

  container.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Total Films</div>
      <div class="stat-value">${data.total_films != null ? data.total_films.toLocaleString() : 'N/A'}</div>
      <div class="stat-subvalue">in dataset</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg Divergence</div>
      <div class="stat-value">${sign}${avgDiv}</div>
      <div class="stat-subvalue">critic - audience</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Most Critic-Favoured</div>
      <div class="stat-value" style="font-size:16px;line-height:1.4;">${data.most_divergent_critic_film || 'N/A'}</div>
      <div class="stat-subvalue">critics vs audiences</div>
    </div>
    <div class="stat-card blue-top">
      <div class="stat-label">Most Audience-Favoured</div>
      <div class="stat-value" style="font-size:16px;line-height:1.4;">${data.most_divergent_audience_film || 'N/A'}</div>
      <div class="stat-subvalue">audiences vs critics</div>
    </div>
  `;

  if (data.genre_divergence_averages && data.genre_divergence_averages.length > 0) {
    drawGenreChart(data.genre_divergence_averages);
  }
}

function drawGenreChart(genreData) {
  var canvas = document.getElementById('genre-chart');
  if (!canvas) return;

  // set canvas dimensions based on container width
  var container = canvas.parentElement;
  canvas.width = container.clientWidth - 48;
  canvas.height = 320;

  var ctx = canvas.getContext('2d');

  var w = canvas.width;
  var h = canvas.height;
  var paddingLeft = 160;
  var paddingRight = 40;
  var paddingTop = 20;
  var paddingBottom = 20;
  var barHeight = 28;
  var barGap = 12;

  var maxAbs = 0;
  genreData.forEach(function(g) {
    if (Math.abs(g.avg_divergence) > maxAbs) maxAbs = Math.abs(g.avg_divergence);
  });
  if (maxAbs === 0) maxAbs = 1;

  var chartW = w - paddingLeft - paddingRight;
  var centreX = paddingLeft + chartW / 2;

  ctx.clearRect(0, 0, w, h);

  // draw centre line
  ctx.beginPath();
  ctx.strokeStyle = 'rgba(48,54,61,0.8)';
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.moveTo(centreX, paddingTop);
  ctx.lineTo(centreX, h - paddingBottom);
  ctx.stroke();
  ctx.setLineDash([]);

  genreData.forEach(function(item, i) {
    var y = paddingTop + i * (barHeight + barGap);
    var div = item.avg_divergence;
    var barW = Math.abs(div) / maxAbs * (chartW / 2 - 10);

    // draw genre label on the left
    ctx.fillStyle = '#8b949e';
    ctx.font = '13px Inter, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText(item.genre, paddingLeft - 10, y + barHeight / 2);

    // draw bar
    if (div >= 0) {
      // critics-favoured bar goes right
      ctx.fillStyle = 'rgba(232,93,4,0.75)';
      ctx.beginPath();
      ctx.roundRect(centreX + 2, y, barW, barHeight, 3);
      ctx.fill();

      // score label
      ctx.fillStyle = '#e85d04';
      ctx.font = '600 12px JetBrains Mono, monospace';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillText('+' + div.toFixed(1), centreX + barW + 6, y + barHeight / 2);
    } else {
      // audience-favoured bar goes left
      ctx.fillStyle = 'rgba(111,187,248,0.75)';
      ctx.beginPath();
      ctx.roundRect(centreX - barW - 2, y, barW, barHeight, 3);
      ctx.fill();

      ctx.fillStyle = '#6fbbf8';
      ctx.font = '600 12px JetBrains Mono, monospace';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(div.toFixed(1), centreX - barW - 6, y + barHeight / 2);
    }
  });
}


// film search
function setupSearch() {
  var input = document.getElementById('film-search-input');
  var dropdown = document.getElementById('search-dropdown');

  input.addEventListener('input', function() {
    var q = input.value.trim();

    // debouncing this because it was absolutely hammering the API on every keypress
    clearTimeout(state.searchTimeout);

    if (q.length < 2) {
      closeDropdown();
      return;
    }

    state.searchTimeout = setTimeout(function() {
      fetchSearchResults(q);
    }, 300);
  });

  // close dropdown when clicking outside
  document.addEventListener('click', function(e) {
    if (!e.target.closest('.search-input-wrap')) {
      closeDropdown();
    }
  });

  input.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeDropdown();
  });
}

async function fetchSearchResults(query) {
  try {
    var results = await apiFetch('/api/films/search?q=' + encodeURIComponent(query));
    renderDropdown(results);
  } catch (e) {
    showError('Search failed - check the console');
    console.error(e);
  }
}

function renderDropdown(results) {
  var dropdown = document.getElementById('search-dropdown');

  if (!results || results.length === 0) {
    dropdown.innerHTML = '<div class="dropdown-item"><span class="dropdown-title" style="color:var(--text-muted)">No films found</span></div>';
    dropdown.classList.remove('hidden');
    return;
  }

  dropdown.innerHTML = results.map(function(film) {
    var rt = film.tomatometer_rating != null ? film.tomatometer_rating + '%' : '--';
    var aud = film.audience_rating != null ? film.audience_rating + '%' : '--';
    return `
      <div class="dropdown-item" data-link="${escapeHtml(film.rotten_tomatoes_link || '')}">
        <span class="dropdown-title">${escapeHtml(film.movie_title || 'Unknown')}</span>
        <div class="dropdown-badges">
          <span class="score-badge orange" title="Tomatometer">${rt}</span>
          <span class="score-badge blue" title="Audience">${aud}</span>
        </div>
      </div>
    `;
  }).join('');

  // wire up click handlers
  dropdown.querySelectorAll('.dropdown-item').forEach(function(item) {
    item.addEventListener('click', function() {
      var link = item.getAttribute('data-link');
      var title = item.querySelector('.dropdown-title').textContent;
      document.getElementById('film-search-input').value = title;
      closeDropdown();
      if (link) loadFilm(link);
    });
  });

  dropdown.classList.remove('hidden');
  state.searchDropdownOpen = true;
}

function closeDropdown() {
  var dropdown = document.getElementById('search-dropdown');
  dropdown.classList.add('hidden');
  state.searchDropdownOpen = false;
}


// film modal open / close
function openFilmModal() {
  var modal = document.getElementById('film-modal');
  if (!modal) return;
  modal.classList.remove('hidden');
  modal.scrollTop = 0;
  document.body.style.overflow = 'hidden';
}

function closeFilmModal() {
  var modal = document.getElementById('film-modal');
  if (!modal) return;
  modal.classList.add('hidden');
  document.body.style.overflow = '';
}

//  film detail
async function loadFilm(rtLink) {
  var filmEl    = document.getElementById('film-modal-film');
  var explainEl = document.getElementById('film-modal-explain');
  filmEl.innerHTML = '<div class="loading-spinner">Loading film data...</div>';
  if (explainEl) explainEl.innerHTML = '';

  openFilmModal();
  state.selectedFilm = rtLink;

  try {
    // all three in parallel - explain uses only cached DB data so it's fast
    var [divergence, temporal, explanation] = await Promise.all([
      apiFetch('/api/films/' + encodeURIComponent(rtLink) + '/divergence'),
      apiFetch('/api/films/' + encodeURIComponent(rtLink) + '/temporal'),
      apiFetch('/api/films/' + encodeURIComponent(rtLink) + '/explain').catch(function() { return null; }),
    ]);

    renderFilmResult(divergence, temporal);
    if (explanation && explainEl) {
      renderGapExplanation(explanation, explainEl);
    }
  } catch (e) {
    showError('Could not load film data - check the console');
    console.error(e);
    filmEl.innerHTML = '<p style="color:var(--danger);font-size:13px;padding:20px;">Failed to load film data.</p>';
  }
}

function renderFilmResult(film, temporal) {
  var resultEl = document.getElementById('film-modal-film');

  var genres = '';
  if (film.genres) {
    genres = film.genres.split(',').map(function(g) {
      return '<span class="genre-pill">' + escapeHtml(g.trim()) + '</span>';
    }).join('');
  }

  var year = film.release_year || (film.original_release_date ? film.original_release_date.slice(0, 4) : '');
  var runtime = film.runtime ? film.runtime + ' min' : '';
  var director = film.directors || '';

  var divScore = film.divergence_score != null ? film.divergence_score.toFixed(1) : 'N/A';
  var divLabel = film.divergence_label || 'Aligned';

  var badgeClass = 'aligned';
  var badgePrefix = '';
  if (divLabel.indexOf('Critics') !== -1) {
    badgeClass = 'critics';
    badgePrefix = '+';
  } else if (divLabel.indexOf('Audiences') !== -1) {
    badgeClass = 'audience';
  }

  var consensus = film.critics_consensus
    ? '<blockquote class="consensus-block">' + escapeHtml(film.critics_consensus) + '</blockquote>'
    : '';

  var reviewsHtml = renderReviewCards(film.critic_reviews || []);
  var temporalHtml = renderTemporalChart(temporal);

  var rtRating  = film.tomatometer_rating != null ? Math.round(film.tomatometer_rating) : 0;
  var audRating = film.audience_rating    != null ? Math.round(film.audience_rating)    : 0;

  // backfill films use IMDB rating × 10 as audience score because RT Audience Score
  // is not available via any free API for post-2020 films
  var isImdbProxy   = film.audience_score_source === 'imdb';
  var audLabel      = isImdbProxy ? 'Audience (IMDB)' : 'Audience Score';
  var imdbProxyNote = isImdbProxy
    ? '<p class="imdb-proxy-note">* Audience score is IMDB rating × 10 — RT Audience Score is unavailable for films not in the Kaggle dataset</p>'
    : '';

  resultEl.innerHTML = `
    <div class="film-header">
      <div class="film-title">${escapeHtml(film.movie_title || 'Unknown Film')}</div>
      <div class="film-meta">
        ${year ? '<span class="film-meta-item">' + year + '</span>' : ''}
        ${runtime ? '<span class="film-meta-item">' + escapeHtml(runtime) + '</span>' : ''}
        ${director ? '<span class="film-meta-item">dir. ' + escapeHtml(director) + '</span>' : ''}
      </div>
      <div class="genre-pills">${genres}</div>
    </div>

    <div class="scores-row">
      ${makeScoreCircle(rtRating, 'orange', 'Tomatometer')}
      ${makeScoreCircle(audRating, 'blue', audLabel)}
    </div>
    ${imdbProxyNote}

    <div>
      <span class="divergence-badge ${badgeClass}">
        Divergence: ${badgePrefix}${divScore}
        <span style="font-weight:400;opacity:0.8;font-size:11px;">${escapeHtml(divLabel)}</span>
      </span>
    </div>

    ${consensus}
    ${temporalHtml}
    ${reviewsHtml}
  `;

  // trigger the circle animations after a short delay so CSS can pick up
  setTimeout(function() {
    animateScoreCircles(rtRating, audRating);
  }, 80);

  // draw temporal chart if we have data
  if (temporal && temporal.monthly_data && temporal.monthly_data.length > 0) {
    setTimeout(function() { drawTemporalChart(temporal); }, 100);
  }
}

function makeScoreCircle(score, colour, label) {
  var radius = 48;
  var circumference = 2 * Math.PI * radius;
  // the stroke-dasharray trick for animated circles - took a while to get the math right
  return `
    <div class="score-circle-wrap">
      <svg class="score-circle-svg" width="120" height="120" viewBox="0 0 120 120">
        <circle class="score-circle-bg" cx="60" cy="60" r="${radius}" />
        <circle
          class="score-circle-arc ${colour}"
          cx="60" cy="60" r="${radius}"
          data-score="${score}"
          data-circumference="${circumference}"
          style="stroke-dasharray: 0 ${circumference}"
        />
        <g transform="rotate(90, 60, 60)">
          <text class="score-circle-text" x="60" y="56" text-anchor="middle">${score}%</text>
          <text class="score-circle-label-text" x="60" y="70" text-anchor="middle">${label}</text>
        </g>
      </svg>
      <div class="score-label">${label}</div>
    </div>
  `;
}

function animateScoreCircles(rtScore, audScore) {
  var arcs = document.querySelectorAll('.score-circle-arc');
  arcs.forEach(function(arc) {
    var score = parseFloat(arc.getAttribute('data-score'));
    var circ = parseFloat(arc.getAttribute('data-circumference'));
    var filled = (score / 100) * circ;
    arc.style.strokeDasharray = filled + ' ' + (circ - filled);
  });
}

function renderReviewCards(reviews) {
  if (!reviews || reviews.length === 0) return '';

  var html = '<div class="reviews-section"><h3>Critic Reviews</h3>';
  reviews.forEach(function(r, idx) {
    var content = r.review_content || '';
    var isLong = content.length > 200;
    var excerpt = isLong ? content.slice(0, 200) + '...' : content;
    var fullId = 'review-full-' + idx;
    var shortId = 'review-short-' + idx;
    var btnId = 'review-btn-' + idx;

    var chipClass = (r.review_type || '').toLowerCase() === 'fresh' ? 'fresh' : 'rotten';
    var scoreText = r.review_score || r.review_type || '';

    // build the IMDB sentiment badges if both models returned results
    var sentimentHtml = '';
    if (r.sentiment_fast || r.sentiment_deep) {
      sentimentHtml = '<div class="imdb-sentiment-row">';
      sentimentHtml += '<span class="imdb-sentiment-label">IMDB Sentiment</span>';

      if (r.sentiment_fast) {
        var fc = r.sentiment_fast.sentiment === 'Positive' ? 'positive' : 'negative';
        var fpct = Math.round(r.sentiment_fast.confidence * 100);
        sentimentHtml += `<span class="imdb-badge ${fc}">TF-IDF: ${escapeHtml(r.sentiment_fast.sentiment)} ${fpct}%</span>`;
      }

      if (r.sentiment_deep) {
        var dc = r.sentiment_deep.sentiment === 'Positive' ? 'positive' : 'negative';
        var dpct = Math.round(r.sentiment_deep.confidence * 100);
        var agree = r.sentiment_fast && r.sentiment_fast.sentiment === r.sentiment_deep.sentiment;
        sentimentHtml += `<span class="imdb-badge ${dc}">BERT: ${escapeHtml(r.sentiment_deep.sentiment)} ${dpct}%</span>`;
        // flag disagreement - sometimes a critic writes positively but marks rotten, interesting signal
        if (r.sentiment_fast && !agree) {
          sentimentHtml += '<span class="imdb-badge-disagree" title="Models disagree on sentiment">!</span>';
        }
      }

      sentimentHtml += '</div>';
    }

    html += `
      <div class="review-card">
        <div class="review-meta">
          <div>
            <div class="review-critic">${escapeHtml(r.critic_name || 'Unknown Critic')}</div>
            <div class="review-publication">${escapeHtml(r.publisher_name || '')}</div>
          </div>
          <span class="review-type-chip ${chipClass}">${escapeHtml(scoreText)}</span>
        </div>
        ${sentimentHtml}
        <div class="review-excerpt">
          <span id="${shortId}">${escapeHtml(excerpt)}</span>
          ${isLong ? `<span id="${fullId}" class="hidden">${escapeHtml(content)}</span>` : ''}
        </div>
        ${isLong ? `<button class="read-more-btn" id="${btnId}" onclick="toggleReviewExpand(${idx})">read more</button>` : ''}
      </div>
    `;
  });

  html += '</div>';
  return html;
}

function toggleReviewExpand(idx) {
  var full = document.getElementById('review-full-' + idx);
  var short = document.getElementById('review-short-' + idx);
  var btn = document.getElementById('review-btn-' + idx);

  if (!full) return;

  if (full.classList.contains('hidden')) {
    full.classList.remove('hidden');
    short.classList.add('hidden');
    btn.textContent = 'read less';
  } else {
    full.classList.add('hidden');
    short.classList.remove('hidden');
    btn.textContent = 'read more';
  }
}

function renderGapExplanation(data, el) {
  // only show when there's a meaningful gap and actual review data
  if (!data || !data.has_data) return;

  var gap = data.gap || 0;
  if (gap < 15) return;  // gaps under 15 points aren't interesting enough to explain

  var directionLabel = data.direction === 'audience_higher'
    ? 'Audiences rated this higher than critics'
    : 'Critics rated this higher than audiences';

  var directionClass = data.direction === 'audience_higher' ? 'explain-audience' : 'explain-critic';

  // build quote blocks
  var quotesHtml = '';
  if (data.quotes && data.quotes.length > 0) {
    var quoteItems = data.quotes.map(function(q) {
      var byline = [q.critic, q.publisher].filter(Boolean).join(', ');
      var typeClass = q.type === 'Rotten' ? 'quote-rotten' : 'quote-fresh';
      return (
        '<div class="explain-quote">' +
          '<p class="explain-quote-text">"' + escapeHtml(q.text) + '"</p>' +
          (byline ? '<p class="explain-quote-by ' + typeClass + '">' + escapeHtml(byline) + (q.type ? ' · ' + escapeHtml(q.type) : '') + '</p>' : '') +
        '</div>'
      );
    }).join('');
    quotesHtml = '<div class="explain-quotes">' + quoteItems + '</div>';
  }

  // stats pill row
  var stats = data.stats || {};
  var statsPills = '';
  if (stats.total_reviews > 0) {
    statsPills = (
      '<div class="explain-stats">' +
        '<span class="explain-stat">' + stats.total_reviews + ' reviews stored</span>' +
        (stats.rotten_count  ? '<span class="explain-stat stat-rotten">'  + stats.rotten_count  + ' Rotten</span>'  : '') +
        (stats.fresh_count   ? '<span class="explain-stat stat-fresh">'   + stats.fresh_count   + ' Fresh</span>'   : '') +
      '</div>'
    );
  }

  el.innerHTML = (
    '<div class="explain-card ' + directionClass + '">' +
      '<div class="explain-header">' +
        '<span class="explain-icon">◉</span>' +
        '<div>' +
          '<h3 class="explain-title">Why the gap?</h3>' +
          '<p class="explain-direction">' + escapeHtml(directionLabel) + ' by ' + gap + ' points</p>' +
        '</div>' +
      '</div>' +
      '<p class="explain-body">' + escapeHtml(data.explanation) + '</p>' +
      (data.quotes && data.quotes.length > 0 ? '<p class="explain-quotes-label">What critics actually said:</p>' : '') +
      quotesHtml +
      statsPills +
    '</div>'
  );
  el.classList.remove('hidden');
}


function renderTemporalChart(temporal) {
  if (!temporal) return '';

  var annotation = '';
  if (temporal.warning) {
    annotation = '<div class="temporal-annotation no-trend">' + escapeHtml(temporal.warning) + '</div>';
  } else {
    var trend = temporal.trend || 'no trend';
    if (trend === 'increasing') {
      annotation = '<div class="temporal-annotation increasing">^ Improving over time</div>';
    } else if (trend === 'decreasing') {
      annotation = '<div class="temporal-annotation decreasing">v Declining over time</div>';
    } else {
      annotation = '<div class="temporal-annotation no-trend">-- No significant trend detected</div>';
    }
  }

  var chartHtml = temporal.monthly_data && temporal.monthly_data.length > 0
    ? '<canvas id="temporal-chart" height="180"></canvas>'
    : '<p style="color:var(--text-muted);font-size:13px;">Not enough review data for a timeline chart.</p>';

  return `
    <div class="temporal-section">
      <h3>Review Trend Over Time</h3>
      ${annotation}
      ${chartHtml}
    </div>
  `;
}

function drawTemporalChart(temporal) {
  var canvas = document.getElementById('temporal-chart');
  if (!canvas || !temporal.monthly_data || temporal.monthly_data.length === 0) return;

  var container = canvas.parentElement;
  canvas.width = container.clientWidth - 20;
  canvas.height = 180;

  var ctx = canvas.getContext('2d');
  var w = canvas.width;
  var h = canvas.height;
  var pL = 48, pR = 20, pT = 16, pB = 36;

  var chartW = w - pL - pR;
  var chartH = h - pT - pB;

  var data = temporal.monthly_data.filter(function(d) { return d.fresh_ratio != null; });
  if (data.length < 2) return;

  ctx.clearRect(0, 0, w, h);

  var xStep = chartW / (data.length - 1);

  // draw grid lines
  ctx.strokeStyle = 'rgba(48,54,61,0.6)';
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 3]);
  [0, 0.25, 0.5, 0.75, 1.0].forEach(function(v) {
    var y = pT + (1 - v) * chartH;
    ctx.beginPath();
    ctx.moveTo(pL, y);
    ctx.lineTo(w - pR, y);
    ctx.stroke();

    ctx.fillStyle = '#484f58';
    ctx.font = '11px JetBrains Mono, monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText(Math.round(v * 100) + '%', pL - 6, y);
  });
  ctx.setLineDash([]);

  // draw the line
  ctx.beginPath();
  ctx.strokeStyle = '#6fbbf8';
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';

  data.forEach(function(point, i) {
    var x = pL + i * xStep;
    var y = pT + (1 - point.fresh_ratio) * chartH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // fill area under the line
  ctx.beginPath();
  data.forEach(function(point, i) {
    var x = pL + i * xStep;
    var y = pT + (1 - point.fresh_ratio) * chartH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.lineTo(pL + (data.length - 1) * xStep, pT + chartH);
  ctx.lineTo(pL, pT + chartH);
  ctx.closePath();
  var grad = ctx.createLinearGradient(0, pT, 0, pT + chartH);
  grad.addColorStop(0, 'rgba(111,187,248,0.25)');
  grad.addColorStop(1, 'rgba(111,187,248,0.0)');
  ctx.fillStyle = grad;
  ctx.fill();

  // dots at data points
  data.forEach(function(point, i) {
    var x = pL + i * xStep;
    var y = pT + (1 - point.fresh_ratio) * chartH;
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, 2 * Math.PI);
    ctx.fillStyle = '#6fbbf8';
    ctx.fill();
  });

  // draw a couple of period labels on the x axis to orient the viewer
  var labelStep = Math.max(1, Math.floor(data.length / 5));
  ctx.fillStyle = '#484f58';
  ctx.font = '10px JetBrains Mono, monospace';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  data.forEach(function(point, i) {
    if (i % labelStep === 0 || i === data.length - 1) {
      var x = pL + i * xStep;
      ctx.fillText(point.period, x, pT + chartH + 6);
    }
  });
}


// review analyser
async function analyseReview() {
  var textarea = document.getElementById('review-textarea');
  var resultsEl = document.getElementById('analyser-results');
  var btn = document.getElementById('analyse-btn');

  var text = textarea.value.trim();
  if (!text) {
    showError('Paste a review first');
    return;
  }

  btn.disabled = true;
  resultsEl.classList.remove('hidden');

  // skeleton loading while we wait for the model
  resultsEl.innerHTML = buildSkeletonCards();

  try {
    var result = await apiFetch('/api/analyse/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ review_text: text }),
    });

    renderAnalysisResults(result);
  } catch (e) {
    showError('Analysis failed - the models might still be loading');
    console.error(e);
    resultsEl.innerHTML = '<p style="color:var(--danger);font-size:13px;padding:12px;">Analysis failed. Check that the backend models have finished loading.</p>';
  } finally {
    btn.disabled = false;
  }
}

function buildSkeletonCards() {
  var cards = '';
  for (var i = 0; i < 8; i++) {
    cards += '<div class="aspect-card skeleton skeleton-aspect"></div>';
  }
  return '<div class="aspect-grid">' + cards + '</div>';
}

function renderAnalysisResults(data) {
  var resultsEl = document.getElementById('analyser-results');

  var overall = data.overall_sentiment || 'Mixed';
  var overallClass = overall.toLowerCase();

  // top row: aspect-based overall sentiment + IMDB ensemble side by side
  var imdbHtml = '';
  if (data.imdb_sentiment) {
    var im = data.imdb_sentiment;
    var imClass = im.sentiment === 'Positive' ? 'positive' : 'negative';
    var imPct = Math.round(im.confidence * 100);
    var imAgreement = im.agreement
      ? '<span class="imdb-agree-dot" title="Both models agree">&#10003;</span>'
      : '<span class="imdb-agree-dot disagree" title="Models disagree">!</span>';

    var tfPct  = Math.round(im.tfidf.confidence * 100);
    var dbPct  = Math.round(im.distilbert.confidence * 100);
    var tfClass  = im.tfidf.sentiment  === 'Positive' ? 'positive' : 'negative';
    var dbClass  = im.distilbert.sentiment === 'Positive' ? 'positive' : 'negative';

    imdbHtml = `
      <div class="imdb-ensemble-block">
        <div class="imdb-ensemble-title">IMDB-Trained Sentiment ${imAgreement}</div>
        <div class="imdb-ensemble-badges">
          <span class="imdb-badge ${tfClass}">TF-IDF: ${escapeHtml(im.tfidf.sentiment)} ${tfPct}%</span>
          <span class="imdb-badge ${dbClass}">DistilBERT: ${escapeHtml(im.distilbert.sentiment)} ${dbPct}%</span>
          <span class="imdb-badge ${imClass} ensemble-result">Ensemble: ${escapeHtml(im.sentiment)} ${imPct}%</span>
        </div>
      </div>
    `;
  }

  var overallHtml = `
    <div class="analyser-top-row">
      <div class="overall-sentiment-badge ${overallClass}">Aspect Sentiment: ${escapeHtml(overall)}</div>
      ${imdbHtml}
    </div>
  `;

  var gridHtml = '<div class="aspect-grid">';
  (data.aspects || []).forEach(function(item) {
    var detected = item.confidence > 0.15;
    var sentClass = detected ? item.sentiment.toLowerCase() : 'neutral';

    var stars = '';
    for (var s = 1; s <= 5; s++) {
      stars += '<div class="star ' + (s <= item.stars ? 'filled' : 'empty') + '"></div>';
    }

    var barW = detected ? Math.round(item.confidence * 100) : 0;

    gridHtml += `
      <div class="aspect-card ${detected ? '' : 'not-detected'}">
        <div class="aspect-name">${escapeHtml(item.aspect)}</div>
        <div class="aspect-sentiment-label ${sentClass}">
          ${detected ? escapeHtml(item.sentiment) : 'Not detected'}
        </div>
        <div class="aspect-confidence-bar-track">
          <div class="aspect-confidence-bar-fill ${sentClass}" style="width:${barW}%"></div>
        </div>
        <div class="aspect-stars">${stars}</div>
      </div>
    `;
  });
  gridHtml += '</div>';

  resultsEl.innerHTML = overallHtml + gridHtml;

  // reveal the training feedback form now that we have a review to label
  var trainingBlock = document.getElementById('training-block');
  if (trainingBlock) trainingBlock.classList.remove('hidden');
}


// incremental training form
async function submitTraining() {
  var textarea = document.getElementById('review-textarea');
  var text = textarea.value.trim();
  var sentimentEl = document.querySelector('input[name="train-sentiment"]:checked');
  var statusEl = document.getElementById('training-status');
  var btn = document.getElementById('train-submit-btn');

  if (!text) {
    showError('No review text to submit - analyse a review first');
    return;
  }
  if (!sentimentEl) {
    showError('Select Positive or Negative before submitting');
    return;
  }

  btn.disabled = true;
  statusEl.className = 'training-status';
  statusEl.textContent = 'Submitting...';
  statusEl.classList.remove('hidden');

  try {
    var result = await apiFetch('/api/train/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ review_text: text, sentiment: sentimentEl.value }),
    });
    statusEl.className = 'training-status success';
    statusEl.textContent = result.message;
    // invalidate accuracy cache so next open shows updated metrics
    _accuracyLoaded = false;
  } catch (e) {
    statusEl.className = 'training-status error';
    statusEl.textContent = 'Submit failed: ' + (e.message || 'check console');
    console.error(e);
  } finally {
    btn.disabled = false;
  }
}


// model accuracy collapsible
var _accuracyLoaded = false;

async function toggleAccuracyPanel() {
  var panel = document.getElementById('accuracy-panel');
  var icon = document.getElementById('accuracy-toggle-icon');

  if (!panel.classList.contains('hidden')) {
    panel.classList.add('hidden');
    icon.textContent = '+';
    return;
  }

  panel.classList.remove('hidden');
  icon.textContent = '−';

  if (!_accuracyLoaded) {
    panel.innerHTML = '<div class="accuracy-loading">Loading accuracy report…</div>';
    try {
      var data = await apiFetch('/api/models/accuracy');
      renderAccuracyReport(data);
      _accuracyLoaded = true;
    } catch (e) {
      panel.innerHTML = '<p style="color:var(--text-muted);font-size:13px;padding:8px 0;">Could not load accuracy report.</p>';
      console.error(e);
    }
  }
}

function renderAccuracyReport(data) {
  var panel = document.getElementById('accuracy-panel');

  if (data.message) {
    panel.innerHTML = '<p class="accuracy-note">' + escapeHtml(data.message) + '</p>';
    _accuracyLoaded = false;  // allow retry next time panel is opened
    return;
  }

  function fmtPct(v) { return v != null ? (v * 100).toFixed(1) + '%' : '--'; }
  function fmtNum(v) { return v != null ? Number(v).toLocaleString() : '--'; }

  var rows = '';
  if (data.tfidf_sgd) {
    var t = data.tfidf_sgd;
    rows += '<tr><td>TF-IDF + SGD</td><td>' + fmtPct(t.train_accuracy) + '</td><td>' +
      fmtPct(t.val_accuracy) + '</td><td>' + fmtPct(t.test_accuracy) + '</td><td>' +
      fmtNum(t.train_samples) + '</td></tr>';
  }
  if (data.distilbert) {
    var d = data.distilbert;
    rows += '<tr><td>DistilBERT</td><td>' + fmtPct(d.train_accuracy) + '</td><td>' +
      fmtPct(d.val_accuracy) + '</td><td>' + fmtPct(d.test_accuracy) + '</td><td>' +
      fmtNum(d.train_samples) + '</td></tr>';
  }

  var ts = data.generated_at
    ? '<p class="accuracy-note">Trained: ' + escapeHtml(data.generated_at.slice(0, 10)) + '</p>'
    : '';

  panel.innerHTML = ts +
    '<table class="accuracy-table"><thead><tr>' +
    '<th>Model</th><th>Train Acc</th><th>Val Acc</th><th>Test Acc</th><th>Train Samples</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>';
}


// leaderboard
// TODO: add pagination to the leaderboard at some point
async function loadLeaderboard() {
  var container = document.getElementById('leaderboard-content');
  container.innerHTML = '<div class="loading-spinner">Loading leaderboard...</div>';

  try {
    var data = await apiFetch('/api/divergence/leaderboard');
    renderLeaderboard(data);
    state.leaderboardLoaded = true;
  } catch (e) {
    showError('Could not load leaderboard - check the console');
    console.error(e);
    container.innerHTML = '<p style="color:var(--danger);font-size:13px;padding:20px;">Failed to load leaderboard.</p>';
  }
}

function renderLeaderboard(data) {
  var container = document.getElementById('leaderboard-content');

  var criticFilms = data.critic_favoured || [];
  var audienceFilms = data.audience_favoured || [];

  // find max divergence score for bar scaling
  var maxDiv = 0;
  criticFilms.concat(audienceFilms).forEach(function(f) {
    if (f.divergence_score != null && Math.abs(f.divergence_score) > maxDiv) {
      maxDiv = Math.abs(f.divergence_score);
    }
  });
  if (maxDiv === 0) maxDiv = 1;

  function buildRows(films, colour) {
    return films.map(function(film, i) {
      var div = film.divergence_score != null ? Math.abs(film.divergence_score).toFixed(1) : '--';
      var barW = film.divergence_score != null ? Math.round(Math.abs(film.divergence_score) / maxDiv * 100) : 0;
      var rt = film.tomatometer_rating != null ? film.tomatometer_rating + '%' : '--';
      var aud = film.audience_rating != null ? film.audience_rating + '%' : '--';
      var link = film.rotten_tomatoes_link || '';

      return `
        <div class="leaderboard-row" onclick="loadFilmFromLeaderboard('${escapeAttr(link)}')">
          <div class="leaderboard-rank">${i + 1}</div>
          <div class="leaderboard-film-name">${escapeHtml(film.movie_title || 'Unknown')}</div>
          <div class="leaderboard-bar-wrap">
            <div class="leaderboard-bar-track">
              <div class="leaderboard-bar-fill ${colour}" style="width:${barW}%"></div>
            </div>
          </div>
          <div class="leaderboard-scores">
            <span class="leaderboard-score-chip orange" title="Tomatometer">${rt}</span>
            <span class="leaderboard-score-chip blue" title="Audience">${aud}</span>
          </div>
        </div>
      `;
    }).join('');
  }

  container.innerHTML = `
    <div class="leaderboard-col">
      <div class="leaderboard-col-header orange">Critics Loved It, Audiences Didn't</div>
      ${buildRows(criticFilms, 'orange')}
    </div>
    <div class="leaderboard-col">
      <div class="leaderboard-col-header blue">Audiences Loved It, Critics Didn't</div>
      ${buildRows(audienceFilms, 'blue')}
    </div>
  `;
}

function loadFilmFromLeaderboard(rtLink) {
  if (!rtLink) return;
  loadFilm(rtLink);
}


// browse by year
function setupBrowse() {
  // allow hitting Enter in any of the browse controls to trigger a search
  var inputs = ['browse-from-year', 'browse-to-year', 'browse-sort'];
  inputs.forEach(function(id) {
    var el = document.getElementById(id);
    if (el) {
      el.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') doBrowse();
      });
    }
  });
}

// called by the Browse button - resets to page 1 and fetches
function doBrowse() {
  state.browse.page = 1;
  fetchBrowseResults();
}

// called by pagination buttons
function changeBrowsePage(delta) {
  var next = state.browse.page + delta;
  if (next < 1 || next > state.browse.totalPages) return;
  state.browse.page = next;
  fetchBrowseResults();
}

async function fetchBrowseResults() {
  if (state.browse.loading) return;

  var fromYear = document.getElementById('browse-from-year').value.trim();
  var toYear   = document.getElementById('browse-to-year').value.trim();
  var sortBy   = document.getElementById('browse-sort').value;

  // build query string - omit year params if blank
  var params = new URLSearchParams();
  if (fromYear) params.set('from_year', fromYear);
  if (toYear)   params.set('to_year',   toYear);
  params.set('sort_by', sortBy);
  params.set('page',    state.browse.page);

  // show loading state inside the results area
  var resultsEl    = document.getElementById('browse-results');
  var gridEl       = document.getElementById('browse-grid');
  var infoEl       = document.getElementById('browse-results-info');
  var paginationEl = document.getElementById('browse-pagination');

  resultsEl.classList.remove('hidden');
  infoEl.textContent = 'Loading...';
  gridEl.innerHTML = '';
  paginationEl.classList.add('hidden');

  state.browse.loading = true;
  try {
    var data = await apiFetch('/api/films/browse?' + params.toString());
    renderBrowseResults(data);
  } catch (e) {
    infoEl.textContent = 'Failed to load results - check the console';
    infoEl.classList.add('browse-error');
    console.error(e);
  } finally {
    state.browse.loading = false;
  }
}

function renderBrowseResults(data) {
  var gridEl       = document.getElementById('browse-grid');
  var infoEl       = document.getElementById('browse-results-info');
  var paginationEl = document.getElementById('browse-pagination');
  var prevBtn      = document.getElementById('browse-prev');
  var nextBtn      = document.getElementById('browse-next');
  var pageInfoEl   = document.getElementById('browse-page-info');

  state.browse.total      = data.total;
  state.browse.totalPages = data.total_pages;
  state.browse.page       = data.page;

  // "Showing 1–20 of 1234 films" - small UX detail that helps orient the user
  var start = (data.page - 1) * data.per_page + 1;
  var end   = Math.min(start + data.per_page - 1, data.total);
  infoEl.textContent = data.total > 0
    ? 'Showing ' + start.toLocaleString() + '–' + end.toLocaleString() + ' of ' + data.total.toLocaleString() + ' films'
    : 'No films found for that range';
  infoEl.className = 'browse-results-info';

  if (!data.films || data.films.length === 0) {
    gridEl.innerHTML = '';
    paginationEl.classList.add('hidden');
    return;
  }

  gridEl.innerHTML = data.films.map(function(film) {
    var rt     = film.tomatometer_rating != null ? Math.round(film.tomatometer_rating) + '%' : '--';
    var aud    = film.audience_rating    != null ? Math.round(film.audience_rating)    + '%' : '--';
    var year   = film.release_year != null ? film.release_year : '';
    var link   = film.rotten_tomatoes_link || '';

    // truncate genre list if it's too long to fit on the card
    var genres = '';
    if (film.genres) {
      var genreList = film.genres.split(',').slice(0, 3).map(function(g) {
        return '<span class="browse-genre-pill">' + escapeHtml(g.trim()) + '</span>';
      });
      genres = '<div class="browse-genre-pills">' + genreList.join('') + '</div>';
    }

    var divBadge = '';
    if (film.divergence_score != null) {
      var score = film.divergence_score;
      var sign  = score >= 0 ? '+' : '';
      var cls   = score > 5 ? 'critics' : score < -5 ? 'audience' : 'aligned';
      divBadge  = '<span class="browse-div-badge ' + cls + '">' + sign + score.toFixed(1) + '</span>';
    }

    return `
      <div class="browse-film-card" onclick="loadFilmFromBrowse('${escapeAttr(link)}')">
        <div class="browse-film-title">${escapeHtml(film.movie_title || 'Unknown')}</div>
        <div class="browse-film-year">${year}</div>
        ${genres}
        <div class="browse-scores">
          <span class="browse-score-chip orange" title="Tomatometer">${rt}</span>
          <span class="browse-score-chip blue" title="Audience Score">${aud}</span>
          ${divBadge}
        </div>
      </div>
    `;
  }).join('');

  // pagination controls
  if (data.total_pages > 1) {
    paginationEl.classList.remove('hidden');
    pageInfoEl.textContent = 'Page ' + data.page + ' of ' + data.total_pages;
    prevBtn.disabled = data.page <= 1;
    nextBtn.disabled = data.page >= data.total_pages;
  } else {
    paginationEl.classList.add('hidden');
  }
}

function loadFilmFromBrowse(rtLink) {
  if (!rtLink) return;
  loadFilm(rtLink);
}


// utilities
async function apiFetch(url, options) {
  var res = await fetch(API + url, options);
  if (!res.ok) {
    var errText = await res.text().catch(function() { return ''; });
    throw new Error('HTTP ' + res.status + ': ' + errText);
  }
  return res.json();
}

// minimal HTML escape - avoids XSS from film titles/review content
function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// for use in html attribute values (onclick etc)
function escapeAttr(str) {
  if (str == null) return '';
  return String(str).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}
