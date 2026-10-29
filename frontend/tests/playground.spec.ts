import { expect, test } from '@playwright/test'
import type { Page } from '@playwright/test'

async function setup(page: Page) {
  await page.route('**/api/health', route => route.fulfill({ json: { ok: true } }))
  await page.goto('/')
  await expect(page.getByText('API connected', { exact: true })).toBeVisible()
}

async function configure(page: Page, name: string) {
  const card = page.getByRole('article', { name, exact: true })
  await card.getByRole('button', { name: 'Settings' }).click()
  await card.getByLabel(`${name} API key`, { exact: true }).fill('test-key-not-real')
  if (name === 'Azure Speech') {
    const resource = card.getByLabel('Azure resource name or endpoint', { exact: true })
    if (await resource.isVisible()) await resource.fill('test-resource')
  }
  return card
}

async function upload(page: Page) {
  await page.getByLabel('Upload audio', { exact: true }).setInputFiles({
    name: 'sample.wav', mimeType: 'audio/wav', buffer: Buffer.from('RIFF-example-audio'),
  })
}

async function addProvider(page: Page, name: string) {
  await page.getByRole('button', { name: '+ Add provider', exact: true }).click()
  await page.getByRole('dialog', { name: 'Add a provider' }).getByRole('button', { name: `Add ${name}`, exact: true }).click()
  return page.getByRole('article', { name, exact: true })
}

test('provider picker searches, avoids duplicates, and clears removed keys', async ({ page }) => {
  await setup(page)
  let card = await addProvider(page, 'ElevenLabs')
  await expect(card.getByRole('button', { name: /Included/ })).toHaveAttribute('aria-pressed', 'true')
  await card.getByLabel('ElevenLabs API key', { exact: true }).fill('test-key-not-real')
  await page.getByRole('button', { name: '+ Add provider', exact: true }).click()
  const picker = page.getByRole('dialog', { name: 'Add a provider' })
  await expect(picker.getByRole('button', { name: 'Add ElevenLabs', exact: true })).toHaveCount(0)
  await picker.getByLabel('Search providers', { exact: true }).fill('sarv')
  await expect(picker.getByRole('button', { name: 'Add Sarvam', exact: true })).toBeVisible()
  await expect(picker.getByRole('button', { name: 'Add Gemini', exact: true })).toHaveCount(0)
  await picker.getByLabel('Search providers', { exact: true }).fill('unknown')
  await expect(picker.getByText('No matching providers.', { exact: true })).toBeVisible()
  await picker.getByLabel('Search providers', { exact: true }).fill('')
  await page.keyboard.press('Escape')
  await expect(picker).not.toBeVisible()
  await card.getByRole('button', { name: 'Remove ElevenLabs', exact: true }).click()
  await expect(page.getByRole('article')).toHaveCount(4)
  await page.getByRole('button', { name: 'Include all', exact: true }).click()
  await upload(page)
  await expect(page.getByRole('button', { name: 'Transcribe with 4 providers', exact: true })).toBeVisible()
  card = await addProvider(page, 'ElevenLabs')
  await expect(card.getByLabel('ElevenLabs API key', { exact: true })).toHaveValue('')
  await expect(page.getByRole('article')).toHaveCount(5)
})

