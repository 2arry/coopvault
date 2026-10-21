app_name = "coopvault"
app_title = "CoopVault"
app_publisher = "Larry Adah"
app_description = "Cooperative savings, contributions and member loans management"
app_email = "larryadah@gmail.com"
app_license = "mit"
required_apps = ["frappe"]

# Tile on the desk's apps screen
add_to_apps_screen = [
	{
		"name": "coopvault",
		"logo": "/assets/coopvault/logo.svg",
		"title": "CoopVault",
		"route": "/desk/coopvault",
	}
]
app_logo_url = "/assets/coopvault/logo.svg"

# Installation
# ------------

before_install = "coopvault.install.before_install"
after_install = "coopvault.install.after_install"
after_migrate = "coopvault.install.after_migrate"

# Document Events
# ---------------

doc_events = {
	"Coop Member": {
		"after_insert": "coopvault.ledger.on_member_change",
		"on_update": "coopvault.ledger.on_member_change",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"daily": [
		"coopvault.ledger.refresh_all_cooperatives",
	],
	"cron": {
		# Autosave charges go out at 09:00 in the site time zone (Africa/Lagos by default)
		"0 9 * * *": [
			"coopvault.api.payments.run_autosave",
		],
	},
}
