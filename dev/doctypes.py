"""Generate the app's DocType JSON files from a compact spec (dev helper, not shipped).

usage: python dev/doctypes.py coopvault        (from apps/coopvault)
Edit the spec here, regenerate, then `bench migrate`. Controllers (.py) are only created when missing.
"""
import datetime
import json
import os
import sys

APP = sys.argv[1]  # path to coopvault package dir
MODULE = "CoopVault"
TS = "2026-10-21 18:00:00.000000"


def F(fieldname, fieldtype="Data", label=None, **kw):
	d = {"fieldname": fieldname, "fieldtype": fieldtype}
	if fieldtype not in ("Section Break", "Column Break") or label:
		d["label"] = label or fieldname.replace("_", " ").title()
	for k, v in kw.items():
		if isinstance(v, bool):
			v = 1 if v else 0
		d[k] = v
	return d


SB = lambda n, label=None, **kw: F(n, "Section Break", label, **kw)
CB = lambda n: F(n, "Column Break")
# Bank Transfer / Cash remain only for records made before online-only payments
PAY_METHODS = "Paystack\nOpening Balance\nBank Transfer\nCash"
RO = dict(read_only=True)
PAYOUT_SNAPSHOT = [
	F("payout_bank_name", "Data", "Sent To Bank", **RO),
	F("payout_account_number", "Data", "Sent To Account", **RO),
	F("payout_account_name", "Data", "Account Name", **RO),
]

