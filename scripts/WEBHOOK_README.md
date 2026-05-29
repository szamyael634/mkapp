Webhook Forwarder

Overview
- The database migration adds `webhook_endpoints` and `webhook_queue` tables and attaches a trigger to `products` to enqueue events on INSERT/UPDATE/DELETE.
- A small forwarder (`webhook_forwarder.js`) subscribes to new rows in `webhook_queue` using Supabase Realtime and forwards them via HTTP POST to registered endpoint URLs.

Setup & Run
1. Install dependencies:

```bash
cd scripts
npm install
```

2. Set environment variables (recommended to use a service role key so the forwarder can update `webhook_queue` status):

- `SUPABASE_URL` — your Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` — your Supabase service_role key

3. Run the forwarder:

```bash
npm start
```

Notes
- The forwarder listens for INSERTs into `webhook_queue`. When an endpoint is registered in `webhook_endpoints`, events will be enqueued and then forwarded.
- To register a webhook endpoint, insert a row into `webhook_endpoints` with the target `url` and optional `secret`.

Example SQL to add an endpoint:

```sql
INSERT INTO webhook_endpoints (name, url, secret, events) VALUES ('Order Webhook','https://example.com/webhook','my-secret', ARRAY['INSERT']::TEXT[]);
```

Security
- Store `SUPABASE_SERVICE_ROLE_KEY` securely — it has elevated privileges.
- Forwarder adds `X-Webhook-Secret` header if `secret` is provided for the endpoint.

Scaling
- Run multiple forwarder instances for throughput; add more robust retry/backoff logic as needed.
