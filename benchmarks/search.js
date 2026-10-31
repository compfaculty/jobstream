import http from 'k6/http';
import { check, sleep } from 'k6';

// Local or explicitly authorized staging only. Set a test user's session cookie.
export const options = {
  stages: [{ duration: '30s', target: 10 }, { duration: '60s', target: 30 }, { duration: '30s', target: 0 }],
  thresholds: { http_req_failed: ['rate<0.01'], checks: ['rate>0.99'] },
};
export default function () {
  if (!__ENV.BASE_URL || !__ENV.SESSION_ID) throw new Error('BASE_URL and SESSION_ID required');
  const response = http.get(`${__ENV.BASE_URL}/api/v1/jobs/?q=Python&location=Tel%20Aviv`, {
    cookies: { sessionid: __ENV.SESSION_ID }, redirects: 0,
    headers: __ENV.HOST_HEADER ? { Host: __ENV.HOST_HEADER } : {},
    tags: { name: 'job-search' },
  });
  check(response, { 'authenticated search succeeds': r => r.status === 200 });
  sleep(1);
}