DOCTYPES = [
	dict(
		name="Cooperative", autoname="format:COOP-{#####}", naming_rule="Expression",
		title_field="cooperative_name", search_fields="cooperative_name,join_code",
		fields=[
			F("cooperative_name", label="Cooperative Name", reqd=True, in_list_view=True, bold=True),
			F("status", "Select", options="Active\nInactive", default="Active", in_list_view=True, in_standard_filter=True),
			F("description", "Small Text"),
			CB("cb1"),
			F("join_code", label="Join Code", unique=True, read_only=True, in_list_view=True,
				description="Share this code with members so they can request to join"),
			F("currency", default="NGN"),
			F("created_by_user", "Link", "Created By", options="User", read_only=True),
			SB("rules_section", "Contributions"),
			F("contribution_amount", "Currency", "Current Contribution", options="currency", **RO,
				description="From the latest contribution plan"),
			F("contribution_frequency", "Select", "Current Frequency", options="Weekly\nMonthly\nQuarterly\nFlexible",
				default="Monthly", **RO),
			F("start_date", "Date", "Contributions Start",
				description="Members owe from the first full period after this date or the day they joined, whichever is later"),
			F("contribution_plans", "Table", "Contribution Plan History", options="Coop Contribution Plan",
				description="Each change applies from its effective date, so past periods keep the amount that applied then"),
			SB("loan_section", "Loans"),
			F("loan_multiplier", "Float", "Borrow Limit Multiplier", default="1", non_negative=True,
				description="Share of a member's savings they can borrow. 1 = members can never borrow more than they have saved."),
			F("interest_rate", "Percent", "Loan Interest Rate (flat %)", default="0"),
			CB("cb2"),
			F("max_loan_tenure_months", "Int", "Max Loan Tenure (Months)", default="12"),
			SB("bank_section", "Settlement Bank Account",
				description="Where online payments settle (through a Paystack subaccount)"),
			F("bank_name", **RO),
			F("settlement_bank_code", label="Bank Code", **RO),
			F("bank_changed_on", "Datetime", "Last Changed", **RO),
			CB("cb3"),
			F("account_number", **RO),
			F("account_verified", "Check", "Account Verified with Paystack", **RO),
			F("bank_changed_by", "Link", "Changed By", options="User", **RO),
			CB("cb4"),
			F("account_name", **RO),
			F("paystack_subaccount_code", label="Paystack Subaccount", **RO),
			SB("online_section", "Online Payments"),
			F("accept_online_payments", "Check", "Accept Online Payments", default="1"),
			CB("cb6"),
			F("fee_bearer", "Select", "Who Pays Paystack Fees", options="Member\nCooperative", default="Member",
				description="Member: the fee is added at checkout so the full amount reaches the pool"),
			SB("books_section", "Books"),
			F("fiscal_year_start", "Select", "Financial Year Starts",
				options="January\nFebruary\nMarch\nApril\nMay\nJune\nJuly\nAugust\nSeptember\nOctober\nNovember\nDecember",
				default="January"),
			F("reserve_percent", "Percent", "Reserve Fund Share of Surplus", default="25",
				description="Set aside from each year's surplus before sharing out. The Cooperative Societies law asks for at least 25%."),
			CB("cb_books"),
			F("books_closed_until", "Date", "Books Closed Up To", **RO,
				description="Nothing can be posted on or before this date. Set by a share-out or by closing the books."),
			F("chart_ready", "Check", "Chart of Accounts Created", **RO),
			SB("summary_section", "Pool Summary", collapsible=True),
			F("member_count", "Int", "Active Members", **RO),
			F("total_contributions", "Currency", options="currency", **RO),
			F("total_repayments", "Currency", options="currency", **RO),
			F("total_withdrawn", "Currency", options="currency", **RO),
			CB("cb5"),
			F("total_disbursed", "Currency", "Total Loans Disbursed", options="currency", **RO),
			F("loans_outstanding", "Currency", "Loans Outstanding", options="currency", **RO),
			F("pool_balance", "Currency", "Pool Balance (Cash at hand)", options="currency", bold=True, **RO),
		],
	),
	dict(
		name="Coop Contribution Plan", istable=True,
		fields=[
			F("effective_from", "Date", reqd=True, in_list_view=True),
			F("amount", "Currency", reqd=True, in_list_view=True, non_negative=True),
			F("frequency", "Select", options="Weekly\nMonthly\nQuarterly\nFlexible", reqd=True, in_list_view=True),
			F("changed_by", "Link", options="User", in_list_view=True, **RO),
		],
	),
	dict(
		name="Coop Member", autoname="hash", title_field="full_name", search_fields="full_name,user,cooperative",
		fields=[
			F("cooperative", "Link", options="Cooperative", reqd=True, in_list_view=True, in_standard_filter=True),
			F("user", "Link", options="User", reqd=True, in_standard_filter=True, search_index=True),
			F("full_name", in_list_view=True, fetch_from="user.full_name", fetch_if_empty=True),
			F("phone"),
			CB("cb1"),
			F("role", "Select", options="Member\nTreasurer\nAccountant\nLoan Officer\nChairman\nAdmin", default="Member",
				in_list_view=True, in_standard_filter=True),
			F("member_number", "Data", "Member No.", **RO, in_list_view=True,
				description="Given when the membership is approved"),
			F("status", "Select", options="Pending\nActive\nSuspended\nLeft", default="Pending", in_list_view=True, in_standard_filter=True),
			F("joined_on", "Date"),
			F("security_hold_until", "Datetime", "Security Hold Until", **RO,
				description="After a password reset or phone change, payout changes, loans and withdrawals wait until this time"),
			F("phone_changed_on", "Datetime", "Phone Last Changed", **RO),
			SB("balances_section", "Balances"),
			F("total_contributed", "Currency", "Savings Balance", **RO),
			F("total_repaid", "Currency", **RO),
			CB("cb2"),
			F("loan_outstanding", "Currency", "Loan Balance (to pay back)", **RO),
			F("borrow_limit", "Currency", "Available to Borrow", bold=True, **RO),
			SB("payout_section", "Payout Account (where loans and withdrawals are sent)", collapsible=True),
			F("payout_bank_name", label="Bank", **RO),
			F("payout_bank_code", label="Bank Code", **RO),
			F("payout_changed_on", "Datetime", "Last Changed", **RO),
			CB("cb3"),
			F("payout_account_number", label="Account Number", **RO),
			F("payout_account_name", label="Account Name", **RO),
			SB("autosave_section", "Autosave", collapsible=True),
			F("autosave_enabled", "Check", "Autosave On", **RO),
			F("autosave_amount", "Currency", **RO),
			F("autosave_authorization", "Link", "Payment Method", options="Coop Payment Authorization", **RO),
			CB("cb4"),
			F("autosave_next_date", "Date", "Next Autosave", **RO),
			F("autosave_failures", "Int", **RO),
			F("autosave_last_error", "Small Text", **RO),
			F("autosave_reminded_for", "Date", "Reminder Sent For", **RO),
			SB("reminder_section", "Reminders", collapsible=True),
			F("due_reminded_for", "Date", "Due Reminder Sent For Period", **RO),
			SB("dva_section", "Dedicated Account", collapsible=True),
			F("paystack_customer_code", label="Paystack Customer", **RO),
			F("dva_status", "Select", "Status", options="\nRequested\nAssigned\nFailed\nClosed", **RO,
				description="Closed: the member left and the account number was switched off"),
			F("dva_error", "Small Text", "Error", **RO),
			CB("cb5"),
			F("dva_account_number", label="Account Number", **RO, search_index=True),
			F("dva_id", label="Paystack Dedicated Account ID", **RO),
			F("dva_account_name", label="Account Name", **RO),
			F("dva_bank_name", label="Bank", **RO),
		],
	),
	dict(
		name="Coop Contribution", autoname="CONT-.YYYY.-.#####", naming_rule="By \"Naming Series\" field",
		autoname_series=True, title_field="member_name", search_fields="member_name,reference",
		fields=[
			F("naming_series", "Select", options="CONT-.YYYY.-.#####", default="CONT-.YYYY.-.#####", hidden=True),
			F("cooperative", "Link", options="Cooperative", reqd=True, in_standard_filter=True),
			F("member", "Link", options="Coop Member", reqd=True, in_standard_filter=True),
			F("member_name", fetch_from="member.full_name", in_list_view=True, **RO),
			F("user", "Link", options="User", fetch_from="member.user", **RO),
			CB("cb1"),
			F("amount", "Currency", reqd=True, in_list_view=True, non_negative=True),
			F("contribution_date", "Date", reqd=True, default="Today", in_list_view=True),
			F("status", "Select", options="Pending\nConfirmed\nRejected\nReversed", default="Pending", in_list_view=True,
				in_standard_filter=True,
				description="Pending is only used for opening balances waiting for the member to agree"),
			SB("payment_section", "Payment"),
			F("payment_method", "Select", options=PAY_METHODS, default="Paystack"),
			F("reference", "Data", "Payment Reference"),
			F("proposed_by", "Link", "Opening Balance Proposed By", options="User", **RO),
			CB("cb2"),
			F("notes", "Small Text"),
			F("reviewed_by", "Link", options="User", **RO),
			F("reviewed_on", "Datetime", **RO),
			F("review_note", "Small Text"),
			F("recorded_by", "Link", "Recorded By", options="User", **RO,
				description="Staff member who entered an offline payment; another one approves it"),
		],
	),
	dict(
		name="Coop Loan", autoname="LOAN-.YYYY.-.#####", naming_rule="By \"Naming Series\" field",
		autoname_series=True, title_field="member_name", search_fields="member_name",
		fields=[
			F("naming_series", "Select", options="LOAN-.YYYY.-.#####", default="LOAN-.YYYY.-.#####", hidden=True),
			F("cooperative", "Link", options="Cooperative", reqd=True, in_standard_filter=True),
			F("member", "Link", options="Coop Member", reqd=True, in_standard_filter=True),
			F("member_name", fetch_from="member.full_name", in_list_view=True, **RO),
			F("user", "Link", options="User", fetch_from="member.user", **RO),
			F("purpose", "Small Text"),
			CB("cb1"),
			F("status", "Select", options="Pending\nApproved\nSent\nActive\nRepaid\nRejected\nCancelled", default="Pending",
				in_list_view=True, in_standard_filter=True,
				description="Sent: the treasurer has transferred the money and the borrower still has to confirm it arrived"),
			F("application_date", "Date", default="Today"),
			F("tenure_months", "Int", "Tenure (Months)", default="1"),
			F("disbursed_on", "Date", "Sent On", **RO),
			F("received_on", "Date", "Borrower Confirmed On", **RO,
				description="Repayments are scheduled from this date"),
			F("due_date", "Date", **RO),
			SB("amounts_section", "Amounts"),
			F("principal", "Currency", "Amount Borrowed", reqd=True, in_list_view=True, non_negative=True),
			F("interest_rate", "Percent", "Interest Rate (flat %)", **RO),
			F("interest_amount", "Currency", **RO),
			CB("cb2"),
			F("total_payable", "Currency", bold=True, **RO),
			F("amount_repaid", "Currency", **RO),
			F("balance", "Currency", "Balance to Pay Back", bold=True, in_list_view=True, **RO),
			SB("transfer_section", "Transfer"),
			F("transfer_reference", "Data", **RO),
			*PAYOUT_SNAPSHOT,
			CB("cb4"),
			F("receipt_disputed", "Check", "Borrower Says Not Received", **RO),
			F("dispute_note", "Small Text", **RO),
			SB("review_section", "Review"),
			F("reviewed_by", "Link", options="User", **RO),
			F("reviewed_on", "Datetime", **RO),
			CB("cb3"),
			F("review_note", "Small Text"),
		],
	),
	dict(
		name="Coop Loan Repayment", autoname="REP-.YYYY.-.#####", naming_rule="By \"Naming Series\" field",
		autoname_series=True, title_field="member_name", search_fields="member_name,loan,reference",
		fields=[
			F("naming_series", "Select", options="REP-.YYYY.-.#####", default="REP-.YYYY.-.#####", hidden=True),
			F("loan", "Link", options="Coop Loan", reqd=True, in_list_view=True, in_standard_filter=True),
			F("cooperative", "Link", options="Cooperative", fetch_from="loan.cooperative", read_only=True, in_standard_filter=True),
			F("member", "Link", options="Coop Member", fetch_from="loan.member", **RO),
			F("member_name", fetch_from="loan.member_name", in_list_view=True, **RO),
			F("user", "Link", options="User", fetch_from="loan.user", **RO),
			CB("cb1"),
			F("amount", "Currency", reqd=True, in_list_view=True, non_negative=True),
			F("payment_date", "Date", reqd=True, default="Today"),
			F("status", "Select", options="Pending\nConfirmed\nRejected\nReversed", default="Confirmed", in_list_view=True,
				in_standard_filter=True),
			SB("payment_section", "Payment"),
			F("payment_method", "Select", options=PAY_METHODS, default="Paystack"),
			F("reference", "Data", "Payment Reference"),
			CB("cb2"),
			F("notes", "Small Text"),
			F("reviewed_by", "Link", options="User", **RO),
			F("reviewed_on", "Datetime", **RO),
			F("review_note", "Small Text"),
			F("recorded_by", "Link", "Recorded By", options="User", **RO,
				description="Staff member who entered an offline payment; another one approves it"),
		],
	),
	dict(
		name="Coop Withdrawal", autoname="WDR-.YYYY.-.#####", naming_rule="By \"Naming Series\" field",
		autoname_series=True, title_field="member_name", search_fields="member_name",
		fields=[
			F("naming_series", "Select", options="WDR-.YYYY.-.#####", default="WDR-.YYYY.-.#####", hidden=True),
			F("cooperative", "Link", options="Cooperative", reqd=True, in_standard_filter=True),
			F("member", "Link", options="Coop Member", reqd=True, in_standard_filter=True),
			F("member_name", fetch_from="member.full_name", in_list_view=True, **RO),
			F("user", "Link", options="User", fetch_from="member.user", **RO),
			F("reason", "Small Text"),
			CB("cb1"),
			F("amount", "Currency", reqd=True, in_list_view=True, non_negative=True),
			F("status", "Select", options="Pending\nApproved\nSent\nCompleted\nRejected\nCancelled", default="Pending",
				in_list_view=True, in_standard_filter=True),
			F("requested_on", "Date", default="Today"),
			F("sent_on", "Date", **RO),
			F("received_on", "Date", "Member Confirmed On", **RO),
			SB("transfer_section", "Transfer"),
			F("transfer_reference", "Data", **RO),
			*PAYOUT_SNAPSHOT,
			CB("cb2"),
			F("receipt_disputed", "Check", "Member Says Not Received", **RO),
			F("dispute_note", "Small Text", **RO),
			F("reviewed_by", "Link", options="User", **RO),
			F("review_note", "Small Text"),
		],
	),
	dict(
		name="Coop Announcement", autoname="hash", title_field="title",
		fields=[
			F("cooperative", "Link", options="Cooperative", reqd=True, in_list_view=True, in_standard_filter=True),
			F("title", reqd=True, in_list_view=True),
			F("message", "Text", reqd=True),
			F("pinned", "Check", default="0"),
			F("is_system", "Check", "Posted Automatically", **RO),
			F("posted_by", "Link", options="User", **RO),
		],
	),
	dict(
		name="Coop Payment Transaction", autoname="field:reference", naming_rule="By fieldname",
		title_field="reference",
		fields=[
			F("reference", reqd=True, unique=True, in_list_view=True),
			F("purpose", "Select", options="Contribution\nLoan Repayment", reqd=True, in_list_view=True),
			F("status", "Select", options="Initiated\nPending\nSuccess\nFailed\nAbandoned\nRefunded\nDisputed\nUnmatched",
				default="Initiated", in_list_view=True, in_standard_filter=True),
			F("source", "Select", options="Checkout\nAutosave\nDedicated Account", default="Checkout", in_standard_filter=True),
			F("settles_to", "Select", options="Cooperative Bank\nPlatform Paystack Balance", **RO,
				description="Cooperative Bank when the group's subaccount receives it; otherwise it waits in the platform's Paystack balance"),
			F("allocated_to", "Link", "Allocated To Member", options="Coop Member", **RO,
				description="For an unmatched transfer, the member it was added to"),
			F("amount", "Currency", "Amount (credited)", options="currency", in_list_view=True),
			F("fee", "Currency", "Fee Added for Member", options="currency"),
			F("charged_amount", "Currency", "Amount Charged", options="currency"),
			F("paystack_fees", "Currency", "Paystack Fees", options="currency", **RO),
			F("settled_amount", "Currency", "Settled to Cooperative", options="currency", **RO,
				description="What actually reaches the cooperative after Paystack's fee and any platform commission"),
			F("refunded_amount", "Currency", options="currency", **RO),
			F("refund_ids", "Small Text", "Refunds Applied", **RO, description="Paystack refund ids already counted"),
			F("currency", default="NGN"),
			CB("cb1"),
			F("cooperative", "Link", options="Cooperative", in_standard_filter=True),
			F("member", "Link", options="Coop Member"),
			F("user", "Link", options="User"),
			F("loan", "Link", options="Coop Loan"),
			F("reference_doctype", "Link", "Created Document Type", options="DocType", **RO),
			F("reference_name", "Dynamic Link", "Created Document", options="reference_doctype", **RO),
			SB("gateway_section", "Gateway", collapsible=True),
			F("channel", **RO),
			F("paid_at", "Datetime", **RO),
			F("save_method", "Check", "Save Payment Method"),
			F("setup_autosave", "Check", "Turn On Autosave After Payment"),
			F("authorization", "Link", "Saved Payment Method", options="Coop Payment Authorization", **RO),
			F("message", "Small Text", **RO),
			F("access_code", **RO),
			F("authorization_url", "Small Text", **RO),
			F("gateway_response", "Code", options="JSON", **RO),
		],
	),
	dict(
		name="Coop Payment Authorization", autoname="hash", title_field="label",
		fields=[
			F("user", "Link", options="User", reqd=True, in_list_view=True, in_standard_filter=True),
			F("label", in_list_view=True),
			F("channel", "Select", options="card\ndirect_debit\nbank", in_list_view=True),
			F("status", "Select", options="Pending\nActive\nInactive", default="Pending", in_list_view=True,
				in_standard_filter=True),
			F("reference", "Data", "Setup Reference", **RO),
			CB("cb1"),
			F("authorization_code", "Password", **RO),
			F("signature", **RO),
			F("customer_code", **RO),
			F("email", **RO),
			SB("details_section", "Details"),
			F("brand", **RO),
			F("bank", **RO),
			F("last4", **RO),
			CB("cb2"),
			F("exp_month", "Int", **RO),
			F("exp_year", "Int", **RO),
			F("account_name", **RO),
		],
	),
	dict(
		name="Coop Session", autoname="hash", title_field="user", perms=["System Manager"],
		fields=[
			F("user", "Link", options="User", reqd=True, in_list_view=True, in_standard_filter=True, search_index=True, **RO),
			F("token_hash", "Data", unique=True, **RO),
			F("device", "Data", in_list_view=True, **RO),
			F("ip_address", "Data", **RO),
			CB("cb1"),
			F("last_seen", "Datetime", in_list_view=True, **RO),
			F("expires_on", "Datetime", **RO),
			F("revoked", "Check", in_list_view=True, in_standard_filter=True),
		],
	),
	dict(
		name="Coop Password Reset", autoname="hash", title_field="user", perms=["System Manager"],
		fields=[
			F("user", "Link", options="User", reqd=True, in_list_view=True, **RO),
			F("cooperative", "Link", options="Cooperative", **RO),
			F("code_hash", "Data", **RO),
			CB("cb1"),
			F("created_by", "Link", options="User", in_list_view=True, **RO),
			F("expires_on", "Datetime", **RO),
			F("used_on", "Datetime", in_list_view=True, **RO),
			F("attempts", "Int", **RO),
		],
	),
	dict(
		name="Coop Notification", autoname="hash", title_field="title",
		fields=[
			F("user", "Link", options="User", reqd=True, in_list_view=True, in_standard_filter=True, search_index=True),
			F("cooperative", "Link", options="Cooperative", in_standard_filter=True),
			F("kind", "Data", in_standard_filter=True),
			CB("cb1"),
			F("title", reqd=True, in_list_view=True),
			F("body", "Small Text"),
			F("link", "Data", "App Link"),
			F("read", "Check", in_list_view=True, in_standard_filter=True),
		],
	),
	dict(
		name="Coop Push Subscription", autoname="hash", title_field="user", perms=["System Manager"],
		fields=[
			F("user", "Link", options="User", reqd=True, in_list_view=True, search_index=True, **RO),
			F("endpoint", "Small Text", reqd=True, **RO),
			F("endpoint_hash", "Data", unique=True, **RO),
			CB("cb1"),
			F("p256dh", "Data", **RO),
			F("auth", "Data", **RO),
			F("user_agent", "Data", **RO),
			F("session", "Link", "Login Session", options="Coop Session", search_index=True, **RO,
				description="Signing out of this login removes the device from push notifications"),
			F("discreet", "Check", "Hide Amounts on Lock Screen"),
		],
	),
	dict(
		name="Coop Account", autoname="hash", title_field="account_name", search_fields="account_code,account_name,cooperative",
		fields=[
			F("cooperative", "Link", options="Cooperative", reqd=True, in_standard_filter=True, search_index=True),
			F("account_code", "Data", "Code", reqd=True, in_list_view=True),
			F("account_name", "Data", "Account", reqd=True, in_list_view=True, bold=True),
			CB("cb1"),
			F("root_type", "Select", "Type", options="Asset\nLiability\nEquity\nIncome\nExpense", reqd=True, in_list_view=True,
				in_standard_filter=True),
			F("account_type", "Select", "Purpose", options="\nBank\nCash\nPaystack Clearing\nLoan Receivable\nOther Asset\nMember Savings\nUnallocated Receipts\nOther Liability\nReserve Fund\nRetained Surplus\nOpening Balance Equity\nInterest Income\nPenalty Income\nFee Income\nOther Income\nPayment Charges\nBank Charges\nOperating Expense", in_standard_filter=True,
				description="System accounts the app posts to automatically"),
			F("is_system", "Check", "System Account", **RO,
				description="Created with the cooperative; can be renamed but not removed"),
			F("disabled", "Check"),
			F("description", "Small Text"),
		],
	),
	dict(
		name="Coop Journal Entry", autoname="JV-.YYYY.-.#####", naming_rule='By "Naming Series" field',
		title_field="remarks", search_fields="voucher_type,reference_name,remarks",
		fields=[
			F("naming_series", "Select", options="JV-.YYYY.-.#####", default="JV-.YYYY.-.#####", hidden=True),
			F("cooperative", "Link", options="Cooperative", reqd=True, in_standard_filter=True, search_index=True, **RO),
			F("posting_date", "Date", reqd=True, in_list_view=True, **RO),
			F("voucher_type", "Select", options="Contribution\nLoan Disbursement\nLoan Repayment\nWithdrawal\nPayment Charges\nRefund\nUnallocated Receipt\nAllocation\nShare-out\nManual Journal\nReversal", reqd=True, in_list_view=True, in_standard_filter=True, **RO),
			F("status", "Select", options="Draft\nPosted\nReversed\nRejected", default="Posted", in_list_view=True,
				in_standard_filter=True, **RO),
			CB("cb1"),
			F("reference_doctype", "Link", "Source Type", options="DocType", **RO),
			F("reference_name", "Dynamic Link", "Source", options="reference_doctype", search_index=True, **RO),
			F("member", "Link", options="Coop Member", **RO),
			F("total_debit", "Currency", **RO),
			F("total_credit", "Currency", **RO),
			SB("lines_section", "Lines"),
			F("lines", "Table", options="Coop Journal Line", reqd=True, **RO),
			F("remarks", "Small Text", **RO),
			SB("audit_section", "Audit"),
			F("prepared_by", "Link", options="User", **RO),
			F("approved_by", "Link", options="User", **RO),
			F("approved_on", "Datetime", **RO),
			CB("cb2"),
			F("reverses", "Link", "Reverses", options="Coop Journal Entry", **RO),
			F("reversed_by", "Link", "Reversed By", options="Coop Journal Entry", **RO),
			F("reversal_reason", "Small Text", **RO),
		],
	),
	dict(
		name="Coop Journal Line", istable=True,
		fields=[
			F("account", "Link", options="Coop Account", reqd=True, in_list_view=True, search_index=True),
			F("debit", "Currency", in_list_view=True),
			F("credit", "Currency", in_list_view=True),
			F("member", "Link", options="Coop Member", in_list_view=True, search_index=True),
			F("loan", "Link", options="Coop Loan", search_index=True),
			F("remarks", "Data"),
			F("account_type", "Data", **RO, search_index=True),
			F("root_type", "Data", **RO),
			F("cooperative", "Link", options="Cooperative", **RO, search_index=True),
			F("posting_date", "Date", **RO, search_index=True),
			F("is_posted", "Check", **RO,
				description="Drafts waiting for approval don't count in any balance"),
		],
	),
	dict(
		name="Coop Share Out", autoname="SO-.YYYY.-.###", naming_rule='By "Naming Series" field',
		title_field="cooperative",
		fields=[
			F("naming_series", "Select", options="SO-.YYYY.-.###", default="SO-.YYYY.-.###", hidden=True),
			F("cooperative", "Link", options="Cooperative", reqd=True, in_standard_filter=True, **RO),
			F("status", "Select", options="Draft\nPosted\nCancelled", default="Draft", in_list_view=True, **RO),
			F("period_start", "Date", reqd=True, in_list_view=True, **RO),
			F("period_end", "Date", reqd=True, in_list_view=True, **RO),
			F("basis", "Select", "Shared In Proportion To",
				options="Average savings\nSavings at year end\nInterest paid", default="Average savings", **RO),
			CB("cb1"),
			F("income", "Currency", **RO),
			F("expenses", "Currency", **RO),
			F("surplus", "Currency", bold=True, **RO),
			F("reserve_percent", "Percent", **RO),
			F("reserve_amount", "Currency", **RO),
			F("member_percent", "Percent", "Share of the Rest Given to Members", **RO),
			F("to_members", "Currency", "Shared With Members", bold=True, **RO),
			F("retained", "Currency", "Kept in Retained Surplus", **RO),
			SB("lines_section", "Members"),
			F("lines", "Table", options="Coop Share Out Line", **RO),
			SB("audit_section", "Audit"),
			F("prepared_by", "Link", options="User", **RO),
			F("approved_by", "Link", options="User", **RO),
			F("posted_on", "Datetime", **RO),
			CB("cb2"),
			F("journal_entry", "Link", options="Coop Journal Entry", **RO),
			F("note", "Small Text", **RO),
		],
	),
	dict(
		name="Coop Share Out Line", istable=True,
		fields=[
			F("member", "Link", options="Coop Member", reqd=True, in_list_view=True),
			F("member_name", "Data", in_list_view=True),
			F("basis_value", "Currency", in_list_view=True),
			F("share_amount", "Currency", in_list_view=True),
		],
	),
	dict(
		name="Coop Approval Rule", autoname="hash", title_field="action",
		fields=[
			F("cooperative", "Link", options="Cooperative", reqd=True, in_standard_filter=True, search_index=True),
			F("action", "Select", options="Member admission\nLoan\nWithdrawal\nOffline payment\nManual journal\nShare-out", reqd=True, in_list_view=True),
			F("min_amount", "Currency", "Applies From Amount", default="0", in_list_view=True,
				description="The rule with the highest amount at or below the request's amount is used"),
			F("enabled", "Check", default="1"),
			F("stages", "Table", options="Coop Approval Rule Stage", reqd=True),
		],
	),
	dict(
		name="Coop Approval Rule Stage", istable=True,
		fields=[
			F("label", "Data", "Stage", in_list_view=True),
			F("roles", "Small Text", "Who Can Approve", reqd=True, in_list_view=True,
				description="Comma-separated roles, e.g. Loan Officer, Treasurer"),
		],
	),
	dict(
		name="Coop Approval", autoname="APR-.YYYY.-.#####", naming_rule='By "Naming Series" field',
		title_field="summary", search_fields="action,reference_name,summary",
		fields=[
			F("naming_series", "Select", options="APR-.YYYY.-.#####", default="APR-.YYYY.-.#####", hidden=True),
			F("cooperative", "Link", options="Cooperative", reqd=True, in_standard_filter=True, search_index=True, **RO),
			F("action", "Select", options="Member admission\nLoan\nWithdrawal\nOffline payment\nManual journal\nShare-out", reqd=True, in_list_view=True, in_standard_filter=True, **RO),
			F("status", "Select", options="Pending\nApproved\nRejected\nCancelled", default="Pending", in_list_view=True,
				in_standard_filter=True, **RO),
			F("summary", "Data", in_list_view=True, **RO),
			F("amount", "Currency", **RO),
			CB("cb1"),
			F("reference_doctype", "Link", options="DocType", **RO),
			F("reference_name", "Dynamic Link", options="reference_doctype", search_index=True, **RO),
			F("requested_by", "Link", options="User", **RO),
			F("beneficiary", "Link", options="Coop Member", **RO,
				description="Who benefits: they can't approve it"),
			F("current_stage", "Int", default="1", **RO),
			F("completed_on", "Datetime", **RO),
			SB("steps_section", "Stages"),
			F("steps", "Table", options="Coop Approval Step", **RO),
		],
	),
	dict(
		name="Coop Approval Step", istable=True,
		fields=[
			F("stage_no", "Int", in_list_view=True),
			F("label", "Data", in_list_view=True),
			F("roles", "Small Text", in_list_view=True),
			F("status", "Select", options="Waiting\nApproved\nRejected\nSkipped", default="Waiting", in_list_view=True),
			F("decided_by", "Link", options="User", in_list_view=True),
			F("decided_on", "Datetime"),
			F("note", "Small Text"),
			F("fallback", "Check", "Admin Covering",
				description="Nobody else holds the roles for this stage, so an Admin approves it"),
		],
	),
	dict(
		name="CoopVault Settings", issingle=True,
		fields=[
			F("frontend_url", label="Frontend (PWA) URL", default="https://pay-frappe.web.app",
				description="Used for Paystack redirects. Also add it to allow_cors in site_config.json"),
			F("allow_signup", "Check", "Allow Self Sign-up", default="1"),
			F("allow_create_cooperative", "Check", "Allow Members to Create Cooperatives", default="1"),
			SB("security_section", "Security"),
			F("session_idle_days", "Int", "Log Out After Days of Inactivity", default="7"),
			F("session_max_days", "Int", "Always Log Out After Days", default="30"),
			CB("cb_security"),
			F("confirm_after_days", "Int", "Auto-confirm Loans and Withdrawals After Days", default="7",
				description="If the member neither confirms nor disputes a transfer, it is treated as received"),
			F("security_hold_hours", "Int", "Hold After Password Reset (Hours)", default="72"),
			SB("paystack_section", "Paystack"),
			F("paystack_enabled", "Check", "Enable Paystack", default="0"),
			F("paystack_public_key", label="Public Key", depends_on="paystack_enabled"),
			F("paystack_secret_key", "Password", "Secret Key", depends_on="paystack_enabled"),
			F("webhook_url", "Data", "Webhook URL (set this in your Paystack dashboard)", **RO),
			F("platform_fee_percent", "Percent", "Platform Commission", default="0", depends_on="paystack_enabled",
				description="Your share of each online payment made into a cooperative's subaccount. Usually 0."),
			SB("features_section", "Payment Features", depends_on="paystack_enabled"),
			F("enable_autosave", "Check", "Autosave (recurring card charges)", default="1"),
			F("enable_direct_debit", "Check", "Direct Debit Mandates", default="0",
				description="Requires Direct Debit to be activated on your Paystack account"),
			CB("cb_features"),
			F("enable_dedicated_accounts", "Check", "Dedicated Virtual Accounts", default="0",
				description="Each member gets a personal account number; transfers into it are recorded automatically. Requires Paystack approval."),
			F("dedicated_account_bank", label="Dedicated Account Provider", default="wema-bank",
				depends_on="enable_dedicated_accounts", description="Test keys always use test-bank"),
			SB("fees_section", "Paystack Pricing (used when members pay the fee)", collapsible=True,
				depends_on="paystack_enabled"),
			F("fee_percent", "Float", "Percentage Fee", default="1.5"),
			F("fee_flat", "Currency", "Flat Fee", default="100"),
			CB("cb_fees"),
			F("fee_flat_waived_below", "Currency", "Flat Fee Waived Below", default="2500"),
			F("fee_cap", "Currency", "Fee Cap", default="2000"),
			SB("push_section", "Push Notifications", collapsible=True),
			F("vapid_public_key", "Small Text", "VAPID Public Key", **RO),
			F("vapid_private_key", "Password", "VAPID Private Key", **RO),
			F("vapid_subject", "Data", "VAPID Contact", default="mailto:admin@example.com"),
		],
	),
]

