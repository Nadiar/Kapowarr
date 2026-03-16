// Calendar page JavaScript
// Follows existing Kapowarr patterns: fetchAPI, localStorage, element refs

const calendar_els = {
	grid: document.querySelector('#calendar-grid'),
	loading: document.querySelector('#loading-calendar'),
	empty: document.querySelector('#empty-calendar'),
	header: {
		prev: document.querySelector('#prev-period'),
		next: document.querySelector('#next-period'),
		today: document.querySelector('#today-button'),
		label: document.querySelector('#period-label'),
		view_week: document.querySelector('#view-week'),
		view_month: document.querySelector('#view-month')
	},
	filter: {
		panel: document.querySelector('#publisher-filter'),
		toggle: document.querySelector('#filter-toggle'),
		any_checkbox: document.querySelector('.any-publisher input'),
		list: document.querySelector('#publisher-list'),
		select_all: document.querySelector('#select-all-publishers'),
		select_none: document.querySelector('#select-none-publishers'),
		format_tpb: document.querySelector('#filter-tpb'),
		format_hardcover: document.querySelector('#filter-hardcover'),
		format_webcomic: document.querySelector('#filter-webcomic')
	},
	toolbar: {
		refresh: document.querySelector('#refresh-calendar'),
		add_all: document.querySelector('#add-all-unmonitored')
	},
	add_window: {
		cv_id: document.querySelector('#calendar-cv-id'),
		title: document.querySelector('#calendar-add-title'),
		root_folder: document.querySelector('#calendar-rootfolder-input'),
		monitor: document.querySelector('#calendar-monitor-input'),
		monitor_issues: document.querySelector('#calendar-monitor-issues-input'),
		monitoring_scheme: document.querySelector('#calendar-monitoring-scheme-input'),
		special: document.querySelector('#calendar-special-input'),
		auto_search: document.querySelector('#calendar-auto-search-input'),
		submit: document.querySelector('#calendar-add-submit'),
		form: document.querySelector('#calendar-add-form')
	}
};

const pre_build_els = {
	issue: document.querySelector('.pre-build-els .calendar-issue')
};

// State
let currentWeekStart = getMonday(new Date());
let viewMode = 'week'; // 'week' or 'month'
let publishers = [];          // ["Marvel", "DC Comics", ...]
let publisherCategories = {}; // {category_name: ["Marvel", ...]}
let selectedPublisherNames = new Set();
let anyPublisherMode = true;
let cachedIssues = [];
let showTPBs = true;
let showHardCovers = true;
let showWebcomics = true;

// Format detection regex patterns
const FORMAT_PATTERNS = {
	tpb: /\bTPB\b|\bTrade\s*Paper\s*Back|\bEpic\s*Collection\b|\bComplete\s*Collection\b|\bCompendium\b|\bDeluxe\s*Edition\b|\bCollected\s*Edition\b|\bOmnibus\b|^Vol\.?\s*\d+/i,
	hardcover: /\bHC\b|\bHard[\s-]?Cover/i,
	webcomic: /\bWebcomic\b|\bWeb\s*Comic\b|\bDigital\s*First\b|\bInfinit[ey]\s*Comic/i
};

function detectFormat(text) {
	if (!text) return 'normal';
	if (FORMAT_PATTERNS.tpb.test(text)) return 'tpb';
	if (FORMAT_PATTERNS.hardcover.test(text)) return 'hardcover';
	if (FORMAT_PATTERNS.webcomic.test(text)) return 'webcomic';
	return 'normal';
};

function detectIssueFormat(issue) {
	const volFmt = detectFormat(issue.volume_name);
	if (volFmt !== 'normal') return volFmt;
	// Also check issue title for format hints
	return detectFormat(issue.title);
};

//
// Date helpers
//
function getMonday(date) {
	const d = new Date(date);
	const day = d.getDay();
	const diff = d.getDate() - day + (day === 0 ? -6 : 1);
	d.setDate(diff);
	d.setHours(0, 0, 0, 0);
	return d;
};

function getMonthStart(date) {
	const d = new Date(date);
	d.setDate(1);
	d.setHours(0, 0, 0, 0);
	return d;
};

