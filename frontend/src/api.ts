export type Provider = 'azure' | 'openai' | 'deepgram' | 'soniox'
export type Mode = 'batch' | 'live'
export type Options = {
  provider: Provider
  api_key: string
  mode: Mode
  model: string
  language: string
  region: string
  resource: string
  diarize: boolean
  punctuate: boolean
  endpoint_ms: number
  prompt: string
}
export type Result = {
  text: string
  interim: string
  segments: Record<string, unknown>[]
  warnings: string[]
  status: 'idle' | 'connecting' | 'listening' | 'working' | 'done' | 'error' | 'cancelled'
  error?: string
  elapsed_ms?: number
  first_ms?: number
  model?: string
  mode?: Mode
}
export const emptyResult = (): Result => ({ text: '', interim: '', segments: [], warnings: [], status: 'idle' })
export const catalog: { id: Provider; name: string; icon: string; color: string; models: Record<Mode, string[]>; note: string }[] = [
  { id: 'azure', name: 'Azure Speech', icon: 'A', color: '#0078d4', models: { batch: ['azure-transcribe'], live: ['azure-realtime'] }, note: 'Fast file transcription / continuous live recognition' },
  { id: 'openai', name: 'OpenAI', icon: 'O', color: '#172c27', models: { batch: ['gpt-transcribe', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe', 'whisper-1'], live: [] }, note: 'File transcription. Live not included in this adapter.' },
  { id: 'deepgram', name: 'Deepgram', icon: 'D', color: '#14835d', models: { batch: ['nova-3', 'nova-2'], live: ['nova-3', 'nova-2'] }, note: 'File and live transcription with interim results' },
  { id: 'soniox', name: 'Soniox', icon: 'S', color: '#7751d1', models: { batch: ['stt-async-v5', 'stt-async-v4'], live: ['stt-rt-v5', 'stt-rt-v4'] }, note: 'Asynchronous files / real-time token streaming' },
]
export const initialOptions = (provider: Provider): Options => ({
  provider, api_key: '', mode: 'batch', model: catalog.find(p => p.id === provider)!.models.batch[0],
  language: 'en-US', region: 'eastus', resource: '', diarize: false, punctuate: true, endpoint_ms: 1000, prompt: '',
})
const configuredBase = (import.meta.env.VITE_API_BASE ?? '').replace(/\/+$/, '')
const apiBase = configuredBase || window.location.origin
export const needsBackend = import.meta.env.PROD && !configuredBase
export function endpoint(path: string) {
  return `${apiBase}${path}`
}

export async function transcribe(file: File, options: Options, signal: AbortSignal): Promise<Result> {
  const body = new FormData()
  body.append('audio', file)
  body.append('settings', JSON.stringify(options))
  const response = await fetch(endpoint('/api/transcribe'), { method: 'POST', body, signal })
  let result
  try {
    result = await response.json()
  } catch {
    throw new Error('Backend returned a non-JSON response. Check its URL and deployment.')
  }
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Invalid request. Check provider settings.')
  return { ...emptyResult(), ...result, mode: 'batch', status: 'done' }
}

export type LiveSession = { send: (bytes: ArrayBuffer) => void; stop: () => void; cancel: () => void; done: Promise<void> }

export function liveSession(options: Options, update: (fn: (previous: Result) => Result) => void, signal: AbortSignal): Promise<LiveSession> {
  const url = new URL(endpoint('/api/live'))
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  const socket = new WebSocket(url)
  const started = performance.now()
  let ready = false
  let terminal = false
  let resolveDone: () => void
  const done = new Promise<void>(resolve => { resolveDone = resolve })
  let stopTimer: ReturnType<typeof setTimeout> | undefined
  let audioStarted: number | undefined
  const fail = (message: string) => {
    if (terminal) return
    terminal = true
    update(previous => ({ ...previous, status: 'error', error: message }))
  }
  return new Promise((resolve, reject) => {
    const cancel = () => {
      if (terminal) return
      terminal = true
      update(previous => ({ ...previous, status: 'cancelled', interim: '' }))
      reject(new DOMException('Live comparison cancelled.', 'AbortError'))
      socket.close()
    }
    signal.addEventListener('abort', cancel, { once: true })
    if (signal.aborted) queueMicrotask(cancel)
    const timer = setTimeout(() => {
      fail('Live connection timed out. Check the backend and provider settings.')
      reject(new Error('Live connection timed out.'))
      socket.close()
    }, 35000)
    socket.onopen = () => socket.send(JSON.stringify(options))
    socket.onmessage = event => {
      if (terminal) return
      let data
      try { data = JSON.parse(event.data) } catch {
        fail('Invalid live response from backend.')
        reject(new Error('Invalid live response.'))
        socket.close()
        return
      }
      if (data.type === 'ready') {
        ready = true
        clearTimeout(timer)
        update(previous => ({ ...previous, status: 'listening' }))
        resolve({
          done,
          send(bytes) {
            if (socket.readyState !== WebSocket.OPEN || terminal) return
            audioStarted ??= performance.now()
            if (socket.bufferedAmount > 512000) {
              fail('Connection is too slow for live audio. Stop and retry with fewer providers.')
              socket.close()
            } else socket.send(bytes)
          },
          stop() {
            if (socket.readyState === WebSocket.OPEN && !terminal) {
              update(previous => ({ ...previous, status: 'working', interim: '' }))
              socket.send('{"type":"stop"}')
              stopTimer = setTimeout(() => {
                fail('Timed out waiting for final results; partial text is preserved.')
                socket.close()
              }, 30000)
            }
          },
          cancel,
        })
      } else if (data.type === 'transcript') {
        update(previous => ({
          ...previous,
          first_ms: previous.first_ms ?? (data.text && audioStarted !== undefined ? Math.round(performance.now() - audioStarted) : undefined),
          text: data.final && data.text ? previous.text + (previous.text ? (data.join ?? ' ') : '') + data.text : previous.text,
          interim: data.final ? '' : data.text,
          segments: data.final ? [...previous.segments, ...(data.segments ?? [])] : previous.segments,
        }))
      } else if (data.type === 'error') {
        fail(data.message)
        reject(new Error(data.message))
        socket.close()
      } else if (data.type === 'done') {
        terminal = true
        update(previous => ({ ...previous, status: 'done', interim: '', elapsed_ms: Math.round(performance.now() - started) }))
        socket.close()
      }
    }
    socket.onerror = () => {
      fail('WebSocket connection failed. Check the backend URL and its allowed origins.')
      reject(new Error('WebSocket connection failed.'))
      socket.close()
    }
    socket.onclose = () => {
      signal.removeEventListener('abort', cancel)
      clearTimeout(timer)
      clearTimeout(stopTimer)
      if (!terminal) fail(ready ? 'Live connection closed before final results. Partial text is preserved.' : 'Live connection was rejected. Check settings and backend availability.')
      if (!ready) reject(new Error('Live connection closed.'))
      resolveDone()
    }
  })
}