PERM = lambda role, **kw: {"role": role, "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1,
	"export": 1, "print": 1, "email": 1, "share": 1, **kw}


def build(spec):
	name = spec["name"]
	d = {
		"actions": [], "allow_rename": 0, "creation": TS, "modified": TS, "modified_by": "Administrator",
		"owner": "Administrator", "doctype": "DocType", "name": name, "module": MODULE, "custom": 0,
		"editable_grid": 1, "engine": "InnoDB", "links": [], "states": [], "sort_field": "creation",
		"sort_order": "DESC", "track_changes": 1, "row_format": "Dynamic",
		"field_order": [f["fieldname"] for f in spec["fields"]],
		"fields": spec["fields"],
		"index_web_pages_for_search": 0,
	}
	if spec.get("istable"):
		d.update(istable=1, permissions=[], track_changes=0)
	elif spec.get("issingle"):
		d["issingle"] = 1
		d["permissions"] = [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "print": 1, "email": 1, "share": 1}]
	else:
		d["permissions"] = [PERM(r) for r in spec.get("perms", ["System Manager", "Coop Manager"])]
		d["autoname"] = spec.get("autoname")
		if spec.get("naming_rule"):
			d["naming_rule"] = spec["naming_rule"]
		if spec.get("title_field"):
			d["title_field"] = spec["title_field"]
			d["show_title_field_in_link"] = 1
		if spec.get("search_fields"):
			d["search_fields"] = spec["search_fields"]
	return d