function getMonthEnd(date) {
	const d = new Date(date.getFullYear(), date.getMonth() + 1, 0);
	d.setHours(0, 0, 0, 0);
	return d;
};

function addDays(date, days) {
	const d = new Date(date);
	d.setDate(d.getDate() + days);
	return d;
};

function addMonths(date, months) {
	const d = new Date(date);
	d.setMonth(d.getMonth() + months);
	return d;
};

function formatDate(date) {
	const y = date.getFullYear();
	const m = String(date.getMonth() + 1).padStart(2, '0');
	const d = String(date.getDate()).padStart(2, '0');
	return `${y}-${m}-${d}`;
};

function formatDisplayDate(date) {
	return date.toLocaleDateString('en-US', {
		month: 'short',
		day: 'numeric'
	});
};

function formatDayHeader(dateStr) {
	const date = new Date(dateStr + 'T00:00:00');
	return date.toLocaleDateString('en-US', {
		weekday: 'long',
		month: 'long',
		day: 'numeric',
		year: 'numeric'
	});
};

//
// Period label (week or month)
//
function getPeriodRange() {
	if (viewMode === 'month') {
		const start = getMonthStart(currentWeekStart);
		const end = getMonthEnd(currentWeekStart);
		return { start, end };
	} else {
		const start = currentWeekStart;
		const end = addDays(start, 6);
		return { start, end };
	}
};

function updatePeriodLabel() {
	const { start, end } = getPeriodRange();
	if (viewMode === 'month') {
		calendar_els.header.label.textContent = start.toLocaleDateString('en-US', {
			month: 'long',
			year: 'numeric'
		});
	} else {
		calendar_els.header.label.textContent =
			`${formatDisplayDate(start)} - ${formatDisplayDate(end)}, ${end.getFullYear()}`;
	}
};

//
// Publisher filter
//
function loadPublishers(api_key) {
	fetchAPI('/calendar/publishers', api_key)
	.then(json => {
		const data = json.result;
		publishers = data.publishers || [];
		publisherCategories = data.categories || {};
		renderCategoryToggles();
		renderPublisherList();
		restorePublisherPrefs();
	})
	.catch(() => {
		console.error('Failed to load publisher presets');
	});
};

function renderPublisherList() {
	while (calendar_els.filter.list.firstChild)
		calendar_els.filter.list.removeChild(
			calendar_els.filter.list.firstChild
		);
	publishers.forEach(pub => {
		const label = document.createElement('label');
		label.className = 'publisher-option';

		const checkbox = document.createElement('input');
		checkbox.type = 'checkbox';
		checkbox.value = pub;
		checkbox.checked = true;

		const span = document.createElement('span');
		span.textContent = pub;

		label.appendChild(checkbox);
		label.appendChild(span);
		calendar_els.filter.list.appendChild(label);

		checkbox.addEventListener('change', () => {
			onPublisherCheckboxChange();
		});
	});
};

function onPublisherCheckboxChange() {
	// When individual publishers change, update "any" checkbox
	const checkboxes = calendar_els.filter.list.querySelectorAll('input[type="checkbox"]');
	const allChecked = Array.from(checkboxes).every(c => c.checked);

	if (allChecked) {
		calendar_els.filter.any_checkbox.checked = true;
		anyPublisherMode = true;
	} else {
		calendar_els.filter.any_checkbox.checked = false;
		anyPublisherMode = false;
	}

	updateSelectedPublishers();
	savePublisherPrefs();
	updateFilterButtonLabel();
	updateCategoryToggleStates();
	renderIssues();
};

function updateSelectedPublishers() {
	selectedPublisherNames.clear();
	if (anyPublisherMode) return;

	const checkboxes = calendar_els.filter.list.querySelectorAll('input[type="checkbox"]');
	checkboxes.forEach(cb => {
		if (cb.checked) {
			selectedPublisherNames.add(cb.value);
		}
	});
};

function savePublisherPrefs() {
	const checkedNames = [];
	const checkboxes = calendar_els.filter.list.querySelectorAll('input[type="checkbox"]');
	checkboxes.forEach(cb => {
		if (cb.checked) checkedNames.push(cb.value);
	});
	setLocalStorage({
		calendar_any_publisher: anyPublisherMode,
		calendar_publisher_names: checkedNames
	});
};

