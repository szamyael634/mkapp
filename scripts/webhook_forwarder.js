// Simple webhook forwarder using Supabase Realtime
// Usage: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or anon for read only)
//        set FORWARDER_SECRET optional
// Run: node scripts/webhook_forwarder.js

import fetch from 'node-fetch';
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;
if (!SUPABASE_URL || !SUPABASE_KEY) {
  console.error('Please set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY');
  process.exit(1);
}

const supabase = createClient(SUPABASE_URL, SUPABASE_KEY, { realtime: { params: { eventsPerSecond: 10 } } });

async function forwardQueueRow(row) {
  try {
    // fetch endpoint details
    const { data: endpoint } = await supabase.from('webhook_endpoints').select('*').eq('id', row.endpoint_id).single();
    if (!endpoint) {
      console.warn('Endpoint not found for id', row.endpoint_id);
      return;
    }

    const payload = row.payload || {};
    const url = endpoint.url;
    const headers = { 'Content-Type': 'application/json' };
    if (endpoint.secret) headers['X-Webhook-Secret'] = endpoint.secret;

    const resp = await fetch(url, { method: 'POST', headers, body: JSON.stringify({ table: row.table_name, operation: row.operation, data: payload }) });
    if (!resp.ok) {
      const text = await resp.text();
      console.error('Forward failed', resp.status, text);
      await supabase.from('webhook_queue').update({ attempts: row.attempts + 1, last_error: text }).eq('id', row.id);
      return;
    }

    await supabase.from('webhook_queue').update({ status: 'sent', sent_at: new Date().toISOString() }).eq('id', row.id);
    console.log('Forwarded webhook for row', row.id, '->', url);
  } catch (e) {
    console.error('Error forwarding row', e.message || e);
    try { await supabase.from('webhook_queue').update({ attempts: row.attempts + 1, last_error: (e.message||String(e)) }).eq('id', row.id); } catch(_){}
  }
}

(async function main(){
  console.log('Starting webhook forwarder...');
  const channel = supabase.channel('public:webhook_queue')
    .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'webhook_queue' }, (payload) => {
      const row = payload.new;
      forwardQueueRow(row);
    });

  await channel.subscribe();
  console.log('Subscribed to webhook_queue INSERTs.');
})();