def scrub(s):
	return s.replace(" ", "_").replace("-", "_").lower()


CONTROLLER = '''# Copyright (c) 2026, Larry Adah and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class {cls}(Document):
	pass
'''

def write(path, doc):
	"""Keep the existing `modified` stamp unless something changed: Frappe only re-syncs a DocType
	whose JSON is newer than the database copy."""
	if os.path.exists(path):
		old = json.load(open(path))
		same = {k: v for k, v in old.items() if k != "modified"} == {k: v for k, v in doc.items() if k != "modified"}
		if same:
			return False
		doc["modified"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
	with open(path, "w") as fh:
		json.dump(doc, fh, indent=1, sort_keys=True)
		fh.write("\n")
	return True


for spec in DOCTYPES:
	folder = os.path.join(APP, "coopvault", "doctype", scrub(spec["name"]))
	os.makedirs(folder, exist_ok=True)
	open(os.path.join(folder, "__init__.py"), "a").close()
	if not write(os.path.join(folder, scrub(spec["name"]) + ".json"), build(spec)):
		continue
	py = os.path.join(folder, scrub(spec["name"]) + ".py")
	if not os.path.exists(py):
		with open(py, "w") as fh:
			fh.write(CONTROLLER.format(cls=spec["name"].replace(" ", "")))
	print("wrote", spec["name"])