function restorePublisherPrefs() {
	const prefs = getLocalStorage(
		'calendar_any_publisher', 'calendar_publisher_names'
	);
	if (prefs.calendar_any_publisher === undefined
		|| prefs.calendar_any_publisher === null) {
		// First time: default to "any"
		anyPublisherMode = true;
		calendar_els.filter.any_checkbox.checked = true;
		return;
	}

	anyPublisherMode = prefs.calendar_any_publisher;
	calendar_els.filter.any_checkbox.checked = anyPublisherMode;

	if (!anyPublisherMode
		&& Array.isArray(prefs.calendar_publisher_names)) {
		const savedNames = new Set(prefs.calendar_publisher_names);
		const checkboxes = calendar_els.filter.list
			.querySelectorAll('input[type="checkbox"]');
		checkboxes.forEach(cb => {
			cb.checked = savedNames.has(cb.value);
		});
	}
	updateSelectedPublishers();
	updateFilterButtonLabel();
	updateCategoryToggleStates();
};

//
// Fetch and render calendar
//
function fetchCalendar(api_key, forceRefresh) {
	const { start, end } = getPeriodRange();
	const startStr = formatDate(start);
	const endStr = formatDate(end);

	// Show loading
	hide([calendar_els.grid, calendar_els.empty], [calendar_els.loading]);

	const params = { start: startStr, end: endStr };
	if (forceRefresh) params.force = '1';

	fetchAPI('/calendar', api_key, params)
	.then(json => {
		cachedIssues = json.result || [];
		renderIssues();
	})
	.catch(err => {
		console.error('Failed to fetch calendar:', err);
		hide([calendar_els.loading], [calendar_els.empty]);
		calendar_els.empty.querySelector('p').textContent =
			'Failed to load calendar data';
	});
};

function renderIssues() {
	// Filter by publisher client-side
	let issues = cachedIssues;
	if (!anyPublisherMode) {
		// When specific publishers are selected, filter to only those
		if (selectedPublisherNames.size > 0) {
			issues = issues.filter(
				i => (i.publisher_name
					&& selectedPublisherNames.has(i.publisher_name))
					|| (i.parent_publisher_name
					&& selectedPublisherNames.has(
						i.parent_publisher_name))
			);
		} else {
			// When no publishers are selected, show no issues
			issues = [];
		}
	}

	// Filter by format
	if (!showTPBs || !showHardCovers || !showWebcomics) {
		issues = issues.filter(i => {
			const fmt = detectIssueFormat(i);
			if (fmt === 'tpb' && !showTPBs) return false;
			if (fmt === 'hardcover' && !showHardCovers) return false;
			if (fmt === 'webcomic' && !showWebcomics) return false;
			return true;
		});
	}

	// Group by effective_date (store_date, falling back to cover_date)
	const byDate = {};
	issues.forEach(issue => {
		const date = issue.effective_date || 'Unknown';
		if (!byDate[date]) byDate[date] = [];
		byDate[date].push(issue);
	});

	// Sort dates
	const sortedDates = Object.keys(byDate).sort();

	// Render
	calendar_els.grid.innerHTML = '';

	if (sortedDates.length === 0) {
		hide([calendar_els.loading], [calendar_els.empty]);
		calendar_els.empty.querySelector('p').textContent =
			'No comics found for this period';
		calendar_els.grid.classList.add('hidden');
		return;
	}

	hide([calendar_els.loading, calendar_els.empty]);
	calendar_els.grid.classList.remove('hidden');

	sortedDates.forEach(dateStr => {
		const dayIssues = byDate[dateStr];

		// Day container
		const dayDiv = document.createElement('div');
		dayDiv.className = 'calendar-day';

		// Day header
		const header = document.createElement('div');
		header.className = 'calendar-day-header';

		const headerTitle = document.createElement('h3');
		headerTitle.textContent = dateStr === 'Unknown'
			? 'Unknown Date'
			: formatDayHeader(dateStr);

		const countSpan = document.createElement('span');
		countSpan.className = 'issue-count';
		countSpan.textContent = `${dayIssues.length} issue${dayIssues.length !== 1 ? 's' : ''}`;

		header.appendChild(headerTitle);
		header.appendChild(countSpan);
		dayDiv.appendChild(header);

		// Issues grid
		const issuesDiv = document.createElement('div');
		issuesDiv.className = 'calendar-day-issues';

		dayIssues.forEach(issue => {
			const card = buildIssueCard(issue);
			issuesDiv.appendChild(card);
		});

		dayDiv.appendChild(issuesDiv);
		calendar_els.grid.appendChild(dayDiv);
	});
};