test('added providers expose only implemented file and live settings', async ({ page }) => {
  await setup(page)
  const eleven = await addProvider(page, 'ElevenLabs')
  const sarvam = await addProvider(page, 'Sarvam')
  const gemini = await addProvider(page, 'Gemini')
  await expect(eleven.getByLabel('Speaker diarization', { exact: false })).toBeVisible()
  await expect(gemini.getByLabel('Speaker diarization', { exact: false })).toBeVisible()
  await expect(sarvam.getByLabel('Speaker diarization', { exact: false })).toHaveCount(0)
  await expect(sarvam).toContainText('under 30 seconds')
  await sarvam.getByLabel('Sarvam output mode', { exact: true }).selectOption('codemix')
  await page.getByRole('button', { name: 'Live streaming', exact: true }).click()
  await expect(eleven.getByLabel('ElevenLabs model', { exact: true })).toHaveValue('scribe_v2_realtime')
  await expect(eleven.getByLabel('Speaker diarization', { exact: false })).toHaveCount(0)
  await expect(sarvam.getByLabel('Sarvam endpoint silence', { exact: true })).toBeVisible()
  await expect(sarvam.getByLabel('Sarvam output mode', { exact: true })).toHaveValue('codemix')
  await expect(gemini.getByRole('button', { name: 'Unavailable', exact: true })).toBeDisabled()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.getByRole('button', { name: '+ Add provider', exact: true }).click()
  const picker = page.getByRole('dialog', { name: 'Add a provider' })
  await expect(picker.getByText('All available providers have been added.', { exact: true })).toBeVisible()
  expect(await picker.evaluate(dialog => dialog.scrollWidth <= dialog.clientWidth)).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('new provider cards dispatch independently and removal clears results and export', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: 'Clear', exact: true }).click()
  const providers = [['ElevenLabs', 'elevenlabs'], ['Sarvam', 'sarvam'], ['Gemini', 'gemini']]
  for (const [name] of providers) {
    const card = await addProvider(page, name)
    await card.getByLabel(`${name} API key`, { exact: true }).fill('test-key-not-real')
  }
  const requested: string[] = []
  await page.route('**/api/transcribe', async route => {
    const body = route.request().postDataBuffer()!.toString()
    const provider = providers.find(([, id]) => body.includes(`"provider":"${id}"`))!
    requested.push(provider[1])
    await route.fulfill({ json: { text: `${provider[0]} result`, segments: [], warnings: [], elapsed_ms: 100, model: provider[1] } })
  })
  await upload(page)
  await page.getByRole('button', { name: 'Transcribe with 3 providers', exact: true }).click()
  for (const [name] of providers) await expect(page.getByLabel(`${name} transcript`, { exact: true })).toContainText(`${name} result`)
  expect(requested.sort()).toEqual(['elevenlabs', 'gemini', 'sarvam'])
  for (const [name] of providers) await page.getByRole('button', { name: `Remove ${name}`, exact: true }).click()
  await expect(page.getByRole('button', { name: 'Export JSON' })).toBeDisabled()
  const eleven = await addProvider(page, 'ElevenLabs')
  await expect(eleven.getByLabel('ElevenLabs API key', { exact: true })).toHaveValue('')
  await expect(eleven.getByLabel('ElevenLabs transcript', { exact: true })).not.toContainText('ElevenLabs result')
})

for (const outcome of ['error', 'cancel']) {
  test(`preserves ElevenLabs provisional text when finalization ends with ${outcome}`, async ({ page }) => {
    await page.routeWebSocket('**/api/live', socket => {
      socket.onMessage(data => {
        if (typeof data !== 'string') {
          socket.send(JSON.stringify({ type: 'transcript', text: 'Provisional words', final: false }))
        } else if (JSON.parse(data).type === 'stop') {
          if (outcome === 'error') socket.send(JSON.stringify({ type: 'error', message: 'Provider closed before final confirmation.' }))
        } else socket.send(JSON.stringify({ type: 'ready' }))
      })
    })
    await setup(page)
    await page.getByRole('button', { name: 'Clear', exact: true }).click()
    const eleven = await addProvider(page, 'ElevenLabs')
    await eleven.getByLabel('ElevenLabs API key', { exact: true }).fill('test-key-not-real')
    await page.getByRole('button', { name: 'Live streaming', exact: true }).click()
    await page.getByRole('button', { name: 'Start live comparison', exact: true }).click()
    await expect(eleven.getByLabel('ElevenLabs transcript', { exact: true })).toContainText('Provisional words')
    await page.getByRole('button', { name: /^Stop \d/ }).click()
    if (outcome === 'cancel') await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    else await expect(eleven.getByRole('alert')).toContainText('Provider closed before final confirmation.')
    await expect(page.getByRole('button', { name: 'Start live comparison', exact: true })).toBeEnabled()
    await expect(eleven.getByLabel('ElevenLabs transcript', { exact: true })).toContainText('Provisional words')
    await expect(eleven.getByRole('button', { name: 'Copy', exact: true })).toBeDisabled()
  })
}

