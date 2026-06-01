// Cloudflare Worker — Intervals.icu CORS proxy
// Deploy at: https://dash.cloudflare.com → Workers & Pages → Create Worker
// Paste this code, deploy, then set WORKER_URL in dashboard.html

export default {
  async fetch(request) {
    // Handle CORS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type',
        }
      });
    }

    if (request.method !== 'POST') {
      return new Response('Method not allowed', { status: 405 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response('Invalid JSON', { status: 400 });
    }

    const { athleteId, apiKey, limit = 30 } = body;

    if (!athleteId || !apiKey) {
      return new Response(JSON.stringify({ error: 'Missing athleteId or apiKey' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' }
      });
    }

    const credentials = btoa(`API_KEY:${apiKey}`);
    const url = `https://intervals.icu/api/v1/athlete/${athleteId}/activities?limit=${limit}`;

    const response = await fetch(url, {
      headers: { 'Authorization': `Basic ${credentials}` }
    });

    const data = await response.text();

    return new Response(data, {
      status: response.status,
      headers: {
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
      }
    });
  }
};