function buildIssueCard(issue) {
	const card = pre_build_els.issue.cloneNode(true);

	card.href = issue.site_url || '#';
	card.setAttribute('aria-label',
		`${issue.volume_name} #${issue.issue_number}`);

	// Status badges
	const badges = card.querySelector('.calendar-issue-badges');
	if (issue.in_library) {
		const libraryBadge = document.createElement('span');
		libraryBadge.className = 'badge badge-library';
		libraryBadge.title = 'In library';
		libraryBadge.textContent = '📚';
		badges.appendChild(libraryBadge);
	}
	if (issue.monitored) {
		const monitoredBadge = document.createElement('span');
		monitoredBadge.className = 'badge badge-monitored';
		monitoredBadge.title = 'Issue monitored';
		monitoredBadge.textContent = '🔔';
		badges.appendChild(monitoredBadge);
	} else if (issue.volume_monitored) {
		const volMonBadge = document.createElement('span');
		volMonBadge.className = 'badge badge-vol-monitored';
		volMonBadge.title = 'Volume monitored';
		volMonBadge.textContent = '📖';
		badges.appendChild(volMonBadge);
	}
	if (issue.has_files) {
		const downloadedBadge = document.createElement('span');
		downloadedBadge.className = 'badge badge-downloaded';
		downloadedBadge.title = 'Downloaded';
		downloadedBadge.textContent = '✅';
		badges.appendChild(downloadedBadge);
	}

	// Apply status classes to card
	if (issue.in_library) card.classList.add('in-library');
	if (issue.monitored) card.classList.add('is-monitored');
	if (issue.has_files) card.classList.add('is-downloaded');

	const img = card.querySelector('.calendar-issue-img');
	if (issue.image_url) {
		img.src = issue.image_url;
		img.alt = `${issue.volume_name} #${issue.issue_number}`;
	} else {
		img.src = `${url_base}/static/img/favicon.svg`;
		img.alt = 'No cover';
	}

	card.querySelector('.calendar-issue-volume').textContent =
		issue.volume_name || 'Unknown Volume';

	card.querySelector('.calendar-issue-number').textContent =
		issue.issue_number ? `#${issue.issue_number}` : '';

	const titleEl = card.querySelector('.calendar-issue-title');
	if (issue.title) {
		titleEl.textContent = issue.title;
		titleEl.title = issue.title;
	} else {
		titleEl.classList.add('hidden');
	}

	const publisherEl = card.querySelector('.calendar-issue-publisher');
	if (issue.parent_publisher_name && issue.parent_publisher_name !== issue.publisher_name) {
		publisherEl.textContent = `${issue.parent_publisher_name} / ${issue.publisher_name}`;
	} else {
		publisherEl.textContent = issue.publisher_name || '';
	}

	// Add button: show only if NOT in library
	const addBtn = card.querySelector('.calendar-add-btn');
	if (issue.in_library) {
		addBtn.classList.add('hidden');
		// Link to volume page if in library
		if (issue.volume_id_local) {
			card.href = `${url_base}/volumes/${issue.volume_id_local}`;
			card.title = 'View in library';
		}
	} else {
		addBtn.addEventListener('click', (e) => {
			e.preventDefault();
			e.stopPropagation();
			openAddVolumeWindow(issue);
		});
	}

	return card;
};

//
// Event handlers
//
function navigatePeriod(direction) {
	if (viewMode === 'month') {
		currentWeekStart = addMonths(currentWeekStart, direction);
	} else {
		currentWeekStart = addDays(currentWeekStart, direction * 7);
	}
	updatePeriodLabel();
	usingApiKey().then(api_key => fetchCalendar(api_key));
};

