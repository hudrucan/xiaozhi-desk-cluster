/* Keep the native AdGuard loading state until an accepted policy job completes. */
(() => {
  if (window.__deskPolicyWaitInstalled) return;
  window.__deskPolicyWaitInstalled = true;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, options) => {
    const response = await nativeFetch(input, options);
    const operation = response.headers.get('X-Desk-DNS-Operation');
    if (response.status !== 202 || !operation ||
        response.headers.get('X-Desk-DNS-State') !== 'pending') return response;
    const statusURL = new URL('control/desk_policy', document.baseURI);
    const requestURL = new URL(typeof input?.url === 'string' ? input.url : String(input), document.baseURI);
    if (requestURL.origin !== statusURL.origin) return response;
    const signal = options?.signal || input?.signal;
    const deadline = Date.now() + 15 * 60 * 1000;
    while (Date.now() < deadline) {
      if (signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
      await new Promise(resolve => setTimeout(resolve, 1000));
      const progress = await nativeFetch(statusURL, {cache: 'no-store', signal});
      if (!progress.ok) throw new Error('DNS policy status is temporarily unavailable; reload to check.');
      const state = await progress.json();
      if (state.operation_id !== operation) throw new Error('DNS policy operation changed; reload to check its result.');
      if (state.operation === 'running') continue;
      const headers = new Headers(response.headers);
      headers.set('Content-Type', 'application/json');
      headers.delete('Content-Length');
      headers.set('X-Desk-DNS-Coverage', state.ready.length + '/3');
      headers.set('X-Desk-DNS-Revision', String(state.revision));
      const failed = state.operation === 'failed';
      const status = failed ? state.operation_status || 503 : state.operation_status || 200;
      const value = failed ? {error: state.error || 'policy_operation_failed'} : state.operation_result;
      return new Response(JSON.stringify(value), {status, headers});
    }
    throw new Error('DNS policy is still running; reload to check progress.');
  };
})();
