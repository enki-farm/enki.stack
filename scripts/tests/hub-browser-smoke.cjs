const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.HUB_PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const origin = process.env.HUB_BROWSER_URL;
  const hostname = new URL(origin).hostname;
  const { tokens } = JSON.parse(fs.readFileSync(process.env.HUB_BROWSER_KEYS, 'utf8'));
  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: [`--host-resolver-rules=MAP ${hostname} ${process.env.HUB_BROWSER_IP}`, '--no-proxy-server'],
  });
  try {
    const context = await browser.newContext({
      extraHTTPHeaders: { Authorization: `Bearer ${tokens.member}` },
    });
    const page = await context.newPage();
    const namespaceResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/api/v1/namespaces'));
    const registryResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/api/v1/model_registry'));
    await page.goto(origin, { waitUntil: 'domcontentloaded' });
    const namespaces = await namespaceResponse;
    assert.equal(namespaces.status(), 200);
    assert.ok((await namespaces.json()).data.some(namespace => namespace.name === 'kubeflow'));
    const registries = await registryResponse;
    assert.equal(new URL(registries.url()).searchParams.get('namespace'), 'kubeflow');
    assert.equal(registries.status(), 200);
    assert.ok((await registries.json()).data.some(registry => registry.name === 'model-registry-service'));
    await page.waitForLoadState('networkidle');
    assert.equal(await page.getByText('missing required query parameter: namespace', { exact: false }).count(), 0);
    assert.equal(await page.getByText('Model registry load error', { exact: false }).count(), 0);
    assert.equal(await page.getByText('General loading error', { exact: false }).count(), 0);
    assert.equal(await page.getByText('CentralDashboardEventHandler', { exact: false }).count(), 0);
    console.log('[PASS] Chrome SPA selects Hub namespace and discovers registry from the bare origin');
    const catalogResponse = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/api/v1/model_catalog/models'));
    await page.getByRole('link', { name: 'Model Catalog', exact: true }).click();
    const catalogModels = await catalogResponse;
    assert.equal(catalogModels.status(), 200);
    assert.equal(new URL(catalogModels.url()).searchParams.get('namespace'), 'kubeflow');
    const models = (await catalogModels.json()).data.items;
    assert.ok(Array.isArray(models));
    if (models.length > 0) {
      await page.getByTestId('model-catalog-card-name').first().waitFor({ state: 'visible' });
    }
    console.log('[PASS] Chrome Model Catalog loads in the kubeflow namespace');
    await context.close();
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(error.message);
  process.exitCode = 1;
});