# Karobar AI — Cashfree Payment Links setup

This integration creates a Cashfree-hosted payment link for an existing Karobar AI customer and reconciles the customer ledger only after a server-side status lookup reports the full link as `PAID`.

## 1. Get sandbox credentials

1. Open the [Cashfree Merchant Dashboard](https://merchant.cashfree.com/merchants/signup).
2. Switch to the sandbox/test environment.
3. Open Developers / API Keys and copy the sandbox App ID and Secret Key.
4. Never put either credential in frontend JavaScript, HTML, Git, or a public screenshot.

## 2. Configure server environment

Set these environment variables in the backend deployment (or local server environment):

```env
CASHFREE_APP_ID=your_sandbox_app_id
CASHFREE_SECRET_KEY=your_sandbox_secret_key
CASHFREE_ENV=sandbox
CASHFREE_API_VERSION=2026-01-01
```

For production only, set `CASHFREE_ENV=production` and use production credentials after merchant onboarding. Do not mix sandbox credentials with production mode.

## 3. Available endpoints

All endpoints below require the normal Karobar AI login session.

### Create a payment link

```http
POST /api/payments/cashfree/create
Content-Type: application/json
```

```json
{
  "customer_id": "cust_0001",
  "amount": 2500,
  "purpose": "Outstanding invoice payment"
}
```

The customer must exist and the amount cannot exceed their current outstanding balance. The response includes `link_url`, `link_id`, and `status`. An `ACTIVE` link is **not** a paid transaction.

### Refresh payment status

```http
GET /api/payments/cashfree/{link_id}
```

The server fetches the payment link details directly from Cashfree. Only a full `PAID` status is reconciled to the linked customer's payment ledger. Pending, failed, expired, or incomplete payments do not change the ledger.

## 4. Voice agent setup

The new `create_payment_link` tool is registered in `backend/agent.py`. Regenerate the console instructions:

```bash
python -m backend.samvaad_config > docs/samvaad-setup.md
```

In the Sarvam/Samvaad agent console, add the new `create_payment_link` tool using the generated instructions, with these agent-filled text arguments: `customer`, `customer_id`, `customer_phone`, `amount`, `purpose`. Commit a new agent version and update `SAMVAAD_AGENT_VERSION` to that version. Preserve all existing tools.

The agent must confirm the customer and exact amount with the shopkeeper before invoking this write tool. A payment link being created does not mean the customer has paid.

## 5. Demo flow

1. Log in and open Customers.
2. Select a customer with an outstanding balance.
3. Enter an amount not greater than the outstanding balance.
4. Select **Create online link**.
5. Open the Cashfree sandbox URL and complete a sandbox payment.
6. Select **Verify payment status**. The server checks Cashfree; after full payment, the customer's ledger should refresh.

## Security notes

- Credentials remain on the backend.
- Customer identity is taken from the authenticated shop's own customer records.
- Amounts are checked against outstanding balance server-side.
- Browser redirects are not accepted as proof of payment.
- Ledger reconciliation is idempotent per payment link.
- This implementation uses Cashfree Payment Links API (`POST /pg/links`, `GET /pg/links/{link_id}`), API version `2026-01-01` by default.
