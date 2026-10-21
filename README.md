# CoopVault

A Frappe app for cooperative savings groups (ajo, esusu, thrift and credit societies). Members save into a
shared pool, see exactly where they stand, borrow against their savings and pay back in instalments. It replaces
the "who paid what" WhatsApp threads.

It is the backend for the CoopVault PWA (separate repo) and is also fully usable from Frappe Desk, where it ships
a **CoopVault workspace** with live number cards.

## Features

- **Cooperatives** with invite codes and links, rules (contribution amount and frequency, loan interest, longest
  term, borrow multiplier) and a bank account verified with Paystack
- **Contribution standing** for every member: *up to date / due this period / behind*, "paid up to November",
  plus an admin tracker of who still owes this period with WhatsApp reminder text
- **Loans** that can never exceed what a member has saved:
  `borrow limit = confirmed savings × multiplier (0–1) − balance still owed − pending requests`.
  Loans are only approved when the pool has the cash, and repay in equal monthly instalments
  (next amount due and overdue status are computed automatically)
- **Payments**
  - online through Paystack (auto-confirmed), or bank transfer / cash with a receipt upload for the treasurer to confirm
  - overpaid loan repayments are split into savings rather than lost
- **Receipts** for every payment, **statements**, **announcements**, verified **payout accounts** for loans
- Totals are always recomputed from source documents, so balances cannot drift

### Paystack integration (self-contained, no other payments app needed)

| Feature | How |
| --- | --- |
| In-app checkout | `transaction/initialize` returns an access code for InlineJS v2 (`resumeTransaction`); redirect fallback to `/pay/callback` |
| Money settles to each cooperative | Each cooperative gets a **subaccount** created from its verified bank account (`/subaccount`); every charge is split to it with `bearer: subaccount` |
| Fees | Cooperatives choose whether members pay the Paystack fee (added at checkout with Paystack's pass-on formula) or the cooperative absorbs it |
| Bank lookups | `/bank` list (cached daily) and `/bank/resolve` to confirm account names |
| Autosave | Saved card authorisations are charged on each contribution due date (`/transaction/charge_authorization`, daily cron 09:00) |
| Direct debit | Optional bank-account mandates (`/customer/authorization/initialize`, `direct_debit.authorization.active` webhook) |
| Dedicated accounts | Optional personal account number per member (`/dedicated_account/assign`); transfers into it become confirmed contributions |
| Webhook | HMAC-SHA512 signature check, every `charge.success` re-verified with Paystack, idempotent processing |

## Installation

Requires **Frappe v16** (Python 3.14, Node 24, MariaDB 10.6+, Redis).

```bash
bench get-app https://github.com/2arry/coopvault
bench --site <your-site> install-app coopvault
```

The app has no dependency on any other Frappe app (no ERPNext and no payments app needed).

### Configure

1. **CORS for the PWA.** Allow the frontend origin(s):
   ```bash
   bench --site <site> set-config allow_cors '["https://pay-frappe.web.app"]' --parse
   ```
2. **CoopVault Settings** (`/desk/coopvault-settings`): set the frontend URL and sign-up options.
   To accept online payments, enable Paystack, enter your public and secret keys, and paste the
   **Webhook URL** shown there into the Paystack dashboard (Settings → API Keys & Webhooks).
   Turn on Direct Debit or Dedicated Accounts only once Paystack has enabled them for your business
   (use `test-bank` as the provider with test keys).
3. Each cooperative's admin connects the group's bank account in the app (Manage → Settings). That creates
   the Paystack subaccount so online payments settle straight into it.

## API

All endpoints are whitelisted methods under `/api/method/coopvault.api.*`. Authentication uses Frappe token auth
(`Authorization: token <api_key>:<api_secret>`); `auth.login` and `auth.signup` return the token.

| Module | Endpoints |
| --- | --- |
| `auth` | `login`, `signup`, `me`, `update_profile`, `set_payout_account`, `change_password`, `forgot_password`, `logout_all_devices` |
| `coop` | `preview_invite`, `my_cooperatives`, `create_cooperative`, `join_cooperative`, `get_dashboard`, `update_cooperative`, `set_bank_account`, `regenerate_join_code`, `contribution_tracker`, `list_members`, `update_member`, `leave_cooperative`, `list_announcements`, `post_announcement`, `delete_announcement`, `get_statement`, `pool_report` |
| `finance` | `list_contributions`, `record_contribution`, `review_contribution`, `loan_eligibility`, `apply_loan`, `list_loans`, `get_loan`, `review_loan`, `cancel_loan`, `list_repayments`, `record_repayment`, `review_repayment`, `pending_reviews`, `get_receipt`, `download_proof` |
| `payments` | `config`, `quote`, `initialize`, `verify`, `mark_abandoned`, `banks`, `resolve_account`, `list_methods`, `remove_method`, `get_autosave`, `setup_autosave`, `cancel_autosave`, `start_mandate`, `check_mandate`, `request_dedicated_account`, `paystack_webhook` |

Every endpoint checks cooperative membership and role itself, so members never need Desk access.
Users with the **System Manager** or **Coop Manager** role can manage any cooperative.

## DocTypes

`Cooperative`, `Coop Member`, `Coop Contribution`, `Coop Loan`, `Coop Loan Repayment`, `Coop Announcement`,
`Coop Payment Transaction` (Paystack log), `Coop Payment Authorization` (saved cards / mandates) and
`CoopVault Settings` (single).

## Testing against a fake Paystack

Set `coopvault_paystack_api` in a **test** site's `site_config.json` to point the client at a local stand-in for
`https://api.paystack.co`. Never set it on a production site.

## License

MIT