function setViewMode(mode) {
	viewMode = mode;
	calendar_els.header.view_week.classList.toggle('active', mode === 'week');
	calendar_els.header.view_month.classList.toggle('active', mode === 'month');
	setLocalStorage({ calendar_view_mode: mode });
	updatePeriodLabel();
	usingApiKey().then(api_key => fetchCalendar(api_key));
};

// Navigation buttons
calendar_els.header.prev.onclick = () => navigatePeriod(-1);
calendar_els.header.next.onclick = () => navigatePeriod(1);
calendar_els.header.today.onclick = () => {
	currentWeekStart = getMonday(new Date());
	updatePeriodLabel();
	usingApiKey().then(api_key => fetchCalendar(api_key));
};

// View toggle
calendar_els.header.view_week.onclick = () => setViewMode('week');
calendar_els.header.view_month.onclick = () => setViewMode('month');

// Refresh button
calendar_els.toolbar.refresh.onclick = () => {
	usingApiKey().then(api_key => fetchCalendar(api_key, true));
};

// Filter button label
function updateFilterButtonLabel() {
	const label = calendar_els.filter.toggle.querySelector('p');
	if (!label) return;
	if (anyPublisherMode) {
		label.textContent = 'Publishers';
	} else {
		const checkboxes = calendar_els.filter.list.querySelectorAll('input[type="checkbox"]');
		const count = Array.from(checkboxes).filter(c => c.checked).length;
		label.textContent = `Publishers (${count})`;
	}
};

// Category toggles
function renderCategoryToggles() {
	const container = document.querySelector('#category-toggles');
	if (!container) return;
	while (container.firstChild) container.removeChild(container.firstChild);

	for (const [catName, catPubs] of Object.entries(publisherCategories)) {
		const btn = document.createElement('button');
		btn.type = 'button';
		btn.className = 'category-toggle active';
		btn.dataset.category = catName;
		btn.textContent = catName.charAt(0).toUpperCase() + catName.slice(1);
		btn.title = `Toggle ${btn.textContent} publishers`;

		btn.addEventListener('click', () => {
			toggleCategory(catName);
		});

		container.appendChild(btn);
	}
};

function toggleCategory(catName) {
	const catPubs = publisherCategories[catName];
	if (!catPubs) return;

	const catNames = new Set(catPubs);
	const checkboxes = calendar_els.filter.list
		.querySelectorAll('input[type="checkbox"]');

	// Check if all publishers in this category are checked
	const allChecked = Array.from(checkboxes)
		.filter(cb => catNames.has(cb.value))
		.every(cb => cb.checked);

	// Toggle: if all checked, uncheck all; otherwise check all
	checkboxes.forEach(cb => {
		if (catNames.has(cb.value)) {
			cb.checked = !allChecked;
		}
	});

	onPublisherCheckboxChange();
	updateCategoryToggleStates();
};

function updateCategoryToggleStates() {
	const container = document.querySelector('#category-toggles');
	if (!container) return;

	const checkboxes = calendar_els.filter.list.querySelectorAll('input[type="checkbox"]');

	container.querySelectorAll('.category-toggle').forEach(btn => {
		const catName = btn.dataset.category;
		const catPubs = publisherCategories[catName];
		if (!catPubs) return;

		const catNames = new Set(catPubs);
		const allChecked = Array.from(checkboxes)
			.filter(cb => catNames.has(cb.value))
			.every(cb => cb.checked);

		btn.classList.toggle('active', allChecked);
	});
};

// Publisher filter toggle
calendar_els.filter.toggle.onclick = () => {
	calendar_els.filter.panel.classList.toggle('hidden');
	calendar_els.filter.toggle.classList.toggle('active');
};

// "Any Publisher" checkbox
calendar_els.filter.any_checkbox.addEventListener('change', () => {
	anyPublisherMode = calendar_els.filter.any_checkbox.checked;
	if (anyPublisherMode) {
		// Check all individual publishers
		const checkboxes = calendar_els.filter.list.querySelectorAll('input[type="checkbox"]');
		checkboxes.forEach(cb => { cb.checked = true; });
	}
	updateSelectedPublishers();
	savePublisherPrefs();
	updateFilterButtonLabel();
	updateCategoryToggleStates();
	renderIssues();
});

