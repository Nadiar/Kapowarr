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
	},
	edit: {
		form: document.querySelector("#task-form"),
		preset: document.querySelector('#task-schedule-preset'),
		custom: document.querySelector('#custom-schedule'),
		error: document.querySelector('#schedule-error')
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

const scheduleDescriptions = {
	'0 * * * *': 'Every hour',
	'0 0 * * *': 'Once per day',
	'0 0 * * 1': 'Once per week',
	'0,30 * * * *': 'Every half hour'
};

const weekdays = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday',
	'Friday', 'Saturday'];

// Fork: readable text for the simple cron schedules the fork's tasks use
function describeSchedule(schedule) {
	if (scheduleDescriptions[schedule])
		return scheduleDescriptions[schedule];

	const fields = schedule.trim().split(/\s+/);
	if (fields.length !== 5 || !/^\d+$/.test(fields[0]))
		return schedule;

	const [minute, hour, day, month, weekday] = fields;
	const mm = minute.padStart(2, '0');
	if (hour === '*' && day === '*' && month === '*' && weekday === '*')
		return `Every hour at :${mm}`;

	if (!/^\d+$/.test(hour) || month !== '*')
		return schedule;

	const time = `${hour.padStart(2, '0')}:${mm}`;
	if (day === '*' && weekday === '*')
		return `Daily at ${time}`;

	if (day === '*' && /^[0-6]$/.test(weekday))
		return `${weekdays[weekday]}s at ${time}`;

	const everyNDays = day.match(/^\*\/(\d+)$/);
	if (everyNDays && weekday === '*')
		return `Every ${everyNDays[1]} days at ${time}`;

	return schedule;
};

function convertTime(epoch, future) {
	if (epoch === null)
		return 'Never';

	const deltaSeconds = Math.round(Math.abs(Date.now() / 1000 - epoch));
	const deltaMinutes = Math.round(deltaSeconds / 60);
	const deltaHours = Math.round(deltaMinutes / 60);
	const deltaDays = Math.round(deltaHours / 24);

	let unit;
	let value;
	if (deltaSeconds == 1) {
		unit = "second";
		value = deltaSeconds;
	}
	else if (deltaSeconds < 60) {
		unit = "seconds";
		value = deltaSeconds;
	}
	else if (deltaMinutes == 1) {
		unit = "minute";
		value = deltaMinutes;
	}
	else if (deltaMinutes < 60) {
		unit = "minutes";
		value = deltaMinutes;
	}
	else if (deltaHours == 1) {
		unit = "hour";
		value = deltaHours;
	}
	else if (deltaHours < 24) {
		unit = "hours";
		value = deltaHours;
	}
	else if (deltaDays == 1) {
		unit = "day";
		value = deltaDays;
	}
	else {
		unit = "days"
		value = deltaDays;
	}

	if (future)
		return `in ${value} ${unit}`;
	else
		return `${value} ${unit} ago`;
};

let currentScheduleTask = null;

function openScheduleEditor(taskName, schedule) {
	currentScheduleTask = taskName;
	TaskEls.edit.preset.value = Object.keys(scheduleDescriptions).includes(schedule) ? schedule : "";
	TaskEls.edit.custom.value = schedule;
	hide([TaskEls.edit.error]);
	showWindow("task-window");
};

function submitScheduleEditor(apiKey) {
	const schedule = TaskEls.edit.custom.value.trim();
	hide([TaskEls.edit.error]);
	sendAPI('PUT', '/system/tasks/planning', apiKey, {}, {
		task_name: currentScheduleTask,
		schedule: schedule
	})
	.then(() => {
		currentScheduleTask = null;
		fillPlanning(apiKey);
		closeWindow();
	})
	.catch((e) => {
		if (e.status === 400) {
			hide([], [TaskEls.edit.error])
		}
		else
			console.log(e)
	});
};

function fillPlanning(api_key) {
	fetchAPI('/system/tasks/planning', api_key)
	.then(json => {
		TaskEls.intervals.innerHTML = '';
		json.result.forEach(task => {
			const entry = TaskEls.pre_build.task.cloneNode(true);
			entry.dataset.task_name = task.task_name;

			entry.querySelector('.task-name').innerText = task.display_name;
			entry.querySelector('.schedule-column').innerText =
				describeSchedule(task.schedule);
			entry.querySelector('.schedule-column').title = task.schedule;
			entry.querySelector('.prev-column').innerText =
				convertTime(task.last_run, false);
			entry.querySelector('.next-column').innerText =
				convertTime(task.next_run, true);
			entry.querySelector('.task-info-btn').title =
				task.description || 'No description available';
			entry.querySelector('.actions-column button:first-of-type').onclick = () => {
				sendAPI('POST', '/system/tasks', api_key, {}, {'cmd': task.task_name})
				.then(() => refreshPage(api_key))
			};
			entry.querySelector('.actions-column button:last-of-type').onclick =
				() => openScheduleEditor(task.task_name, task.schedule);

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

function refreshPage(apiKey) {
	fillPlanning(apiKey)
	fillHistory(apiKey)
}

// code run on load

usingApiKey()
.then(api_key => {
	refreshPage(api_key);
	TaskEls.buttons.refresh.onclick = () => refreshPage(api_key);
	TaskEls.buttons.clear.onclick = () => clearHistory(api_key);

	TaskEls.edit.preset.onchange = e => {
		if (e.target.value === '') {
			TaskEls.edit.custom.value = '';
		} else {
			TaskEls.edit.custom.value = e.target.value;
		}
	};

	TaskEls.edit.form.onsubmit = (e) => {
		e.preventDefault();
		submitScheduleEditor(api_key);
	};
});
