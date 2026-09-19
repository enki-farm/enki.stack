// LLM inference benchmark: hits an OpenAI-compatible chat/completions endpoint
// through the Envoy AI Gateway and measures per-request/per-token latencies.
//
// Requires a k6 build with the xk6-sse extension (see benchmark/k6/Dockerfile) -
// core k6 cannot time individual chunks of a streamed HTTP response.
import sse from 'k6/x/sse';
import { sleep } from 'k6';
import { SharedArray } from 'k6/data';
import { Counter, Rate, Trend } from 'k6/metrics';

// --- Configuration (env vars) -----------------------------------------------
const BASE_URL = __ENV.BASE_URL || 'https://ai.zer0.garden';
const MODEL_NAME = __ENV.MODEL_NAME || 'default';
const MODEL_CONFIG = __ENV.MODEL_CONFIG || 'default';
const RUN_ID = __ENV.RUN_ID || 'local';
const RESULTS_FILE = __ENV.RESULTS_FILE || `${RUN_ID}.json`;
const MAX_TOKENS = parseInt(__ENV.MAX_TOKENS || '128', 10);
const TEMPERATURE = parseFloat(__ENV.TEMPERATURE || '0.2');
const THINK_TIME_MS = parseInt(__ENV.THINK_TIME_MS || '1000', 10);
const REQUEST_TIMEOUT = __ENV.REQUEST_TIMEOUT || '60s';

// Which scenario(s) to run in this invocation: ramping_vus | constant_arrival_rate | both
const SCENARIO = __ENV.SCENARIO || 'ramping_vus';

const RAMP_MAX_VUS = parseInt(__ENV.RAMP_MAX_VUS || '10', 10);
const RAMP_UP = __ENV.RAMP_UP || '30s';
const RAMP_HOLD = __ENV.RAMP_HOLD || '2m';
const RAMP_DOWN = __ENV.RAMP_DOWN || '30s';

const ARRIVAL_RATE = parseInt(__ENV.ARRIVAL_RATE || '5', 10); // requests/sec
const ARRIVAL_DURATION = __ENV.ARRIVAL_DURATION || '2m';
const ARRIVAL_PRE_VUS = parseInt(__ENV.ARRIVAL_PRE_VUS || '10', 10);
const ARRIVAL_MAX_VUS = parseInt(__ENV.ARRIVAL_MAX_VUS || '50', 10);

// --- Prompt dataset ----------------------------------------------------------
const promptData = new SharedArray('prompts', function () {
  return [JSON.parse(open('./prompts.json'))];
});
const prompts = promptData[0];

function pickPrompt() {
  // Roughly representative of real traffic: mostly short/medium, some long.
  const r = Math.random();
  if (r < 0.5) return { turns: [randomFrom(prompts.short)] };
  if (r < 0.85) return { turns: [randomFrom(prompts.medium)] };
  if (r < 0.95) return { turns: [randomFrom(prompts.long)] };
  return { turns: randomFrom(prompts.multi_turn) };
}

