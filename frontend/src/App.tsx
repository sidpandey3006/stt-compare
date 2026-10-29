import { useEffect, useRef, useState } from 'react'
import { capture, MAX_BYTES, MAX_SECONDS, wav } from './audio'
import { catalog, emptyResult, endpoint, initialOptions, liveSession, needsBackend, transcribe } from './api'
import type { LiveSession, Mode, Options, Provider, Result } from './api'

const ids = catalog.map(provider => provider.id)
const makeOptions = () => Object.fromEntries(ids.map(id => [id, initialOptions(id)])) as Record<Provider, Options>
const makeResults = () => Object.fromEntries(ids.map(id => [id, emptyResult()])) as Record<Provider, Result>
const message = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Please retry.'
const seconds = (value: number) => `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`

function download(name: string, value: string, type = 'application/json') {
  const url = URL.createObjectURL(new Blob([value], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = name
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export default function App() {
  const [mode, setMode] = useState<Mode>('batch')
  const [options, setOptions] = useState(makeOptions)
  const [selected, setSelected] = useState<Provider[]>(['azure', 'openai', 'deepgram'])
  const [visible, setVisible] = useState<Provider[]>(['azure', 'openai', 'deepgram', 'soniox'])
  const [providerSearch, setProviderSearch] = useState('')
  const providerDialog = useRef<HTMLDialogElement>(null)
  const [results, setResults] = useState(makeResults)
  const [file, setFile] = useState<File>()
  const [audioUrl, setAudioUrl] = useState('')
  const [language, setLanguage] = useState('en-US')
  const [phase, setPhase] = useState<'idle' | 'starting' | 'recording' | 'transcribing' | 'finishing'>('idle')
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking')
  const [details, setDetails] = useState<Provider[]>([])
  const [view, setView] = useState<'transcript' | 'segments'>('transcript')
  const recording = useRef<Awaited<ReturnType<typeof capture>> | null>(null)
  const sessions = useRef<LiveSession[]>([])
  const abort = useRef<AbortController | null>(null)
  const chunks = useRef<ArrayBuffer[]>([])
  const frameCount = useRef(0)
  const stopRef = useRef<() => void>(() => {})
  const cancelled = useRef(false)
  const mounted = useRef(true)
  const busy = phase !== 'idle'
  const active = visible.filter(id => selected.includes(id) && catalog.find(p => p.id === id)!.models[mode].length > 0)
  const available = catalog.filter(provider => !visible.includes(provider.id) && provider.name.toLowerCase().includes(providerSearch.toLowerCase().trim()))

  useEffect(() => {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 15000)
    if (!needsBackend) {
      fetch(endpoint('/api/health'), { signal: controller.signal }).then(response => {
        if (!response.ok) throw new Error('Backend unavailable')
        return response.json()
      }).then(data => setHealth(data.ok ? 'online' : 'offline')).catch(() => setHealth('offline'))
    } else setHealth('offline')
    return () => { clearTimeout(timer); controller.abort() }
  }, [])

  useEffect(() => {
    if (!file) { setAudioUrl(''); return }
    const url = URL.createObjectURL(file)
    setAudioUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])

  useEffect(() => {
    if (phase !== 'recording') return
    const start = Date.now()
    const timer = setInterval(() => {
      const current = Math.floor((Date.now() - start) / 1000)
      setElapsed(current)
      if (current >= MAX_SECONDS) {
        setNotice('Five-minute recording limit reached. Your audio has been saved.')
        stopRef.current()
      }
    }, 250)
    return () => clearInterval(timer)
  }, [phase])

  useEffect(() => {
    mounted.current = true
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault() }
    if (busy) window.addEventListener('beforeunload', unload)
    return () => window.removeEventListener('beforeunload', unload)
  }, [busy])

  useEffect(() => () => {
    mounted.current = false
    cancelled.current = true
    void recording.current?.stop()
    sessions.current.forEach(session => session.cancel())
    abort.current?.abort()
  }, [])

  function update(id: Provider, fn: (previous: Result) => Result) {
    if (mounted.current) setResults(previous => ({ ...previous, [id]: fn(previous[id]) }))
  }

  function settings(id: Provider): Options {
    const provider = catalog.find(p => p.id === id)!
    return {
      ...options[id], language, mode,
      model: provider.models[mode].includes(options[id].model) ? options[id].model : provider.models[mode][0],
      diarize: provider.diarization.includes(mode) && options[id].diarize,
    }
  }

  function validate() {
    if (needsBackend) throw new Error('The deployed frontend needs its API_BASE repository variable configured.')
    if (!active.length) throw new Error('Include at least one provider that supports this mode.')
    const missing = active.filter(id => !options[id].api_key.trim()).map(id => catalog.find(p => p.id === id)!.name)
    if (missing.length) {
      setDetails(previous => [...new Set([...previous, ...active.filter(id => !options[id].api_key.trim())])])
      throw new Error(`Enter API keys for ${missing.join(', ')} or exclude those providers.`)
    }
    if (active.includes('azure') && mode === 'batch' && !options.azure.resource.trim()) {
      setDetails(previous => [...new Set([...previous, 'azure' as Provider])])
      throw new Error('Enter your Azure resource name (not a project endpoint URL) in its settings.')
    }
    if (active.includes('azure') && mode === 'live' && language === 'auto') throw new Error('Choose an explicit language for Azure live transcription, or exclude Azure.')
  }

  function changeOption(id: Provider, patch: Partial<Options>) {
    setOptions(previous => ({ ...previous, [id]: { ...previous[id], ...patch } }))
  }

  function addProvider(id: Provider) {
    setVisible(previous => previous.includes(id) ? previous : [...previous, id])
    setSelected(previous => previous.includes(id) ? previous : [...previous, id])
    setDetails(previous => previous.includes(id) ? previous : [...previous, id])
    providerDialog.current?.close()
    const provider = catalog.find(value => value.id === id)!
    setNotice(provider.models[mode].length ? `${provider.name} added. Enter its API key under Settings.` : `${provider.name} added. Switch to recorded/file mode to use this adapter.`)
  }

  function removeProvider(id: Provider) {
    setVisible(previous => previous.filter(value => value !== id))
    setSelected(previous => previous.filter(value => value !== id))
    setDetails(previous => previous.filter(value => value !== id))
    setOptions(previous => ({ ...previous, [id]: initialOptions(id) }))
    update(id, emptyResult)
    setNotice(`${catalog.find(provider => provider.id === id)!.name} removed. Its key and results have been cleared.`)
  }

  function upload(value?: File) {
    if (!value) return
    setError('')
    if (value.size > MAX_BYTES) { setError('Choose an audio file smaller than 24 MiB.'); return }
    if (!value.size) { setError('The selected file is empty.'); return }
    if (!/\.(wav|mp3|mp4|mpeg|mpga|m4a|webm|ogg|flac|aac|opus)$/i.test(value.name)) {
      setError('Unsupported file type. Use WAV, MP3, MP4, M4A, WebM, Ogg, FLAC, AAC, or Opus.')
      return
    }
    setFile(value)
    setResults(makeResults())
    setNotice('Audio ready. Each included provider will receive this same file.')
  }

  async function runBatch() {
    setError('')
    setNotice('')
    try { validate() } catch (reason) { setError(message(reason)); return }
    if (!file) { setError('Record or upload audio first.'); return }
    const source = file
    const controller = new AbortController()
    abort.current = controller
    setResults(makeResults())
    setPhase('transcribing')
    const timeout = setTimeout(() => controller.abort(), 295000)
    await Promise.all(active.map(async id => {
      const config = settings(id)
      update(id, () => ({ ...emptyResult(), status: 'working', model: config.model, mode }))
      try {
        const result = await transcribe(source, config, controller.signal)
        update(id, () => result)
      } catch (reason) {
        update(id, previous => ({ ...previous, status: controller.signal.aborted ? 'cancelled' : 'error', error: controller.signal.aborted ? 'Request cancelled or timed out. Provider work may already have incurred charges.' : message(reason) }))
      }
    }))
    clearTimeout(timeout)
    abort.current = null
    if (mounted.current) setPhase('idle')
  }

  async function startRecording() {
    setError('')
    setNotice('')
    try { if (mode === 'live') validate() } catch (reason) { setError(message(reason)); return }
    cancelled.current = false
    const controller = new AbortController()
    abort.current = controller
    setResults(makeResults())
    chunks.current = []
    frameCount.current = 0
    setElapsed(0)
    setPhase('starting')
    sessions.current = []
    let microphone: Awaited<ReturnType<typeof capture>> | null = null
    try {
      microphone = await capture(data => {
        frameCount.current += data.byteLength / 2
        if (frameCount.current <= MAX_SECONDS * 16000) {
          chunks.current.push(data)
          sessions.current.forEach(session => session.send(data))
        } else stopRef.current()
      }, () => {
        setNotice('Microphone disconnected. Finalizing the recording.')
        stopRef.current()
      })
      if (cancelled.current) { await microphone.stop(); setPhase('idle'); return }
      if (mode === 'live') {
        const opened = await Promise.allSettled(active.map(id => {
          const config = settings(id)
          update(id, () => ({ ...emptyResult(), status: 'connecting', model: config.model, mode }))
          return liveSession(config, fn => update(id, fn), controller.signal)
        }))
        sessions.current = opened.flatMap(result => result.status === 'fulfilled' ? [result.value] : [])
        if (!sessions.current.length) throw new Error('No live providers connected. Check the errors in each card.')
      }
      if (cancelled.current) { await microphone.stop(); setPhase('idle'); return }
      microphone.start()
      setFile(undefined)
      recording.current = microphone
      setPhase('recording')
    } catch (reason) {
      await microphone?.stop()
      sessions.current.forEach(session => session.cancel())
      sessions.current = []
      if (cancelled.current) setNotice('Comparison cancelled.')
      else setError(message(reason))
      abort.current = null
      setPhase('idle')
    }
  }

  async function stopRecording() {
    const microphone = recording.current
    if (!microphone) return
    recording.current = null
    setPhase('finishing')
    try {
      await microphone.stop()
      const blob = wav(chunks.current)
      setFile(new File([blob], 'microphone.wav', { type: 'audio/wav' }))
      sessions.current.forEach(session => session.stop())
      await Promise.all(sessions.current.map(session => session.done))
      setNotice(cancelled.current ? 'Comparison cancelled. Partial text and your recording are preserved.' : mode === 'live' ? 'Live comparison ended. Check each card for its result. The recording is available for playback or file-mode comparison.' : 'Recording ready. Choose providers, then transcribe.')
    } catch (reason) {
      sessions.current.forEach(session => session.cancel())
      setError(message(reason))
    } finally {
      sessions.current = []
      abort.current = null
      if (mounted.current) setPhase('idle')
    }
  }
  stopRef.current = () => { void stopRecording() }

  function cancel() {
    cancelled.current = true
    abort.current?.abort()
    sessions.current.forEach(session => session.cancel())
    if (recording.current) void stopRecording()
  }

  function exportResults() {
    const transcripts = ids.filter(id => results[id].status !== 'idle').map(id => ({
      provider: id, ...results[id],
    }))
    download('stt-comparison.json', JSON.stringify({ audio: file?.name, transcripts }, null, 2))
  }

  const hasResults = ids.some(id => results[id].status !== 'idle')
  const statusLabels = { idle: 'Ready to compare', connecting: 'Connecting', listening: 'Listening live', working: 'Transcribing', done: 'Complete', error: 'Needs attention', cancelled: 'Cancelled' }

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href={import.meta.env.BASE_URL}><span className="brand-mark" aria-hidden="true">|||</span> speech<span className="brand-light">compare</span><span className="beta">STT LAB</span></a>
        <div className="top-links"><a href="https://sidpandey3006.github.io/tts-compare/">Try TTS Compare <span aria-hidden="true">{'\u2197'}</span></a><span className={`connection ${health}`}><i />{health === 'online' ? 'API connected' : health === 'checking' ? 'Checking API' : 'API offline'}</span></div>
      </header>

      <main>
        <section className="hero">
          <div className="eyebrow"><span /> THE SPEECH-TO-TEXT PLAYGROUND</div>
          <h1>Your audio.<br /><span>Every perspective.</span></h1>
          <p>One recording. Multiple speech models. See what each one hears,<br className="desktop-break" /> side by side, on the audio that matters to you.</p>
          <div className="hero-tags"><span>Bring your own keys</span><span>No app-side audio storage</span><span>Real provider APIs</span></div>
        </section>

        {needsBackend && <div className="alert warning">This preview is not connected to a backend. Deploy the API, set the repository variable API_BASE, and rebuild Pages. Microphone recording still works locally.</div>}
        {!needsBackend && health === 'offline' && <div className="alert warning">The API is not responding. A free Render instance may be waking up. Check the backend URL if transcription fails.</div>}

        <section className="studio" aria-label="Speech comparison playground">
          <div className="studio-toolbar">
            <div className="studio-title"><span className="live-dot" /> Comparison studio <span className="count">{active.length} included</span></div>
            <fieldset disabled={busy} className="toolbar-settings">
              <label className="language-label"><span>Language</span><select aria-label="Language" value={language} onChange={event => setLanguage(event.target.value)}>
                <option value="auto">Auto / multilingual</option>
                <option value="en-US">English (US)</option><option value="en-IN">English (India)</option><option value="en-GB">English (UK)</option>
                <option value="hi-IN">Hindi</option><option value="ta-IN">Tamil</option><option value="te-IN">Telugu</option><option value="kn-IN">Kannada</option>
                <option value="ml-IN">Malayalam</option><option value="mr-IN">Marathi</option><option value="bn-IN">Bengali</option><option value="gu-IN">Gujarati</option>
                <option value="or-IN">Odia</option><option value="pa-IN">Punjabi</option><option value="as-IN">Assamese</option><option value="ur-IN">Urdu</option><option value="ne-IN">Nepali</option>
                <option value="kok-IN">Konkani</option><option value="ks-IN">Kashmiri</option><option value="sd-IN">Sindhi</option><option value="sa-IN">Sanskrit</option><option value="sat-IN">Santali</option>
                <option value="mni-IN">Manipuri</option><option value="brx-IN">Bodo</option><option value="mai-IN">Maithili</option><option value="doi-IN">Dogri</option>
                <option value="es-ES">Spanish</option><option value="fr-FR">French</option><option value="de-DE">German</option>
                <option value="ja-JP">Japanese</option><option value="zh-CN">Chinese</option><option value="ar-SA">Arabic</option>
              </select></label>
              <button className="button subtle" onClick={() => setSelected(visible)}>Include all</button>
              <button className="button subtle" onClick={() => setSelected([])}>Clear</button>
              <button className="button" onClick={() => { setProviderSearch(''); providerDialog.current?.showModal() }}>+ Add provider</button>
            </fieldset>
          </div>

          <div className="input-zone">
            <div className="input-top"><div><h2>Start with your audio</h2><p>Speak naturally or bring a recording. Everyone gets the same input.</p></div>
              <div className="mode-tabs" role="group" aria-label="Transcription mode">
                <button aria-pressed={mode === 'batch'} disabled={busy} className={mode === 'batch' ? 'active' : ''} onClick={() => setMode('batch')}>Recorded / file</button>
                <button aria-pressed={mode === 'live'} disabled={busy} className={mode === 'live' ? 'active' : ''} onClick={() => setMode('live')}><span className="small-dot" /> Live streaming</button>
              </div>
            </div>
            <div className="audio-controls">
              <button className={`record-button ${phase === 'recording' ? 'recording' : ''}`} disabled={busy && phase !== 'recording'} onClick={() => phase === 'recording' ? void stopRecording() : void startRecording()}>
                <span aria-hidden="true">{phase === 'recording' ? '\u25a0' : '\u25cf'}</span>
                {phase === 'recording' ? `Stop ${seconds(elapsed)}` : phase === 'starting' ? 'Connecting...' : mode === 'live' ? 'Start live comparison' : 'Record microphone'}
              </button>
              <span className="or">or</span>
              <label className={`upload-button ${busy || mode === 'live' ? 'disabled' : ''}`}>
                <span aria-hidden="true">&uarr;</span> Upload audio
                <input aria-label="Upload audio" type="file" accept=".wav,.mp3,.mp4,.mpeg,.mpga,.m4a,.webm,.ogg,.flac,.aac,.opus" disabled={busy || mode === 'live'} onChange={event => { upload(event.target.files?.[0]); event.target.value = '' }} />
              </label>
              <div className="audio-hint">{mode === 'live' ? 'True live PCM \u00b7 interim + final results' : 'WAV, MP3, M4A, WebM + more \u00b7 up to 24 MiB'}<span>{mode === 'live' ? 'Mic only \u00b7 five-minute session limit' : 'Mic recordings: 16 kHz mono \u00b7 up to five minutes'}</span></div>
            </div>
            {file && <div className="audio-preview"><div><span className="file-icon" aria-hidden="true">&#9835;</span><strong>{file.name}</strong><span>{(file.size / 1024 / 1024).toFixed(2)} MiB</span></div><audio controls src={audioUrl || undefined} aria-label="Audio playback" /><button className="button subtle" disabled={busy} onClick={() => { setFile(undefined); setNotice('') }}>Remove</button></div>}
            <div className="input-footer"><p>{mode === 'batch' ? 'Recorded/file mode waits for complete audio. Azure uses Fast Transcription; Soniox queues an asynchronous job. Sarvam file clips must be under 30 seconds.' : 'Azure, Deepgram, Soniox, ElevenLabs, and Sarvam support live here. OpenAI and Gemini are file-only in this version.'}</p>
              {mode === 'batch' && <button className="button primary" disabled={busy || !file || !active.length} onClick={() => void runBatch()}>{phase === 'transcribing' ? 'Transcribing...' : `Transcribe with ${active.length} provider${active.length === 1 ? '' : 's'}`} <span aria-hidden="true">&rarr;</span></button>}
              {(phase === 'starting' || phase === 'transcribing' || phase === 'finishing') && <button className="button danger" onClick={cancel}>Cancel</button>}
            </div>
          </div>

          <div className="messages" aria-live="polite">
            {error && <div className="alert error" role="alert">{error}</div>}
            {notice && <div className="notice">{notice}</div>}
          </div>

          <div className="results-toolbar"><div><h2>Different models. Same moment.</h2><p>Include a provider and add its key under Settings.</p></div><div className="results-actions">
            <div className="view-tabs"><button className={view === 'transcript' ? 'active' : ''} onClick={() => setView('transcript')}>Transcript</button><button className={view === 'segments' ? 'active' : ''} onClick={() => setView('segments')}>Details / JSON</button></div>
            <button className="button subtle" disabled={!hasResults || busy} onClick={exportResults}>Export JSON <span aria-hidden="true">&darr;</span></button>
          </div></div>

          <div className="provider-grid">
            {visible.map(id => {
              const provider = catalog.find(value => value.id === id)!
              const supported = provider.models[mode].length > 0
              const included = selected.includes(id)
              const result = results[id]
              const config = settings(id)
              const open = details.includes(id)
              return <article className={`provider-card ${included && supported ? 'included' : 'excluded'}`} key={id} style={{ '--provider-color': provider.color } as React.CSSProperties} aria-label={provider.name}>
                <div className="provider-heading"><div className="provider-identity"><span className="provider-logo">{provider.icon}</span><div><h3>{provider.name}</h3><span>{supported ? config.model : 'File mode only'}</span></div></div>
                  <div className="provider-card-actions"><button disabled={busy || !supported} aria-pressed={included && supported} className={`include-button ${included && supported ? 'on' : ''}`} onClick={() => setSelected(previous => previous.includes(id) ? previous.filter(value => value !== id) : [...previous, id])}>{!supported ? 'Unavailable' : included ? '\u2713 Included' : '+ Include'}</button><button className="remove-provider" disabled={busy} aria-label={`Remove ${provider.name}`} title={`Remove ${provider.name} and clear its key`} onClick={() => removeProvider(id)}>{'\u00d7'}</button></div>
                </div>
                <div className="provider-config"><span className={`status ${result.status}`}><i />{statusLabels[result.status]}</span><button aria-expanded={open} className="settings-toggle" onClick={() => setDetails(previous => open ? previous.filter(value => value !== id) : [...previous, id])}>Settings {open ? '\u2212' : '+'}</button></div>
                {open && <fieldset className="settings-panel" disabled={busy || !supported}>
                  <label>API key <input type="password" autoComplete="off" spellCheck={false} aria-label={`${provider.name} API key`} value={options[id].api_key} placeholder="Kept in memory for this tab" onChange={event => changeOption(id, { api_key: event.target.value })} /></label>
                  <label>Model <select aria-label={`${provider.name} model`} value={config.model ?? ''} onChange={event => changeOption(id, { model: event.target.value })}>{provider.models[mode].map(model => <option key={model}>{model}</option>)}</select></label>
                  {id === 'azure' && <label>{mode === 'batch' ? 'Azure resource name' : 'Azure region'}<input aria-label={mode === 'batch' ? 'Azure resource name' : 'Azure region'} value={mode === 'batch' ? options.azure.resource : options.azure.region} placeholder={mode === 'batch' ? 'my-speech-resource (not a URL)' : 'eastus'} onChange={event => changeOption(id, mode === 'batch' ? { resource: event.target.value } : { region: event.target.value })} /></label>}
                  {provider.diarization.includes(mode) && <label className="check-label"><input type="checkbox" checked={config.diarize} onChange={event => changeOption(id, { diarize: event.target.checked })} /> Speaker diarization (labels in Details / JSON)</label>}
                  {id === 'deepgram' && <label className="check-label"><input type="checkbox" checked={config.punctuate} onChange={event => changeOption(id, { punctuate: event.target.checked })} /> Punctuation and smart formatting</label>}
                  {mode === 'live' && (id === 'deepgram' || id === 'soniox' || id === 'sarvam') && <label>Endpoint silence: {config.endpoint_ms} ms<input aria-label={`${provider.name} endpoint silence`} type="range" min="500" max="3000" step="100" value={config.endpoint_ms} onChange={event => changeOption(id, { endpoint_ms: Number(event.target.value) })} /></label>}
                  {(id === 'openai' || id === 'soniox') && <label>Transcription context (optional)<textarea value={config.prompt} maxLength={2000} placeholder="Relevant names, terminology, or context" onChange={event => changeOption(id, { prompt: event.target.value })} /></label>}
                  {id === 'sarvam' && <><label>Output mode<select aria-label="Sarvam output mode" value={config.sarvam_mode} onChange={event => {
                    const value = event.target.value
                    if (value === 'transcribe' || value === 'verbatim' || value === 'codemix' || value === 'translit') changeOption(id, { sarvam_mode: value })
                  }}><option value="transcribe">Transcribe (normalized)</option><option value="verbatim">Verbatim (include fillers)</option><option value="codemix">Code-mixed (native + English)</option><option value="translit">Transliterate to Roman script</option></select></label><p>English hints use Sarvam's en-IN code for all English locales. Auto uses the provider's language detection.</p></>}
                  <p>{provider.note}. Language and model availability depend on your account.</p>
                  {id === 'openai' && <p>Legacy Whisper / 4o models retire February 26, 2027. Prefer gpt-transcribe.</p>}
                </fieldset>}
                <div className="transcript-body" aria-live="polite" aria-label={`${provider.name} transcript`}>
                  {result.error && <div className="card-error" role="alert">{result.error}</div>}
                  {result.warnings.map(warning => <div className="card-warning" key={warning}>{warning}</div>)}
                  {view === 'segments' && result.segments.length > 0 ? <pre>{JSON.stringify(result.segments, null, 2)}</pre> : result.text || result.interim ? <p className="transcript-text">{result.text}<span className="interim"> {result.interim}</span></p> : <div className="empty-transcript"><div className="waveform" aria-hidden="true">{[12, 24, 38, 22, 46, 30, 18, 35, 15].map((height, index) => <i key={index} style={{ height }} />)}</div><p>{result.status === 'working' ? 'Listening to your recording...' : result.status === 'listening' ? 'Speak now. Words will appear here.' : !supported ? 'Switch to file mode to compare this provider.' : result.status === 'done' ? 'No speech detected in this audio.' : 'A fresh perspective awaits.'}</p><span>{result.status === 'idle' ? 'Your transcript will appear here' : statusLabels[result.status]}</span></div>}
                </div>
                <div className="card-footer"><div><strong>{result.text.trim() ? result.text.trim().split(/\s+/).length : 0}</strong> words{result.first_ms !== undefined && <span>First text {(result.first_ms / 1000).toFixed(2)}s</span>}{result.elapsed_ms !== undefined && <span>{result.mode === 'live' ? 'Session' : 'Request'} {(result.elapsed_ms / 1000).toFixed(2)}s</span>}</div><button className="copy-button" disabled={!result.text} onClick={() => { navigator.clipboard.writeText(result.text).then(() => setNotice(`${provider.name} transcript copied.`)).catch(() => setError('Clipboard access failed. Export the results instead.')) }}>Copy</button></div>
                {result.model && <div className="result-source">Last result: {result.model} &middot; {result.mode === 'live' ? 'live' : 'file'}</div>}
              </article>
            })}
          </div>
          {visible.length === 0 && <div className="empty-providers">No providers yet. Choose <strong>Add provider</strong> above to start a comparison.</div>}
          <div className="studio-bottom"><span>Same input. Independent requests. One provider failing never blocks the others.</span><button className="button subtle" disabled={busy || !hasResults} onClick={() => { setResults(makeResults()); setNotice('Results cleared.') }}>Clear results</button></div>
        </section>

        <dialog ref={providerDialog} aria-labelledby="provider-dialog-title" className="provider-dialog">
          <div className="provider-dialog-heading"><div><h2 id="provider-dialog-title">Add a provider</h2><p>Choose an adapter, then enter your own API key.</p></div><form method="dialog"><button className="remove-provider" aria-label="Close provider picker">{'\u00d7'}</button></form></div>
          <label className="provider-search">Search providers<input autoFocus type="search" value={providerSearch} onChange={event => setProviderSearch(event.target.value)} placeholder="ElevenLabs, Sarvam, Gemini..." /></label>
          <div className="provider-picker-list">
            {available.map(provider => <button key={provider.id} disabled={busy} aria-label={`Add ${provider.name}`} onClick={() => addProvider(provider.id)} className="provider-choice">
              <span className="provider-logo" style={{ color: provider.color }}>{provider.icon}</span><span className="provider-choice-text"><strong>{provider.name}</strong><span>{provider.note}</span><small>{provider.models.live.length ? 'Recorded / file + live streaming' : 'Recorded / file only in this app'}</small></span><span aria-hidden="true">+</span>
            </button>)}
            {available.length === 0 && <p className="picker-empty">{visible.length === catalog.length ? 'All available providers have been added.' : 'No matching providers.'}</p>}
          </div>
          <p className="picker-footnote">Only included cards receive audio. Removing a card clears its key and results. Custom provider URLs are not supported.</p>
        </dialog>

        <section className="how-it-works"><div><span>01</span><h3>Bring your audio</h3><p>Record from your microphone or upload a file. Live mode streams the same 16 kHz PCM to each provider.</p></div><div><span>02</span><h3>Choose your models</h3><p>Use your own provider keys. Tune language, speaker labels, and available model-specific settings.</p></div><div><span>03</span><h3>Trust your own ears</h3><p>Read transcripts side by side. Export results for review. Observed timings are not controlled benchmarks.</p></div></section>
        <div className="privacy-note"><strong>Your audio, your choice.</strong> Audio and keys pass through this backend to your selected providers. This app does not intentionally save them or log request bodies. Provider retention policies and charges still apply. Soniox file jobs are deleted after processing; cleanup failures are reported. Do not upload sensitive audio. Cancel stops local delivery but cannot guarantee cancellation of upstream work.</div>
      </main>
      <footer className="page-footer"><span>speechcompare <span className="muted">/ STT playground</span></span><span>Built for curiosity. Not a leaderboard.</span></footer>
    </div>
  )
}