// Format filter handlers
function onFormatFilterChange() {
	showTPBs = calendar_els.filter.format_tpb.checked;
	showHardCovers = calendar_els.filter.format_hardcover.checked;
	showWebcomics = calendar_els.filter.format_webcomic.checked;
	saveFormatPrefs();
	renderIssues();
};

function saveFormatPrefs() {
	setLocalStorage({
		calendar_show_tpb: showTPBs,
		calendar_show_hardcover: showHardCovers,
		calendar_show_webcomic: showWebcomics
	});
};

function restoreFormatPrefs() {
	const prefs = getLocalStorage(
		'calendar_show_tpb', 'calendar_show_hardcover', 'calendar_show_webcomic'
	);
	if (prefs.calendar_show_tpb !== undefined && prefs.calendar_show_tpb !== null)
		showTPBs = prefs.calendar_show_tpb;
	if (prefs.calendar_show_hardcover !== undefined && prefs.calendar_show_hardcover !== null)
		showHardCovers = prefs.calendar_show_hardcover;
	if (prefs.calendar_show_webcomic !== undefined && prefs.calendar_show_webcomic !== null)
		showWebcomics = prefs.calendar_show_webcomic;

	calendar_els.filter.format_tpb.checked = showTPBs;
	calendar_els.filter.format_hardcover.checked = showHardCovers;
	calendar_els.filter.format_webcomic.checked = showWebcomics;
};

calendar_els.filter.format_tpb.addEventListener('change', onFormatFilterChange);
calendar_els.filter.format_hardcover.addEventListener('change', onFormatFilterChange);
calendar_els.filter.format_webcomic.addEventListener('change', onFormatFilterChange);

// Select All / None
calendar_els.filter.select_all.onclick = () => {
	calendar_els.filter.any_checkbox.checked = true;
	anyPublisherMode = true;
	const checkboxes = calendar_els.filter.list.querySelectorAll('input[type="checkbox"]');
	checkboxes.forEach(cb => { cb.checked = true; });
	updateSelectedPublishers();
	savePublisherPrefs();
	updateFilterButtonLabel();
	updateCategoryToggleStates();
	renderIssues();
};

calendar_els.filter.select_none.onclick = () => {
	calendar_els.filter.any_checkbox.checked = false;
	anyPublisherMode = false;
	const checkboxes = calendar_els.filter.list.querySelectorAll('input[type="checkbox"]');
	checkboxes.forEach(cb => { cb.checked = false; });
	updateSelectedPublishers();
	savePublisherPrefs();
	updateFilterButtonLabel();
	updateCategoryToggleStates();
	renderIssues();
};

//
// Add Volume from Calendar
//
function loadRootFolders(api_key) {
	fetchAPI('/rootfolder', api_key)
	.then(json => {
		calendar_els.add_window.root_folder.innerHTML = '';
		json.result.forEach(folder => {
			const option = document.createElement('option');
			option.value = folder.id;
			option.innerText = folder.folder;
			calendar_els.add_window.root_folder.appendChild(option);
		});
	})
	.catch(() => {
		console.error('Failed to load root folders');
	});
};

function openAddVolumeWindow(issue) {
	calendar_els.add_window.cv_id.value = issue.volume_id;
	calendar_els.add_window.title.textContent =
		`${issue.volume_name}${issue.publisher_name ? ' (' + issue.publisher_name + ')' : ''}`;
	calendar_els.add_window.special.value = 'auto';

	// Restore monitoring preferences from localStorage
	const prefs = getLocalStorage(
		'monitor_new_volume', 'monitor_new_issues', 'monitoring_scheme'
	);
	if (prefs.monitor_new_volume !== undefined && prefs.monitor_new_volume !== null)
		calendar_els.add_window.monitor.value = String(prefs.monitor_new_volume);
	if (prefs.monitor_new_issues !== undefined && prefs.monitor_new_issues !== null)
		calendar_els.add_window.monitor_issues.value = String(prefs.monitor_new_issues);
	if (prefs.monitoring_scheme)
		calendar_els.add_window.monitoring_scheme.value = prefs.monitoring_scheme;

	showWindow('calendar-add-window');
};