function randomFrom(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

// --- Custom metrics -----------------------------------------------------------
// All samples inherit the model_id/model_config/run_id/scenario tags from
// options.tags + the executor's built-in `scenario` tag - no per-call tagging needed.
// Values are plain numbers in milliseconds; `isTime` is intentionally left unset so
// k6 doesn't append its own unit suffix on top of ours in downstream metric names.
const ttft = new Trend('llm_ttft_milliseconds');
const timePerOutputToken = new Trend('llm_time_per_output_token_milliseconds');
const e2eLatency = new Trend('llm_e2e_latency_milliseconds');
const completionTokens = new Counter('llm_completion_tokens');
const promptTokens = new Counter('llm_prompt_tokens');
const requestErrors = new Rate('llm_request_errors');

// --- Scenario/executor config --------------------------------------------------
const scenarios = {};
if (SCENARIO === 'ramping_vus' || SCENARIO === 'both') {
  scenarios.ramping_vus = {
    executor: 'ramping-vus',
    exec: 'rampingVUs',
    startVUs: 0,
    stages: [
      { duration: RAMP_UP, target: RAMP_MAX_VUS },
      { duration: RAMP_HOLD, target: RAMP_MAX_VUS },
      { duration: RAMP_DOWN, target: 0 },
    ],
    gracefulRampDown: '10s',
  };
}
if (SCENARIO === 'constant_arrival_rate' || SCENARIO === 'both') {
  scenarios.constant_arrival_rate = {
    executor: 'constant-arrival-rate',
    exec: 'constantArrival',
    rate: ARRIVAL_RATE,
    timeUnit: '1s',
    duration: ARRIVAL_DURATION,
    preAllocatedVUs: ARRIVAL_PRE_VUS,
    maxVUs: ARRIVAL_MAX_VUS,
  };
}

export const options = {
  scenarios,
  tags: {
    model_id: MODEL_NAME,
    model_config: MODEL_CONFIG,
    run_id: RUN_ID,
  },
};

export function handleSummary(data) {
  return {
    [RESULTS_FILE]: JSON.stringify({
      metadata: {
        run_id: RUN_ID,
        base_url: BASE_URL,
        model_id: MODEL_NAME,
        model_config: MODEL_CONFIG,
        scenario: SCENARIO,
      },
      summary: data,
    }, null, 2),
  };
}

// --- Request logic -------------------------------------------------------------
function sendChatCompletion(userContent, history) {
  const messages = (history || []).concat([{ role: 'user', content: userContent }]);
  const payload = {
    model: MODEL_NAME,
    messages,
    max_tokens: MAX_TOKENS,
    temperature: TEMPERATURE,
    stream: true,
    stream_options: { include_usage: true },
  };

  const params = {
    method: 'POST',
    body: JSON.stringify(payload),
    headers: { 'Content-Type': 'application/json' },
    timeout: REQUEST_TIMEOUT,
  };

  const startTime = Date.now();
  let firstTokenTime = null;
  let lastTokenTime = null;
  let tokenEvents = 0;
  let usage = null;
  let assistantContent = '';
  let sawError = false;

  sse.open(`${BASE_URL}/v1/chat/completions`, params, function (client) {
    client.on('event', function (event) {
      if (!event.data || event.data === '[DONE]') {
        return;
      }
      let chunk;
      try {
        chunk = JSON.parse(event.data);
      } catch (e) {
        return; // ignore malformed/keep-alive lines
      }

      if (chunk.usage) {
        usage = chunk.usage;
      }

      const delta = chunk.choices && chunk.choices[0] && chunk.choices[0].delta;
      if (delta && delta.content) {
        const now = Date.now();
        if (firstTokenTime === null) {
          firstTokenTime = now;
        }
        lastTokenTime = now;
        tokenEvents += 1;
        assistantContent += delta.content;
      }
    });

    client.on('error', function () {
      sawError = true;
    });
  });

  const endTime = Date.now();

  if (sawError || firstTokenTime === null) {
    requestErrors.add(1);
    return { assistantContent: '', history: messages };
  }

  requestErrors.add(0);
  ttft.add(firstTokenTime - startTime);
  e2eLatency.add(endTime - startTime);
  if (tokenEvents > 1) {
    timePerOutputToken.add((lastTokenTime - firstTokenTime) / (tokenEvents - 1));
  }
  if (usage) {
    if (usage.completion_tokens) completionTokens.add(usage.completion_tokens);
    if (usage.prompt_tokens) promptTokens.add(usage.prompt_tokens);
  } else {
    completionTokens.add(tokenEvents);
  }

  return {
    assistantContent,
    history: messages.concat([{ role: 'assistant', content: assistantContent }]),
  };
}

// --- Scenario entry points -------------------------------------------------------
export function rampingVUs() {
  const conversation = pickPrompt();
  let history = [];
  for (const turn of conversation.turns) {
    const result = sendChatCompletion(turn, history);
    history = result.history;
    sleep(THINK_TIME_MS / 1000);
  }
}

export function constantArrival() {
  // Fixed short prompt so throughput/saturation measurements aren't skewed by
  // variable prompt length - this scenario probes capacity, not realism.
  sendChatCompletion(randomFrom(prompts.short), []);
}