test('renders cards, mode capabilities and responsive layout', async ({ page }) => {
  await setup(page)
  await expect(page.getByRole('article')).toHaveCount(4)
  await page.getByRole('button', { name: 'Live streaming', exact: true }).click()
  await expect(page.getByRole('article', { name: 'OpenAI', exact: true }).getByRole('button', { name: 'Unavailable' })).toBeDisabled()
  await expect(page.getByLabel('Upload audio', { exact: true })).toBeDisabled()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('validates provider keys before sending audio', async ({ page }) => {
  await setup(page)
  let requests = 0
  await page.route('**/api/transcribe', route => { requests++; return route.fulfill({ json: {} }) })
  await upload(page)
  await page.getByRole('button', { name: 'Transcribe with 3 providers' }).click()
  await expect(page.getByRole('alert')).toContainText('Enter API keys')
  expect(requests).toBe(0)
})

test('Azure file mode accepts the exact regional endpoint from the portal', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: 'Clear', exact: true }).click()
  const azure = page.getByRole('article', { name: 'Azure Speech', exact: true })
  await azure.getByRole('button', { name: '+ Include', exact: true }).click()
  await configure(page, 'Azure Speech')
  await azure.getByLabel('Azure resource name or endpoint', { exact: true }).fill('https://westus.api.cognitive.microsoft.com/')
  await expect(azure).toContainText('Keys and Endpoint')
  let submitted = false
  await page.route('**/api/transcribe', async route => {
    const body = route.request().postDataBuffer()!.toString()
    expect(body).toContain('"resource":"https://westus.api.cognitive.microsoft.com/"')
    submitted = true
    await route.fulfill({ json: { text: 'Regional endpoint transcript.', segments: [], warnings: [], elapsed_ms: 100, model: 'azure-transcribe' } })
  })
  await upload(page)
  await page.getByRole('button', { name: 'Transcribe with 1 provider', exact: true }).click()
  await expect(azure.getByLabel('Azure Speech transcript', { exact: true })).toContainText('Regional endpoint transcript.')
  expect(submitted).toBe(true)
})

