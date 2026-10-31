let csrf = '';
export function setCsrf(token: string) { csrf = token; }
export class ApiError extends Error {
  constructor(public status: number, public data: { detail?: string; code?: string; missing?: { player_id: number; round_number: number }[] }) { super(typeof data.detail === 'string' ? data.detail : 'The request could not be completed.'); }
}
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch('/api' + path, { method, credentials: 'same-origin', signal: controller.signal, headers: { Accept: 'application/json', ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...(method !== 'GET' && csrf ? { 'X-CSRF-Token': csrf } : {}) }, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
    const data = await response.json().catch(() => ({ detail: 'The server returned an unreadable response. Please try again.' }));
    if (!response.ok) { if (response.status === 401 && csrf) window.dispatchEvent(new Event('difede-session-expired')); throw new ApiError(response.status, data); }
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new Error('Connection interrupted. Your score drafts are kept on this device. Try again when connected.');
  } finally { clearTimeout(timer); }
}
