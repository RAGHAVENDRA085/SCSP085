window.CampusAPI = {
  async request(url, options = {}) {
    if (window.location.protocol === 'file:') {
      const e = new Error('Please start the Smart Campus Flask server and open http://127.0.0.1:5000. Do not open this HTML file directly.');
      e.status = 0;
      throw e;
    }
    const opts = {credentials:'same-origin', ...options, headers:{...(options.headers||{})}};
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== 'string') {
      opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(url, opts);
    let data = null; try { data = await res.json(); } catch (_) {}
    if (!res.ok) { const detail = data?.error || data?.message || `Request failed (${res.status})`; const e = new Error(`${detail} [${opts.method || 'GET'} ${url}]`); e.status=res.status; e.data=data; throw e; }
    if (data?.user) localStorage.setItem('currentUser', JSON.stringify(data.user));
    return data;
  },
  reportError(error) { console.error(error); alert(error?.message || 'Something went wrong.'); }
};