test('Azure auto detection is visible, configurable, and works in live mode', async ({ page }) => {
  let language = ''
  let candidates: string[] = []
  await page.routeWebSocket('**/api/live', socket => {
    socket.onMessage(data => {
      if (typeof data === 'string') {
        const event = JSON.parse(data)
        if (event.type === 'stop') {
          socket.send(JSON.stringify({ type: 'transcript', text: 'Hello namaste.', final: true, segments: [{ locale: 'hi-IN' }] }))
          socket.send(JSON.stringify({ type: 'done' }))
        } else {
          language = event.language
          candidates = event.azure_languages
          socket.send(JSON.stringify({ type: 'ready' }))
        }
      } else socket.send(JSON.stringify({ type: 'transcript', text: 'Hello', final: false }))
    })
  })
  await setup(page)
  await page.getByRole('button', { name: 'Clear', exact: true }).click()
  const azure = page.getByRole('article', { name: 'Azure Speech', exact: true })
  await azure.getByRole('button', { name: '+ Include', exact: true }).click()
  await page.getByRole('button', { name: 'Live streaming', exact: true }).click()
  await configure(page, 'Azure Speech')
  await page.getByLabel('Language', { exact: true }).selectOption('auto')
  const guidance = page.getByRole('region', { name: 'Auto language detection', exact: true })
  await expect(guidance).toContainText('does not guarantee word-by-word mixed-language')
  await expect(guidance).toContainText('English, Hindi')
  await guidance.getByText('Edit candidates', { exact: true }).click()
  await expect(guidance.getByRole('checkbox', { name: 'English', exact: true })).toBeChecked()
  await expect(guidance.getByRole('checkbox', { name: 'Hindi', exact: true })).toBeChecked()
  await guidance.getByRole('checkbox', { name: 'Hindi', exact: true }).uncheck()
  await expect(page.getByRole('button', { name: 'Start live comparison', exact: true })).toBeDisabled()
  await expect(guidance.getByRole('status')).toContainText('at least two')
  await guidance.getByRole('checkbox', { name: 'Tamil', exact: true }).check()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.getByRole('button', { name: 'Start live comparison', exact: true }).click()
  await expect(azure.getByLabel('Azure Speech transcript', { exact: true })).toContainText('Hello')
  await expect(guidance.getByRole('checkbox', { name: 'Tamil', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: /^Stop \d/ }).click()
  await expect(azure.getByLabel('Azure Speech transcript', { exact: true })).toHaveText('Hello namaste.')
  expect(language).toBe('auto')
  expect(candidates).toEqual(['en-US', 'ta-IN'])
})

test('Azure live candidate limits stay visible and do not block file auto mode', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: 'Live streaming', exact: true }).click()
  await page.getByLabel('Language', { exact: true }).selectOption('auto')
  const guidance = page.getByRole('region', { name: 'Auto language detection', exact: true })
  await guidance.getByText('Edit candidates', { exact: true }).click()
  for (const name of ['Tamil', 'Telugu', 'Kannada', 'Malayalam', 'Marathi', 'Bengali', 'Gujarati', 'Odia']) {
    await guidance.getByRole('checkbox', { name, exact: true }).check()
  }
  await expect(guidance.getByRole('checkbox', { name: 'French', exact: true })).toBeDisabled()
  await expect(guidance).toContainText('10/10 selected')
  for (const checkbox of await guidance.getByRole('checkbox').all()) {
    if (await checkbox.isChecked()) await checkbox.uncheck()
  }
  await expect(page.getByRole('button', { name: 'Start live comparison', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Recorded / file', exact: true }).click()
  await expect(page.getByRole('group', { name: /Azure live candidate languages/ })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Record microphone', exact: true })).toBeEnabled()
  await expect(guidance).toContainText('multilingual model without a fixed locale')
})

test('sends identical audio, isolates errors, and exports no keys', async ({ page }) => {
  await setup(page)
  const bodies: string[] = []
  await page.route('**/api/transcribe', async route => {
    const body = route.request().postDataBuffer()!.toString()
    bodies.push(body)
    if (body.includes('"provider":"openai"')) {
      await route.fulfill({ status: 502, json: { detail: 'Provider returned HTTP 401. Check your API key.' } })
    } else await route.fulfill({ json: { text: 'A transcript from the same audio.', segments: [{ text: 'A transcript', speaker: 1 }], warnings: [], elapsed_ms: 750, model: body.includes('"provider":"azure"') ? 'azure-transcribe' : 'nova-3' } })
  })
  await configure(page, 'Azure Speech')
  await configure(page, 'OpenAI')
  await configure(page, 'Deepgram')
  await upload(page)
  await page.getByRole('button', { name: 'Transcribe with 3 providers' }).click()
  await expect(page.getByLabel('Azure Speech transcript', { exact: true })).toContainText('A transcript from the same audio.')
  await expect(page.getByLabel('Deepgram transcript', { exact: true })).toContainText('A transcript from the same audio.')
  await expect(page.getByLabel('OpenAI transcript', { exact: true })).toContainText('HTTP 401')
  expect(bodies).toHaveLength(3)
  expect(bodies.every(body => body.includes('RIFF-example-audio'))).toBe(true)
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Export JSON' }).click()
  const download = await downloaded
  const stream = await download.createReadStream()
  let exported = ''
  for await (const chunk of stream!) exported += chunk.toString()
  expect(exported).not.toContain('test-key-not-real')
  expect(JSON.parse(exported).transcripts).toHaveLength(3)
  await page.getByRole('button', { name: 'Details / JSON' }).click()
  await expect(page.getByLabel('Azure Speech transcript', { exact: true })).toContainText('"speaker": 1')
  await upload(page)
  await expect(page.getByRole('button', { name: 'Export JSON' })).toBeDisabled()
  await expect(page.getByLabel('Azure Speech transcript', { exact: true })).not.toContainText('"speaker": 1')
})

test('records microphone as playable WAV before file transcription', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: 'Record microphone', exact: true }).click()
  const stop = page.getByRole('button', { name: /^Stop \d/ })
  await expect(stop).toBeVisible()
  await page.waitForTimeout(500)
  await stop.click()
  await expect(page.getByText('microphone.wav', { exact: true })).toBeVisible()
  const bytes = await page.getByLabel('Audio playback', { exact: true }).evaluate(async (audio: HTMLAudioElement) => {
    const buffer = await (await fetch(audio.src)).arrayBuffer()
    const view = new DataView(buffer)
    return { header: new TextDecoder().decode(buffer.slice(0, 4)), rate: view.getUint32(24, true), size: buffer.byteLength }
  })
  expect(bytes.header).toBe('RIFF')
  expect(bytes.rate).toBe(16000)
  expect(bytes.size).toBeGreaterThan(44)
})

