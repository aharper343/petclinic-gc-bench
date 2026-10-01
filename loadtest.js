// Open-loop, fixed-arrival-rate load test against spring-petclinic-rest.
// Deliberately NOT wrk/ab-style closed-loop — a closed-loop tool under-reports
// tail latency (coordinated omission), which would hide exactly the GC
// pause-time effect this whole benchmark exists to measure.
import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:9966/petclinic';
const RATE = parseInt(__ENV.RATE || '30', 10); // requests/sec, constant arrival
const DURATION = __ENV.DURATION || '3m';
const PRE_VUS = parseInt(__ENV.PRE_VUS || '50', 10); // enough idle VUs to ride out a ~1s stall at 40 req/s without dropping iterations
const MAX_VUS = parseInt(__ENV.MAX_VUS || '200', 10);
// v2 API is paginated — PAGE_SIZE controls per-request cost, OWNER_COUNT (total
// seeded rows) controls how many distinct pages get exercised. Deliberately
// decoupled: unlike the unpaginated v1 /api/owners endpoint (which returns
// every row every call and made per-request cost scale with total dataset
// size — that's what saturated 2 pinned cores at just 5,000 owners), this lets
// the seed dataset be large (for real resident working-set size / heap
// pressure) while request cost stays flat regardless of dataset size.
const PAGE_SIZE = parseInt(__ENV.PAGE_SIZE || '20', 10);
const OWNER_COUNT = parseInt(__ENV.OWNER_COUNT || '5010', 10);
const TOTAL_PAGES = Math.ceil(OWNER_COUNT / PAGE_SIZE);

// Single scenario per invocation — the driver script calls this script TWICE
// per run: once as a throwaway warmup (output discarded), once for the
// measured window (summary captured). Splitting into two separate k6
// processes, rather than one process with warmup+measured scenarios, avoids
// needing to filter k6's aggregated summary by tag to exclude warmup data.
export const options = {
  scenarios: {
    load: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PRE_VUS,
      maxVUs: MAX_VUS,
      exec: 'mixedRead',
    },
  },
  thresholds: {
    // don't fail the run on latency — we WANT the raw numbers, good or bad
    http_req_failed: ['rate<1.0'],
  },
};

// 50% paginated owner list (random page, fixed size — flat cost regardless of
// total dataset size), 30% owner-by-id, 20% vets list.
export function mixedRead() {
  const r = Math.random();
  let res;
  if (r < 0.5) {
    const page = Math.floor(Math.random() * TOTAL_PAGES);
    res = http.get(`${BASE}/api/v2/owners?page=${page}&size=${PAGE_SIZE}`);
  } else if (r < 0.8) {
    const id = Math.floor(Math.random() * OWNER_COUNT) + 1;
    res = http.get(`${BASE}/api/owners/${id}`);
  } else {
    res = http.get(`${BASE}/api/vets`);
  }
  check(res, { 'status is 200': (r) => r.status === 200 });
}
