import { chromium } from 'playwright'
import fs from 'node:fs'

const baseURL = process.env.FOLIATE_POC_E2E_BASE_URL
const password = process.env.FOLIATE_POC_E2E_PASSWORD
const files = JSON.parse(process.env.FOLIATE_POC_E2E_FILES || '[]')
if (!baseURL || !password || files.length === 0) {
    throw new Error('FOLIATE_POC_E2E_BASE_URL, password, and files are required')
}
for (const file of files) {
    if (!fs.statSync(file).isFile()) throw new Error(`missing EPUB: ${file}`)
}

const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
})
const page = await context.newPage()
const productionWrites = []
page.on('request', request => {
    const url = request.url()
    if (request.method() !== 'GET' && /\/(?:annotations|thoughts|progress|completion)/.test(url)) {
        productionWrites.push({ method: request.method(), url })
    }
})

await page.goto(`${baseURL}/login`)
await page.locator('input[name="password"]').fill(password)
await Promise.all([
    page.waitForURL(/\/library$/),
    page.locator('button[type="submit"]').click(),
])

const results = []
for (const file of files) {
    await page.goto(`${baseURL}/reader-engine-poc`)
    await page.locator('#poc-file-input').setInputFiles(file)
    await page.locator('#poc-status[data-state="success"]').waitFor({ timeout: 30000 })

    await page.locator('#poc-next').click()
    await page.locator('#poc-prev').click()
    await page.locator('#poc-flow').selectOption('scrolled')
    await page.locator('#poc-next').click()
    await page.locator('#poc-flow').selectOption('paginated')
    await page.locator('#poc-font-up').click()
    await page.setViewportSize({ width: 1024, height: 768 })
    await page.waitForTimeout(500)

    const selection = await page.evaluate(() => window.__FOLIATE_POC__.selectFirstText())
    await page.evaluate(() => window.__FOLIATE_POC__.addSelectedHighlight())
    await page.evaluate(() => window.__FOLIATE_POC__.restoreSelected())
    const preliminary = await page.evaluate(() => window.__FOLIATE_POC__.result())
    for (let index = 0; index < Math.min(10, preliminary.spine_items); index += 1) {
        await page.evaluate(index_ => window.__FOLIATE_POC__.goToSection(index_), index)
    }
    const result = await page.evaluate(() => window.__FOLIATE_POC__.result())
    result.selection_debug = selection
    result.security_flags = await page.evaluate(() => ({
        inlineScript: Boolean(window.__EPUB_SCRIPT_EXECUTED),
        externalScript: Boolean(window.__EPUB_EXTERNAL_SCRIPT_EXECUTED),
        inlineHandler: Boolean(window.__EPUB_HANDLER_EXECUTED),
        javascriptLink: Boolean(window.__EPUB_LINK_EXECUTED),
        iframeScript: Boolean(window.__EPUB_IFRAME_EXECUTED),
    }))
    if (Object.values(result.security_flags).some(Boolean)) {
        throw new Error(`EPUB active content executed: ${JSON.stringify(result.security_flags)}`)
    }
    results.push(result)
    await page.evaluate(() => window.__FOLIATE_POC__.close())
    await page.setViewportSize({ width: 1440, height: 900 })
}

if (productionWrites.length) {
    throw new Error(`POC wrote production state: ${JSON.stringify(productionWrites)}`)
}
process.stdout.write(JSON.stringify({ results, productionWrites }, null, 2))
await browser.close()
