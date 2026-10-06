// Laya native inference benchmark using core k6 HTTP support.
// Sends a batch of {state, questions} instances to the public KServe custom-predictor route.
import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const TARGET_URL = __ENV.TARGET_URL || 'https://ai.zer0.garden/laya';
const RUN_ID = __ENV.RUN_ID || 'local';
const RESULTS_FILE = __ENV.RESULTS_FILE || `${RUN_ID}.json`;
const MODEL_CONFIG = __ENV.MODEL_CONFIG || 'default';
const REQUEST_TIMEOUT = __ENV.REQUEST_TIMEOUT || '120s';
const VUS = parseInt(__ENV.VUS || '10', 10);
const DURATION = __ENV.DURATION || '1m';

const requestLatency = new Trend('laya_request_latency_milliseconds');
const responseBytes = new Trend('laya_response_bytes');
const requestErrors = new Rate('laya_request_errors');
const successfulRequests = new Counter('laya_successful_requests');

const payload = JSON.stringify({
    instances: [
      {
        state: {
          subject: 'Duplicate charge',
          body: 'I was billed twice for March. Please refund the duplicate charge.',
        },
        questions: {
          department: {
            type: 'choice',
            instructions: 'Which department should handle this request?',
            criteria: {
              billing: 'invoices, payments, refunds',
              technical: 'bugs, outages, system errors',
              other: 'everything else',
            },
          },
          refund_requested: {
            type: 'noul',
            instructions: 'Does the user explicitly request a refund?',
          },
        },
      }
    ],
});

const params = {
  headers: { 'Content-Type': 'application/json' },
  timeout: REQUEST_TIMEOUT,
  tags: { model_config: MODEL_CONFIG, run_id: RUN_ID },
};

export const options = {
  vus: VUS,
  duration: DURATION,
  thresholds: {
    laya_request_errors: ['rate<0.01'],
    checks: ['rate>0.99'],
  },
};

export function handleSummary(data) {
  return {
    [RESULTS_FILE]: JSON.stringify({
      metadata: {
        run_id: RUN_ID,
        target_url: TARGET_URL,
        model_config: MODEL_CONFIG,
        vus: VUS,
        duration: DURATION,
      },
      summary: data,
    }, null, 2),
  };
}

function sendPrediction() {
  const start = Date.now();
  const response = http.post(TARGET_URL, payload, params);
  const latency = Date.now() - start;
  const body = response.json();
  const validResponse = response.status === 200 && body && Array.isArray(body.predictions);

  requestLatency.add(latency);
  responseBytes.add(response.body.length);
  requestErrors.add(validResponse ? 0 : 1);
  if (validResponse) successfulRequests.add(1);

  check(response, {
    'Laya response is HTTP 200': (res) => res.status === 200,
    'Laya response contains answers': () => Boolean(validResponse),
  });
}

export default function () {
  sendPrediction();
}