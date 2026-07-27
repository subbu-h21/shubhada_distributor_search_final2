import axios from 'axios';

// Always relative — the backend serves both the API and this built frontend
// from the same origin (see server.py's catch-all route), so there is no
// separate backend URL to configure. In dev (`yarn start`), CRA's built-in
// proxy (the "proxy" field in package.json) forwards these to the backend
// on :8001 without needing this to be anything other than relative.
export const API_BASE = '/api';

const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
  timeout: 120000,
});

// Attach auth token from localStorage to every request
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('ps.token');
  if (token) {
    config.headers = { ...config.headers, Authorization: `Bearer ${token}` };
  }
  return config;
});

// Auto-logout on 401
api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err?.response?.status === 401) {
      const path = err?.config?.url || '';
      // Don't logout while on the login flow itself
      if (!path.includes('/auth/login')) {
        localStorage.removeItem('ps.token');
        localStorage.removeItem('ps.user');
        // Trigger a soft reload so AuthContext re-evaluates
        if (window.location.pathname !== '/') {
          window.location.href = '/';
        }
      }
    }
    return Promise.reject(err);
  }
);

export const screenshotUrl = (filename) => (filename ? `${API_BASE}/screenshots/${filename}` : null);

export const PortalsAPI = {
  list: () => api.get('/portals').then((r) => r.data),
};

export const DistributorsAPI = {
  list: () => api.get('/targets').then((r) => r.data),
  create: (payload) => api.post('/targets', payload).then((r) => r.data),
  update: (id, payload) => api.patch(`/targets/${id}`, payload).then((r) => r.data),
  remove: (id) => api.delete(`/targets/${id}`).then((r) => r.data),
  bulkSelect: (selected) => api.post('/targets/bulk-select', { selected }).then((r) => r.data),
  testLogin: (id) => api.post(`/targets/${id}/test-login`).then((r) => r.data),
};

export const HistoryAPI = {
  list: () => api.get('/history').then((r) => r.data),
  get: (id) => api.get(`/history/${id}`).then((r) => r.data),
  remove: (id) => api.delete(`/history/${id}`).then((r) => r.data),
};

export const ProductsAPI = {
  count: () => api.get('/products/count').then((r) => r.data),
  search: (q, limit = 20) => api.get('/products/search', { params: { q, limit } }).then((r) => r.data),
  clear: () => api.delete('/products/clear').then((r) => r.data),
  upload: (file) => {
    const form = new FormData();
    form.append('file', file);
    return api.post('/products/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000,
    }).then((r) => r.data);
  },
};

export const ExtractAPI = {
  // Fire an extraction task and poll until it completes. Returns the final
  // history-entry object (same shape the old sync endpoint used to return).
  // This bypasses Cloudflare's ~100s edge timeout by using async task + poll.
  //   onProgress?: (secondsElapsed) => void   — called every poll tick
  run: async (product, quantity, targetIds, { onProgress, pollMs = 2500, timeoutMs = 600000 } = {}) => {
    const start = Date.now();
    const { task_id: taskId } = await api.post('/extract', {
      product,
      quantity: quantity ? Number(quantity) : null,
      target_ids: targetIds,
    }).then((r) => r.data);
    while (true) {
      await new Promise((r) => setTimeout(r, pollMs));
      const s = await api.get(`/extract/status/${taskId}`).then((r) => r.data);
      if (onProgress) onProgress(Math.round((Date.now() - start) / 1000));
      if (s.status === 'done') {
        if (s.error) throw new Error(s.error);
        return s.result;
      }
      if (Date.now() - start > timeoutMs) throw new Error('Extraction timed out');
    }
  },
  manualPick: (historyId, targetId, candidateName) =>
    api.post('/extract/manual-pick', { history_id: historyId, target_id: targetId, candidate_name: candidateName }).then((r) => r.data),
};

export const LiveconnectAPI = {
  status: () => api.get('/liveconnect/session').then((r) => r.data),
  begin: (mobile) => api.post('/liveconnect/session/begin', { mobile }).then((r) => r.data),
  verify: (pendingId, otp) => api.post('/liveconnect/session/verify', { pendingId, otp }).then((r) => r.data),
  clear: () => api.delete('/liveconnect/session').then((r) => r.data),
};

export const RetailioAPI = {
  status: () => api.get('/retailio/session').then((r) => r.data),
  begin: (mobile) => api.post('/retailio/session/begin', { mobile }).then((r) => r.data),
  verify: (pendingId, otp) => api.post('/retailio/session/verify', { pendingId, otp }).then((r) => r.data),
  clear: () => api.delete('/retailio/session').then((r) => r.data),
};

export const MargAPI = {
  status: () => api.get('/marg/session').then((r) => r.data),
  begin: (mobile) => api.post('/marg/session/begin', { mobile }).then((r) => r.data),
  verify: (pendingId, otp) => api.post('/marg/session/verify', { pendingId, otp }).then((r) => r.data),
  clear: () => api.delete('/marg/session').then((r) => r.data),
};

export const OrderAPI = {
  place: (payload) => api.post('/order/place', payload).then((r) => r.data),
  status: (taskId) => api.get(`/order/status/${taskId}`).then((r) => r.data),
  // Convenience helper: submit + poll until 'done' or timeout, then return
  // the final result. onProgress?: (secondsElapsed) => void
  placeAndWait: async (payload, { pollMs = 3000, timeoutMs = 240000, onProgress } = {}) => {
    const start = Date.now();
    const { task_id: taskId } = await OrderAPI.place(payload);
    while (true) {
      await new Promise((r) => setTimeout(r, pollMs));
      const s = await OrderAPI.status(taskId);
      if (onProgress) onProgress(Math.round((Date.now() - start) / 1000));
      if (s.status === 'done') return s;
      if (Date.now() - start > timeoutMs) throw new Error('Order placement timed out');
    }
  },
};

// -----------------------------------------------------------------
// Price-List Vault — upload distributor pricelists, search across them
// -----------------------------------------------------------------
export const PricelistAPI = {
  // Multipart upload; returns { token, headers, preview, mapping_suggested,
  //                             mapping_saved, detected_distributor, rows }
  // onUploadProgress? — called with { loaded, total, percent } during the
  // multipart file transfer (before the server-side parse begins).
  upload: (file, onUploadProgress) => {
    const fd = new FormData();
    fd.append('file', file);
    // NOTE: We intentionally DO NOT set the Content-Type header here.
    // The axios instance has a default 'application/json' — passing
    // `undefined` lets the browser auto-generate the correct
    // 'multipart/form-data; boundary=...'  header.
    return api.post('/pricelist/upload', fd, {
      headers: { 'Content-Type': undefined },
      timeout: 600000,
      onUploadProgress: onUploadProgress ? (e) => {
        const total = e.total || file.size || 1;
        onUploadProgress({ loaded: e.loaded, total, percent: Math.round((e.loaded / total) * 100) });
      } : undefined,
    }).then((r) => r.data);
  },
  confirm: (payload) => api.post('/pricelist/confirm', payload, { timeout: 600000 }).then((r) => r.data),
  search:  (q) => api.get('/pricelist/search', { params: { q } }).then((r) => r.data),
  summary: () => api.get('/pricelist/summary').then((r) => r.data),
  clear:   (distId) => api.delete(`/pricelist/distributor/${distId}`).then((r) => r.data),
};

export default api;