function addVolumeFromCalendar() {
	const data = {
		'comicvine_id': parseInt(calendar_els.add_window.cv_id.value),
		'root_folder_id': parseInt(calendar_els.add_window.root_folder.value),
		'monitor': calendar_els.add_window.monitor.value === 'true',
		'monitoring_scheme': calendar_els.add_window.monitoring_scheme.value,
		'monitor_new_issues': calendar_els.add_window.monitor_issues.value === 'true',
		'volume_folder': '',
		'special_version': calendar_els.add_window.special.value || null,
		'auto_search': calendar_els.add_window.auto_search.checked
	};

	setLocalStorage({
		'monitor_new_volume': data.monitor,
		'monitor_new_issues': data.monitor_new_issues,
		'monitoring_scheme': data.monitoring_scheme
	});

	// Fire-and-forget: close modal immediately, add runs in background
	closeWindow();

	usingApiKey()
	.then(api_key => {
		sendAPI('POST', '/volumes', api_key, {}, data)
		.catch(e => {
			console.error('Add volume failed:', e);
		});
	});
};

calendar_els.add_window.form.action = 'javascript:addVolumeFromCalendar();';

//
// Add All Unmonitored
//
function addAllUnmonitored() {
	// Collect unique volumes not in library from visible (filtered) issues
	let issues = cachedIssues;
	if (!anyPublisherMode && selectedPublisherNames.size > 0) {
		issues = issues.filter(
			i => (i.publisher_name
				&& selectedPublisherNames.has(i.publisher_name))
				|| (i.parent_publisher_name
				&& selectedPublisherNames.has(
					i.parent_publisher_name))
		);
	}
	if (!showTPBs || !showHardCovers || !showWebcomics) {
		issues = issues.filter(i => {
			const fmt = detectIssueFormat(i);
			if (fmt === 'tpb' && !showTPBs) return false;
			if (fmt === 'hardcover' && !showHardCovers) return false;
			if (fmt === 'webcomic' && !showWebcomics) return false;
			return true;
		});
	}

	const seenVolumeIds = new Set();
	const volumesToAdd = [];
	issues.forEach(issue => {
		if (!issue.in_library && !seenVolumeIds.has(issue.volume_id)) {
			seenVolumeIds.add(issue.volume_id);
			volumesToAdd.push(issue);
		}
	});

	if (volumesToAdd.length === 0) {
		return;
	}

	const rootFolderId = parseInt(calendar_els.add_window.root_folder.value);
	if (!rootFolderId) {
		console.error('No root folder configured');
		return;
	}

	const prefs = getLocalStorage(
		'monitor_new_volume', 'monitor_new_issues', 'monitoring_scheme'
	);
	const monitor = prefs.monitor_new_volume !== undefined
		? prefs.monitor_new_volume : true;
	const monitorNewIssues = prefs.monitor_new_issues !== undefined
		? prefs.monitor_new_issues : true;
	const monitoringScheme = prefs.monitoring_scheme || 'all';

	// Fire-and-forget: send all requests, no UI blocking
	const btn = calendar_els.toolbar.add_all;
	btn.disabled = true;
	btn.querySelector('p').textContent = `Adding ${volumesToAdd.length}...`;

	usingApiKey().then(api_key => {
		const promises = volumesToAdd.map(vol => {
			const data = {
				'comicvine_id': vol.volume_id,
				'root_folder_id': rootFolderId,
				'monitor': monitor,
				'monitoring_scheme': monitoringScheme,
				'monitor_new_issues': monitorNewIssues,
				'volume_folder': '',
				'special_version': null,
				'auto_search': false
			};
			return sendAPI('POST', '/volumes', api_key, {}, data)
				.catch(() => {});
		});

		Promise.all(promises).then(() => {
			btn.disabled = false;
			btn.querySelector('p').textContent = 'Add All';
		});
	});
};

calendar_els.toolbar.add_all.onclick = addAllUnmonitored;

//
// Initialize
//
// Restore view mode preference
const savedView = getLocalStorage('calendar_view_mode');
if (savedView.calendar_view_mode === 'month') {
	viewMode = 'month';
	calendar_els.header.view_week.classList.remove('active');
	calendar_els.header.view_month.classList.add('active');
}

updatePeriodLabel();

restoreFormatPrefs();

usingApiKey().then(api_key => {
	loadPublishers(api_key);
	loadRootFolders(api_key);
	fetchCalendar(api_key);
});
