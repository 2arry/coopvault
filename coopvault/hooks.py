app_name = "coopvault"
app_title = "CoopVault"
app_publisher = "Larry Adah"
app_description = "Cooperative savings, contributions and member loans management"
app_email = "larryadah@gmail.com"
app_license = "mit"
required_apps = ["frappe"]

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
}
