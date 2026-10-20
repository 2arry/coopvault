# CoopVault

A Frappe app for cooperative savings groups: members contribute to a shared pool, see their savings,
borrow from the pool and track what they need to pay back. It replaces the "who paid what" WhatsApp threads.

It is the backend for the CoopVault PWA (separate repo) but is fully usable from Frappe Desk too.

## Features

- **Cooperatives** with join codes, rules (expected contribution, loan interest, max tenure) and a bank account for transfers
- **Members** request to join with a code; admins approve, suspend, and assign roles (Member / Treasurer / Admin)
- **Contributions** by bank transfer or cash (with proof upload, confirmed by an admin) or online via Paystack (auto-confirmed)
- **Loans** that can never exceed what a member has contributed:
  `borrow limit = confirmed contributions × multiplier (0–1) − principal still owed or requested`.
  Admins can only disburse when the pool has the cash.
- **Repayments**, the same way as contributions; loans close automatically when fully repaid
- **Pool summary**: total saved, disbursed, outstanding, and cash at hand
- **Member statements** and **announcements**
- Totals are always recomputed from source documents, so balances cannot drift

## Installation

On any Frappe v15 bench:

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
2. **CoopVault Settings** (`/app/coopvault-settings`): set the frontend URL and sign-up options.
   To accept online payments, enable Paystack and enter your public and secret keys.
   Copy the **Webhook URL** shown there into your Paystack dashboard
   (`/api/method/coopvault.api.payments.paystack_webhook`).
3. Optional: hide tracebacks from API clients in System Settings (`allow_error_traceback = 0`).

## API

All endpoints are whitelisted methods under `/api/method/coopvault.api.*`. Authentication uses Frappe token auth
(`Authorization: token <api_key>:<api_secret>`). `auth.login` and `auth.signup` return the token.

| Module | Endpoints |
| --- | --- |
| `auth` | `login`, `signup`, `me`, `update_profile`, `change_password`, `forgot_password`, `logout_all_devices` |
| `coop` | `my_cooperatives`, `create_cooperative`, `join_cooperative`, `get_dashboard`, `update_cooperative`, `regenerate_join_code`, `list_members`, `update_member`, `leave_cooperative`, `list_announcements`, `post_announcement`, `delete_announcement`, `get_statement`, `pool_report` |
| `finance` | `list_contributions`, `record_contribution`, `review_contribution`, `loan_eligibility`, `apply_loan`, `list_loans`, `get_loan`, `review_loan`, `cancel_loan`, `list_repayments`, `record_repayment`, `review_repayment`, `pending_reviews`, `download_proof` |
| `payments` | `config`, `initialize`, `verify`, `paystack_webhook` |

Every endpoint checks cooperative membership and role itself. Members never need Desk access.
Users with the **System Manager** or **Coop Manager** role can manage any cooperative.

## DocTypes

`Cooperative`, `Coop Member`, `Coop Contribution`, `Coop Loan`, `Coop Loan Repayment`, `Coop Announcement`,
`Coop Payment Transaction` (Paystack log) and `CoopVault Settings` (single).

## License

MIT
