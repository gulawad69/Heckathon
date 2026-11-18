// Gate 15/16 k6 script (finaldesign.md §12). Thresholds tightened to KPI values per D7: p50<240, p95<450, p99<590.
// Queries are varied (so cache-hit ratio is realistic) and each VU has think time; KPI-35 (>=8,500 RPS) is a floor.
// Run distributed: >= 10 injectors, e.g.  k6 run --execution-segment "0:1/10" --execution-segment-sequence "0,1/10,...,1" load_tests/k6_50k_load.js
// Smoke (local):  k6 run -e SMOKE=1 -e BASE=http://localhost:8000 load_tests/k6_50k_load.js
import http from 'k6/http';
import { check, sleep } from 'k6';

const SMOKE = __ENV.SMOKE === '1';
const LOCAL_VUS = parseInt(__ENV.LOCAL_VUS || '0');  // scaled-down run on a single box; NOT a KPI-31..36 run
const BASE = __ENV.BASE || 'http://localhost:8000';
export const options = LOCAL_VUS ? {
  stages: [{ duration: '20s', target: LOCAL_VUS }, { duration: '60s', target: LOCAL_VUS }, { duration: '10s', target: 0 }],
  thresholds: { 'http_req_duration': ['p(50)<240', 'p(95)<450', 'p(99)<590'], 'http_req_failed': ['rate<0.0005'], 'checks': ['rate>0.999'] },
} : SMOKE ? {
  stages: [{ duration: '10s', target: 5 }, { duration: '20s', target: 5 }, { duration: '5s', target: 0 }],
  thresholds: { 'http_req_failed': ['rate<0.0005'], 'checks': ['rate>0.999'] },
} : {
  stages: [
    { duration: '2m', target: 10000 },
    { duration: '3m', target: 50000 },
    { duration: '5m', target: 50000 },
    { duration: '2m', target: 0 },
  ],
  thresholds: {
    'http_req_duration': ['p(50)<240', 'p(95)<450', 'p(99)<590'],
    'http_req_failed': ['rate<0.0005'],
    'checks': ['rate>0.999'],
  },
};

const QUERIES = [
  'What is the maximum daily dose of paracetamol?', 'Which tablet is listed for hay fever?', 'Can I take ibuprofen with warfarin?',
  'What OTC options are listed for headache in adults?', 'How should OTC tablets be stored?', 'When should I escalate to a pharmacist?',
  'What is oral rehydration salts used for?', 'Does cetirizine make you drowsy?', 'Which medicine is listed for heartburn?',
  'Summarize the pregnancy guidance.', 'What is the usual adult dose of loratadine?', 'Who should not use ibuprofen?',
];

export default function () {
  const vuId = __VU;
  const tenant = `tenant_pharma_${vuId % 2 === 0 ? 'a' : 'b'}`;   // demo tenants; add more vault namespaces for 50 tenants
  const payload = JSON.stringify({
    tenant_id: tenant,
    user_id: `user_${vuId}`,
    query: QUERIES[(vuId + __ITER) % QUERIES.length],
  });
  const params = { headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': tenant } };
  const res = http.post(`${BASE}/api/v1/agent/query`, payload, params);
  check(res, {
    'status is 200': (r) => r.status === 200,
    'latency within 200-600ms SLA': (r) => r.timings.duration <= 600,
    'no cross-tenant leak': (r) => r.json('tenant_id') === tenant,
  });
  sleep(1 + Math.random() * 4);   // ~3 s think time (§6 capacity model)
}
