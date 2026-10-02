// Uploads files through the real web UI in a headless browser (scenario 9).
//
//   node upload.mjs <mode> <remote folder> <file...>
//
// modes:
//   normal   upload the files and wait until the upload info reports the result
//   tamper   replace the checksum in the tus Upload-Metadata with a wrong one
//   twins    upload the first file, cut it off after the first chunk, then upload the second
//            file, which has the same name, size and mtime, so tus-js-client resumes the
//            first file's upload with the second file's bytes
//
// Prints one JSON line: {"title": "...", "errors": [...]}.
import { chromium } from 'playwright'

const [mode, folder, ...files] = process.argv.slice(2)
const base = process.env.OC_URL
const user = 'admin'
const password = process.env.OC_ADMIN_PASSWORD

const browser = await chromium.launch()
const context = await browser.newContext({ ignoreHTTPSErrors: true })
const page = await context.newPage()

async function login() {
  await page.goto(base)
  await page.locator('#oc-login-username').fill(user)
  await page.locator('#oc-login-password').fill(password)
  await page.keyboard.press('Enter')
  await page.waitForURL(/\/files\//, { timeout: 60000 })
}

async function openFolder() {
  await page.goto(`${base}/files/spaces/personal/${user}/${folder.replace(/^\//, '')}`)
  await page.locator('[id$="files-floating-action-button"]').waitFor({ timeout: 60000 })
}

async function upload(paths) {
  // the upload inputs live in the "new" menu behind the floating action button
  const input = page.locator('#files-file-upload-input')
  if (!(await input.count())) {
    await page.locator('[id$="files-floating-action-button"]').click()
    await input.waitFor({ state: 'attached', timeout: 60000 })
  }
  await input.setInputFiles(paths)
  await page.keyboard.press('Escape')
  // wait until nothing is in progress any more
  const title = page.locator('.upload-info-title p')
  await title.waitFor({ timeout: 60000 })
  await page.waitForFunction(
    () => {
      const t = document.querySelector('.upload-info-title p')?.textContent ?? ''
      return /completed|failed|cancelled/i.test(t)
    },
    null,
    { timeout: 3 * 60 * 60 * 1000, polling: 1000 }
  )
  return (await title.textContent()).trim()
}

async function closeUploadInfo() {
  const close = page.locator('#close-upload-info-btn')
  if (await close.count()) await close.click().catch(() => {})
}

const errors = []
page.on('response', (res) => {
  if (res.status() >= 400 && /\/(data|remote\.php)\//.test(res.url())) {
    errors.push(`${res.request().method()} ${res.status()}`)
  }
})

await login()
await openFolder()

let title
if (mode === 'normal') {
  title = await upload(files)
} else if (mode === 'tamper') {
  await page.route('**/*', async (route) => {
    const req = route.request()
    const meta = req.headers()['upload-metadata']
    if (req.method() === 'POST' && meta) {
      const wrong = Buffer.from('sha1 ' + '0'.repeat(40)).toString('base64')
      const tampered = meta
        .split(',')
        .map((pair) => (pair.startsWith('checksum ') ? `checksum ${wrong}` : pair))
        .join(',')
      return route.continue({ headers: { ...req.headers(), 'upload-metadata': tampered } })
    }
    return route.continue()
  })
  title = await upload(files)
} else if (mode === 'twins') {
  // first upload: let the creation and the first chunk through, then cut the connection
  let patches = 0
  await page.route('**/*', async (route) => {
    if (route.request().method() === 'PATCH' && ++patches > 1) return route.abort('connectionreset')
    return route.continue()
  })
  const first = await upload([files[0]])
  await page.unroute('**/*')
  await closeUploadInfo()
  // second upload: same name, size and mtime, different content
  title = `${first} / ${await upload([files[1]])}`
}

console.log(JSON.stringify({ title, errors }))
await browser.close()
