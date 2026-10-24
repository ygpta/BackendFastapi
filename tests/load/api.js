import { check, sleep } from 'k6';
import http from 'k6/http';

export const options = {
  vus: Number(__ENV.VUS || 5),
  duration: __ENV.DURATION || '30s',
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<1000'],
    checks: ['rate>0.99'],
  },
};

export default function () {
  const base = __ENV.BASE_URL || 'http://127.0.0.1:8001';
  const headers = __ENV.ACCESS_TOKEN
    ? { Authorization: `Bearer ${__ENV.ACCESS_TOKEN}` }
    : (__ENV.API_KEY ? { 'X-API-Key': __ENV.API_KEY } : {});
  check(http.get(`${base}/health/live`), { live: response => response.status === 200 });
  check(http.get(`${base}/api/v1/tickets?limit=10`, { headers }), {
    tickets: response => response.status === 200,
  });
  sleep(0.5);
}