test('streams real mic PCM, replaces interim text, and drains final text on stop', async ({ page }) => {
  let binaryFrames = 0
  let stopReceived = false
  await page.routeWebSocket('**/api/live', socket => {
    socket.onMessage(data => {
      if (typeof data === 'string') {
        const event = JSON.parse(data)
        if (event.type === 'stop') {
          stopReceived = true
          socket.send(JSON.stringify({ type: 'transcript', text: 'Hello world.', final: true, segments: [] }))
          socket.send(JSON.stringify({ type: 'done' }))
        } else socket.send(JSON.stringify({ type: 'ready' }))
      } else {
        binaryFrames++
        socket.send(JSON.stringify({ type: 'transcript', text: binaryFrames === 1 ? 'Hel' : 'Hello', final: false }))
      }
    })
  })
  await setup(page)
  await page.getByRole('button', { name: 'Clear', exact: true }).click()
  const deepgram = page.getByRole('article', { name: 'Deepgram', exact: true })
  await deepgram.getByRole('button', { name: '+ Include', exact: true }).click()
  await page.getByRole('button', { name: 'Live streaming', exact: true }).click()
  await configure(page, 'Deepgram')
  await page.getByRole('button', { name: 'Start live comparison' }).click()
  await expect(page.getByLabel('Deepgram transcript', { exact: true })).toContainText('Hello')
  await page.getByRole('button', { name: /^Stop \d/ }).click()
  await expect(page.getByLabel('Deepgram transcript', { exact: true })).toHaveText('Hello world.')
  expect(binaryFrames).toBeGreaterThan(0)
  expect(stopReceived).toBe(true)
  await expect(page.getByText('microphone.wav', { exact: true })).toBeVisible()
})

test('reports microphone permission failures without leaving controls locked', async ({ page }) => {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () => Promise.reject(new DOMException('Microphone permission denied', 'NotAllowedError'))
  })
  await setup(page)
  await page.getByRole('button', { name: 'Record microphone', exact: true }).click()
  await expect(page.getByRole('alert')).toContainText('Microphone permission denied')
  await expect(page.getByRole('button', { name: 'Record microphone', exact: true })).toBeEnabled()
})

test('cancels file requests and preserves an explicit cancelled state', async ({ page }) => {
  await setup(page)
  await page.getByRole('button', { name: 'Clear', exact: true }).click()
  const deepgram = page.getByRole('article', { name: 'Deepgram', exact: true })
  await deepgram.getByRole('button', { name: '+ Include', exact: true }).click()
  await configure(page, 'Deepgram')
  await page.route('**/api/transcribe', async route => {
    await new Promise(resolve => setTimeout(resolve, 1500))
    await route.fulfill({ json: { text: 'Late result' } }).catch(() => {})
  })
  await upload(page)
  await page.getByRole('button', { name: 'Transcribe with 1 provider', exact: true }).click()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(deepgram.getByText('Cancelled', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Transcribe with 1 provider', exact: true })).toBeEnabled()
})

test('cancels pending live connections and releases the microphone', async ({ page }) => {
  let closed = false
  await page.routeWebSocket('**/api/live', socket => {
    socket.onClose(() => { closed = true })
  })
  await setup(page)
  await page.getByRole('button', { name: 'Clear', exact: true }).click()
  await page.getByRole('article', { name: 'Deepgram', exact: true }).getByRole('button', { name: '+ Include', exact: true }).click()
  await page.getByRole('button', { name: 'Live streaming', exact: true }).click()
  await configure(page, 'Deepgram')
  await page.getByRole('button', { name: 'Start live comparison' }).click()
  await expect(page.getByRole('article', { name: 'Deepgram', exact: true }).getByText('Connecting', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Start live comparison' })).toBeEnabled()
  expect(closed).toBe(true)
  await expect(page.getByText('Comparison cancelled.', { exact: true })).toBeVisible()
})
