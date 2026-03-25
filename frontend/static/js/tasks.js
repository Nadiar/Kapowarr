const TaskEls = {
	pre_build: {
		task: document.querySelector('.pre-build-els .task-entry'),
		history: document.querySelector('.pre-build-els .history-entry')
	},
	intervals: document.querySelector('#task-intervals'),
	history: document.querySelector('#history'),
	buttons: {
		refresh: document.querySelector('#refresh-button'),
		clear: document.querySelector('#clear-button')
	}
};

//
// Task planning
//
function formatDuration(seconds) {
	if (seconds == null) return '\u2014';
	if (seconds < 1) return '< 1s';
	if (seconds < 60) return `${seconds}s`;
	if (seconds < 3600) {
		const m = Math.floor(seconds / 60);
		const s = seconds % 60;
		return s > 0 ? `${m}m ${s}s` : `${m}m`;
	}
	const h = Math.floor(seconds / 3600);
	const m = Math.floor((seconds % 3600) / 60);
	return m > 0 ? `${h}h ${m}m` : `${h}h`;
};

function convertInterval(interval) {
	result = Math.round(interval / 3600); // seconds -> hours
	return `${result} hours`;
};

function convertTime(epoch, future) {
	result = Math.round(Math.abs(Date.now() / 1000 - epoch) / 3600); // delta hours
	if (future) return `in ${result} hours`;
	else return `${result} hours ago`;
};

function fillPlanning(api_key) {
	fetchAPI('/system/tasks/planning', api_key)
	.then(json => {
		TaskEls.intervals.innerHTML = '';
		json.result.forEach(task => {
			const entry = TaskEls.pre_build.task.cloneNode(true);
			entry.dataset.task_name = task.task_name;

			entry.querySelector('.task-name').innerText = task.display_name;
			entry.querySelector('.interval-column').innerText =
				convertInterval(task.interval);
			entry.querySelector('.prev-column').innerText =
				convertTime(task.last_run, false);
			entry.querySelector('.next-column').innerText =
				convertTime(task.next_run, true);
			entry.querySelector('.task-info-btn').title =
				task.description || 'No description available';
			entry.querySelector('button[title="Run the task"]').onclick =
				e => sendAPI('POST', '/system/tasks', api_key, {}, {'cmd': task.task_name})

			TaskEls.intervals.appendChild(entry);
		});
		mapButtons();
	});
};

//
// Task history
//
function fillHistory(api_key) {
	fetchAPI('/system/tasks/history', api_key)
	.then(json => {
		TaskEls.history.innerHTML = '';
		json.result.forEach(obj => {
			const entry = TaskEls.pre_build.history.cloneNode(true);

			entry.querySelector('.title-column').innerText = obj.display_title;

			var d = new Date(obj.run_at * 1000);
			var formatted_date = d.toLocaleString('en-CA').slice(0,10) + ' ' + d.toTimeString().slice(0,5)
			entry.querySelector('.date-column').innerText = formatted_date;
			entry.querySelector('.duration-column').innerText =
				formatDuration(obj.duration_seconds);

			TaskEls.history.appendChild(entry);
		});
	});
};

function clearHistory(api_key) {
	sendAPI('DELETE', '/system/tasks/history', api_key)
	TaskEls.history.innerHTML = '';
};

// code run on load

usingApiKey()
.then(api_key => {
	fillHistory(api_key);
	fillPlanning(api_key);
	TaskEls.buttons.refresh.onclick = e => fillHistory(api_key);
	TaskEls.buttons.clear.onclick = e => clearHistory(api_key);
});
