-- Structure de la base créée par Hangar 1.0.0 (commit 27dd700), sans données : test de migration (tests/test_exploitation.py).
CREATE TABLE absence (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	start_date DATE NOT NULL, 
	end_date DATE NOT NULL, 
	kind VARCHAR(30), 
	note VARCHAR(255), 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE activity (
	id INTEGER NOT NULL, 
	issue_id INTEGER, 
	project_id INTEGER, 
	actor_id INTEGER, 
	verb VARCHAR(30) NOT NULL, 
	field VARCHAR(40), 
	old_value TEXT, 
	new_value TEXT, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(actor_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE api_token (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	prefix VARCHAR(12) NOT NULL, 
	created_at DATETIME, 
	last_used_at DATETIME, 
	expires_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE, 
	UNIQUE (token_hash)
);
CREATE TABLE attachment (
	id INTEGER NOT NULL, 
	issue_id INTEGER NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	stored_name VARCHAR(255) NOT NULL, 
	size INTEGER, 
	mimetype VARCHAR(120), 
	uploaded_by_id INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(uploaded_by_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE bilan_report (
	id INTEGER NOT NULL, 
	project_id INTEGER, 
	title VARCHAR(200) NOT NULL, 
	period_start DATE, 
	period_end DATE, 
	data_json TEXT, 
	highlights TEXT, 
	difficulties TEXT, 
	decisions TEXT, 
	next_steps TEXT, 
	created_by_id INTEGER, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE comment (
	id INTEGER NOT NULL, 
	issue_id INTEGER NOT NULL, 
	author_id INTEGER, 
	body TEXT NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(author_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE cycle (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	description TEXT, 
	start_date DATE, 
	end_date DATE, 
	owner_id INTEGER, 
	created_at DATETIME, 
	archived_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(owner_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE email_message (
	id INTEGER NOT NULL, 
	to_address VARCHAR(200) NOT NULL, 
	to_user_id INTEGER, 
	subject VARCHAR(300) NOT NULL, 
	body_text TEXT, 
	body_html TEXT, 
	kind VARCHAR(20), 
	status VARCHAR(10), 
	error TEXT, 
	created_at DATETIME, 
	sent_at DATETIME, 
	created_by_id INTEGER, 
	reply_to VARCHAR(200), 
	PRIMARY KEY (id), 
	FOREIGN KEY(to_user_id) REFERENCES user (id) ON DELETE SET NULL, 
	FOREIGN KEY(created_by_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE favorite (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	entity_type VARCHAR(20) NOT NULL, 
	entity_id INTEGER NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (user_id, entity_type, entity_id), 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE issue (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sequence_id INTEGER NOT NULL, 
	title VARCHAR(255) NOT NULL, 
	description TEXT, 
	state_id INTEGER, 
	priority VARCHAR(10), 
	start_date DATE, 
	target_date DATE, 
	estimate INTEGER, 
	parent_id INTEGER, 
	cycle_id INTEGER, 
	sort_order FLOAT, 
	external_id VARCHAR(60), 
	created_by_id INTEGER, 
	created_at DATETIME, 
	updated_at DATETIME, 
	completed_at DATETIME, 
	archived_at DATETIME, 
	intake_status VARCHAR(12), 
	intake_snoozed_until DATE, 
	intake_duplicate_of_id INTEGER, 
	intake_source VARCHAR(60), 
	PRIMARY KEY (id), 
	UNIQUE (project_id, sequence_id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(state_id) REFERENCES state (id) ON DELETE SET NULL, 
	FOREIGN KEY(parent_id) REFERENCES issue (id) ON DELETE SET NULL, 
	FOREIGN KEY(cycle_id) REFERENCES cycle (id) ON DELETE SET NULL, 
	FOREIGN KEY(created_by_id) REFERENCES user (id) ON DELETE SET NULL, 
	FOREIGN KEY(intake_duplicate_of_id) REFERENCES issue (id) ON DELETE SET NULL
);
CREATE TABLE issue_assignees (
	issue_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	PRIMARY KEY (issue_id, user_id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE issue_labels (
	issue_id INTEGER NOT NULL, 
	label_id INTEGER NOT NULL, 
	PRIMARY KEY (issue_id, label_id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(label_id) REFERENCES label (id) ON DELETE CASCADE
);
CREATE TABLE issue_link (
	id INTEGER NOT NULL, 
	issue_id INTEGER NOT NULL, 
	url VARCHAR(1000) NOT NULL, 
	title VARCHAR(255), 
	created_by_id INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(created_by_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE issue_modules (
	issue_id INTEGER NOT NULL, 
	module_id INTEGER NOT NULL, 
	PRIMARY KEY (issue_id, module_id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(module_id) REFERENCES module (id) ON DELETE CASCADE
);
CREATE TABLE issue_relation (
	id INTEGER NOT NULL, 
	issue_id INTEGER NOT NULL, 
	related_id INTEGER NOT NULL, 
	relation_type VARCHAR(20), 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(related_id) REFERENCES issue (id) ON DELETE CASCADE
);
CREATE TABLE issue_subscribers (
	issue_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	PRIMARY KEY (issue_id, user_id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE label (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	color VARCHAR(10), 
	description VARCHAR(255), 
	parent_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(parent_id) REFERENCES label (id) ON DELETE SET NULL
);
CREATE TABLE mail_config (
	id INTEGER NOT NULL, 
	enabled BOOLEAN, 
	mode VARCHAR(10), 
	smtp_host VARCHAR(200), 
	smtp_port INTEGER, 
	smtp_security VARCHAR(10), 
	smtp_user VARCHAR(200), 
	smtp_password VARCHAR(300), 
	from_name VARCHAR(120), 
	from_address VARCHAR(200), 
	base_url VARCHAR(200), 
	reminder_hour INTEGER, 
	digest_weekday INTEGER, 
	bilan_weekday INTEGER, 
	bilan_hour INTEGER, 
	send_notifications BOOLEAN, 
	send_reminders BOOLEAN, 
	send_digest BOOLEAN, 
	send_bilan BOOLEAN, 
	last_reminder_date DATE, 
	last_digest_date DATE, 
	last_bilan_date DATE, 
	PRIMARY KEY (id)
);
CREATE TABLE module (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	description TEXT, 
	status VARCHAR(20), 
	lead_id INTEGER, 
	start_date DATE, 
	target_date DATE, 
	created_at DATETIME, 
	archived_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(lead_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE module_members (
	module_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	PRIMARY KEY (module_id, user_id), 
	FOREIGN KEY(module_id) REFERENCES module (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE notification (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	actor_id INTEGER, 
	issue_id INTEGER, 
	project_id INTEGER, 
	kind VARCHAR(20), 
	message VARCHAR(500) NOT NULL, 
	url VARCHAR(300), 
	created_at DATETIME, 
	read_at DATETIME, 
	archived_at DATETIME, 
	snoozed_until DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE, 
	FOREIGN KEY(actor_id) REFERENCES user (id) ON DELETE SET NULL, 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE
);
CREATE TABLE page (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	parent_id INTEGER, 
	title VARCHAR(255) NOT NULL, 
	emoji VARCHAR(16), 
	content TEXT, 
	owner_id INTEGER, 
	is_public BOOLEAN, 
	is_locked BOOLEAN, 
	created_at DATETIME, 
	updated_at DATETIME, 
	updated_by_id INTEGER, 
	archived_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(parent_id) REFERENCES page (id) ON DELETE SET NULL, 
	FOREIGN KEY(owner_id) REFERENCES user (id) ON DELETE SET NULL, 
	FOREIGN KEY(updated_by_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE page_version (
	id INTEGER NOT NULL, 
	page_id INTEGER NOT NULL, 
	title VARCHAR(255), 
	content TEXT, 
	author_id INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(page_id) REFERENCES page (id) ON DELETE CASCADE, 
	FOREIGN KEY(author_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE project (
	id INTEGER NOT NULL, 
	name VARCHAR(160) NOT NULL, 
	identifier VARCHAR(10) NOT NULL, 
	description TEXT, 
	emoji VARCHAR(16), 
	color VARCHAR(10), 
	network VARCHAR(10), 
	lead_id INTEGER, 
	default_assignee_id INTEGER, 
	created_by_id INTEGER, 
	start_date DATE, 
	target_date DATE, 
	created_at DATETIME, 
	archived_at DATETIME, 
	issue_sequence INTEGER, 
	cycles_enabled BOOLEAN, 
	modules_enabled BOOLEAN, 
	views_enabled BOOLEAN, 
	pages_enabled BOOLEAN, 
	intake_enabled BOOLEAN, 
	time_tracking_enabled BOOLEAN, 
	estimate_type VARCHAR(20), 
	auto_archive_months INTEGER, 
	auto_close_months INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (identifier), 
	FOREIGN KEY(lead_id) REFERENCES user (id) ON DELETE SET NULL, 
	FOREIGN KEY(default_assignee_id) REFERENCES user (id) ON DELETE SET NULL, 
	FOREIGN KEY(created_by_id) REFERENCES user (id) ON DELETE SET NULL
);
CREATE TABLE project_member (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	role VARCHAR(20), 
	joined_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (project_id, user_id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE reaction (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	emoji VARCHAR(16) NOT NULL, 
	issue_id INTEGER, 
	comment_id INTEGER, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE, 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(comment_id) REFERENCES comment (id) ON DELETE CASCADE
);
CREATE TABLE saved_view (
	id INTEGER NOT NULL, 
	project_id INTEGER, 
	name VARCHAR(160) NOT NULL, 
	description VARCHAR(500), 
	"query" TEXT, 
	owner_id INTEGER, 
	is_public BOOLEAN, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE, 
	FOREIGN KEY(owner_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE state (
	id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	color VARCHAR(10), 
	"group" VARCHAR(20), 
	description VARCHAR(255), 
	sequence FLOAT, 
	is_default BOOLEAN, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE
);
CREATE TABLE sticky (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	content TEXT, 
	color VARCHAR(10), 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE user (
	id INTEGER NOT NULL, 
	email VARCHAR(200) NOT NULL, 
	username VARCHAR(60) NOT NULL, 
	first_name VARCHAR(80), 
	last_name VARCHAR(80), 
	password_hash VARCHAR(255) NOT NULL, 
	role VARCHAR(20), 
	job_title VARCHAR(120), 
	bio TEXT, 
	avatar_color VARCHAR(10), 
	theme VARCHAR(10), 
	capacity_hours INTEGER, 
	active BOOLEAN, 
	must_change_password BOOLEAN, 
	created_at DATETIME, 
	last_login_at DATETIME, 
	last_seen_notifications DATETIME, 
	contact_email VARCHAR(200), 
	email_prefs VARCHAR(10), 
	email_reminders BOOLEAN, 
	PRIMARY KEY (id), 
	UNIQUE (email), 
	UNIQUE (username)
);
CREATE TABLE work_log (
	id INTEGER NOT NULL, 
	issue_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	minutes INTEGER NOT NULL, 
	date DATE, 
	description VARCHAR(255), 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(issue_id) REFERENCES issue (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
);
CREATE TABLE workspace (
	id INTEGER NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	emoji VARCHAR(16), 
	description TEXT, 
	allow_signup BOOLEAN, 
	week_starts_monday BOOLEAN, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_activity_created_at ON activity (created_at);
CREATE INDEX ix_activity_issue_id ON activity (issue_id);
CREATE INDEX ix_activity_project_id ON activity (project_id);
CREATE INDEX ix_comment_issue_id ON comment (issue_id);
CREATE INDEX ix_email_message_created_at ON email_message (created_at);
CREATE INDEX ix_email_message_status ON email_message (status);
CREATE INDEX ix_issue_project_id ON issue (project_id);
CREATE INDEX ix_notification_created_at ON notification (created_at);
CREATE INDEX ix_notification_user_id ON notification (user_id);
