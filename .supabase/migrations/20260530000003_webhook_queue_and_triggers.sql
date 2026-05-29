-- Migration: Create webhook endpoints, queue and triggers
-- Adds a queue table and trigger function to enqueue webhook events

CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id SERIAL PRIMARY KEY,
  name TEXT,
  url TEXT NOT NULL,
  secret TEXT,
  events TEXT[] NOT NULL DEFAULT ARRAY['INSERT','UPDATE','DELETE']::TEXT[],
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS webhook_queue (
  id SERIAL PRIMARY KEY,
  endpoint_id INTEGER REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
  table_name TEXT NOT NULL,
  operation TEXT NOT NULL,
  payload JSONB,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  sent_at TIMESTAMPTZ
);

-- Function to enqueue webhook events into webhook_queue
CREATE OR REPLACE FUNCTION enqueue_webhook_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  endpoint RECORD;
  event_payload JSONB;
BEGIN
  IF TG_OP = 'DELETE' THEN
    event_payload := row_to_json(OLD)::jsonb;
  ELSE
    event_payload := row_to_json(NEW)::jsonb;
  END IF;

  FOR endpoint IN SELECT * FROM webhook_endpoints LOOP
    -- If endpoint.events contains the current operation (INSERT/UPDATE/DELETE)
    IF endpoint.events IS NULL OR TG_OP = ANY(endpoint.events) THEN
      INSERT INTO webhook_queue (endpoint_id, table_name, operation, payload)
      VALUES (endpoint.id, TG_TABLE_NAME, TG_OP, event_payload);
    END IF;
  END LOOP;

  RETURN NULL; -- AFTER trigger
END;
$$;

-- Example: attach triggers to products table
DROP TRIGGER IF EXISTS products_enqueue_webhook ON products;
CREATE TRIGGER products_enqueue_webhook
AFTER INSERT OR UPDATE OR DELETE ON products
FOR EACH ROW EXECUTE FUNCTION enqueue_webhook_event();

-- You can add additional triggers for other tables similarly:
-- CREATE TRIGGER orders_enqueue_webhook AFTER INSERT OR UPDATE OR DELETE ON orders FOR EACH ROW EXECUTE FUNCTION enqueue_webhook_event();
